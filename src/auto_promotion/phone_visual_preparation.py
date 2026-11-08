"""Durable per-item phone preparation; no outbound attempts or permits are issued here."""

import json

import httpx

from phone_fleet_protocol import ExecutionRequest, ExecutionSnapshot, fingerprint
from phone_fleet_protocol.visual_review import context_sha256, pending_visual_review_result

from .backends.common import BackendError
from .models import canonical, epoch

TERMINAL = {"confirmed", "failed_known", "cancelled", "missed"}


def rows(engine, task_id):
    with engine.store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='phone_visual_preparations'").fetchone():
            return []
        return [dict(r) for r in db.execute("SELECT * FROM phone_visual_preparations WHERE task_id=?", (task_id,))]


def summary(engine, task_id):
    values = rows(engine, task_id)
    items = [{k: r[k] for k in ("item_id", "phase", "native_execution_id", "review_id", "created_at",
                               "prepared_at", "review_synced_at", "expires_at", "reason")} for r in values]
    return {"items": items, "pending_review": any(r["phase"] == "waiting_review" for r in values),
            "device_held": any(r["phase"] == "native_running" for r in values),
            "blocked": any(r["phase"] in {"blocked", "expired", "unknown"} for r in values)}


def _update(service, item_id, **values):
    from .store import Store

    allowed = {"phase", "request", "native_execution_id", "review_id", "source_snapshot",
               "review_decision_sha256", "prepared_at", "review_synced_at", "expires_at", "reason"}
    if not set(values) <= allowed:
        raise ValueError("Unknown preparation fields")
    with service.store.connect(write=True) as db:
        previous = db.execute("SELECT * FROM phone_visual_preparations WHERE item_id=?", (item_id,)).fetchone()
        changed = {k: v for k, v in values.items() if previous[k] != v}
        if not changed:
            return
        changed["updated_at"] = service.engine.clock()
        db.execute("UPDATE phone_visual_preparations SET " + ",".join(k + "=?" for k in changed) + " WHERE item_id=?",
                   [*changed.values(), item_id])
        if "phase" in changed:
            Store.audit(db, "phone.visual_preparation", {"item_id": item_id, "phase": changed["phase"],
                "previous_phase": previous["phase"], "reason": values.get("reason"),
                "native_execution_id": values.get("native_execution_id", previous["native_execution_id"]),
                "review_id": values.get("review_id", previous["review_id"]),
                "device_held": changed["phase"] == "native_running"}, previous["task_id"], service.engine.clock())


def trusted_ready(service, account_id, task_id, *, item_id=None):
    """Task-specific preparation evidence; never refreshes a cached identity timestamp.

    It permits the original queue to reach the new native pass, where identity,
    target, body, review and the one-use action permission are checked afresh.
    """
    if not (getattr(service.config, "async_visual_review_enabled", False)
            and getattr(service.config, "dynamic_task_authorization_enabled", False)):
        return None
    binding = service.config.bindings.get(account_id)
    commissioned = service.config.single_pass_adapters.get(account_id)
    if not binding or not commissioned:
        return None
    with service.store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='phone_visual_preparations'").fetchone():
            return None
        sql = ("SELECT p.*,t.manifest_hash AS current_manifest,t.state AS task_state,t.phase AS task_phase,t.policy,"
               "i.payload,i.status AS item_status FROM phone_visual_preparations p JOIN tasks t ON t.id=p.task_id "
               "JOIN items i ON i.id=p.item_id WHERE p.task_id=? AND i.account_id=? AND p.phase='ready'")
        params = [task_id, account_id]
        if item_id is not None:
            sql += " AND p.item_id=?"
            params.append(item_id)
        candidates = [dict(r) for r in db.execute(sql, params)]
    for row in candidates:
        try:
            if (row["task_state"] not in {"active", "paused"} or row["task_phase"] != "ready"
                    or row["item_status"] not in {"not_attempted", "executing"}
                    or row["manifest_hash"] != row["current_manifest"] or not row["review_decision_sha256"]
                    or not row["expires_at"] or row["expires_at"] <= service.engine.clock()
                    or epoch(json.loads(row["policy"])["ends_at"]) <= service.engine.clock()):
                continue
            request = ExecutionRequest.model_validate_json(row["request"])
            snapshot = ExecutionSnapshot.model_validate_json(row["source_snapshot"])
            proof = snapshot.result
            payload = json.loads(row["payload"])
            if (not proof or proof.simulated is not False or snapshot.status != "waiting_for_human"
                    or not snapshot.resource_released or snapshot.id != row["native_execution_id"]
                    or fingerprint(snapshot.request) != fingerprint(request)
                    or request.business_task_id != task_id or request.task_item_id != "visual-" + row["item_id"]
                    or request.manifest_sha256 != row["current_manifest"] or request.account_id != account_id
                    or request.device_id != binding.device_id or request.social.identity != binding.identity
                    or request.social.client_id != binding.client_id or request.social.receipt_reader != binding.receipt_reader
                    or request.social.target_url != payload["target"] or request.social.text != payload["text"]
                    or proof.social.adapter_sha256 != commissioned.adapter_sha256
                    or not pending_visual_review_result(request, proof, proof.observed_at)
                    or proof.social.visual_review.review_id != row["review_id"]
                    or proof.social.visual_review.expires_at != row["expires_at"]):
                continue
            return {**row, "context": request.social.model_dump(mode="json"),
                    "adapter_sha256": proof.social.adapter_sha256}
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return None


def item_requires_review(service, item_id):
    if not getattr(service.config, "async_visual_review_enabled", False):
        return False
    with service.store.connect() as db:
        return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='phone_visual_preparations'").fetchone()
                    and db.execute("SELECT 1 FROM phone_visual_preparations WHERE item_id=?", (item_id,)).fetchone())


def submit_context(service, account_id, task_id, item_id):
    if not item_requires_review(service, item_id):
        return None
    prepared = trusted_ready(service, account_id, task_id, item_id=item_id)
    if not prepared:
        raise BackendError("PhoneVisualPreparationRequired")
    return {**prepared["context"], "defer_visual_review": False, "visual_review_id": prepared["review_id"],
            "visual_preparation_execution_id": prepared["native_execution_id"]}


def _accept_source(service, row, request, snapshot):
    proof = snapshot.result
    if (snapshot.status != "waiting_for_human" or not snapshot.resource_released
            or not proof or proof.simulated is not False
            or fingerprint(snapshot.request) != fingerprint(request)
            or not pending_visual_review_result(request, proof, proof.observed_at)):
        _update(service, row["item_id"], phase="blocked", reason="PhoneVisualCheckpointRequired")
        return None
    checkpoint = proof.social.visual_review
    _update(service, row["item_id"], phase="waiting_review", native_execution_id=snapshot.id,
        review_id=checkpoint.review_id, source_snapshot=canonical(snapshot.model_dump(mode="json")),
        prepared_at=proof.observed_at, expires_at=checkpoint.expires_at, reason=None)
    # Preserve the actual observation time; this is not a fabricated health refresh.
    _, binding = service.binding(request.account_id)
    with service.store.connect(write=True) as db:
        db.execute("UPDATE phone_social_accounts SET observation=?,observed_at=? WHERE account_id=? "
                   "AND identity=? AND device_id=? AND observed_at<=?",
                   (canonical(snapshot.model_dump(mode="json")), proof.observed_at, request.account_id,
                    binding.identity, binding.device_id, proof.observed_at))
    return checkpoint


def _step_item(service, task, item, row):
    if row["phase"] in {"blocked", "expired", "unknown"}:
        return False
    if row["expires_at"] and row["expires_at"] <= service.engine.clock():
        _update(service, item["id"], phase="expired", reason="visual_review_expired")
        return False
    if row["phase"] == "ready":
        if not trusted_ready(service, item["account_id"], task["id"], item_id=item["id"]):
            _update(service, item["id"], phase="blocked", reason="visual_preparation_context_changed")
        return False
    dispatched = False
    if not row["request"]:
        commissioned = service.config.single_pass_adapters.get(item["account_id"])
        if not commissioned:
            _update(service, item["id"], phase="blocked", reason="visual_adapter_not_commissioned")
            return False
        payload = item["payload"]
        account, binding = service.binding(item["account_id"])
        baseline = service.observer_baseline(account["id"], binding.identity, payload["target"])
        context = {"target_url": payload["target"], "target_id": baseline["peer_username"],
                   "target_author": "@" + baseline["peer_username"], "target_text": baseline["peer_title"],
                   "text": payload["text"], "discard_after_prepare": False, "defer_visual_review": True,
                   "expected_adapter_sha256": commissioned.adapter_sha256}
        request = service.make_request(account["id"], "social_prepare", context, task_id=task["id"],
            item_id="visual-" + item["id"], manifest=task["manifest_hash"], channel="local")
        request = ExecutionRequest.model_validate({**request.model_dump(mode="json"),
            "deadline": min(request.deadline, epoch(task["policy"]["ends_at"]))})
        with service.store.connect(write=True) as db:
            db.execute("UPDATE phone_visual_preparations SET request=?,phase='native_queued',updated_at=? "
                       "WHERE item_id=? AND request IS NULL", (canonical(request.model_dump(mode="json")),
                       service.engine.clock(), item["id"]))
        row = service.store.one("SELECT * FROM phone_visual_preparations WHERE item_id=?", (item["id"],))
    request = ExecutionRequest.model_validate_json(row["request"])
    if row["source_snapshot"]:
        snapshot = ExecutionSnapshot.model_validate_json(row["source_snapshot"])
    else:
        # Stable operation id and persisted request: uncertain create replies are
        # resolved by original lookup, never another native preparation POST.
        value = service.dispatch(request, row["operation_id"], item["account_id"], item["id"], wait=False)
        dispatched = True
        if not value.get("id"):
            _update(service, item["id"], phase="waiting_connection", reason="original_preparation_lookup_only")
            return dispatched
        snapshot = ExecutionSnapshot.model_validate(value)
        if snapshot.status in {"queued", "running", "paused"}:
            _update(service, item["id"], phase="native_running" if snapshot.status == "running" else "native_queued",
                    native_execution_id=snapshot.id, reason=None)
            return dispatched
        if snapshot.status in {"unknown", "failed", "cancelled"}:
            _update(service, item["id"], phase="unknown" if snapshot.status == "unknown" else "blocked",
                    native_execution_id=snapshot.id, source_snapshot=canonical(value),
                    reason=(snapshot.result.code if snapshot.result else "native_preparation_unresolved"))
            return dispatched
        if not _accept_source(service, row, request, snapshot):
            return dispatched
    checkpoint = snapshot.result.social.visual_review
    if checkpoint.expires_at <= service.engine.clock():
        _update(service, item["id"], phase="expired", reason="visual_review_expired")
        return dispatched
    try:
        review = service.request("GET", "/visual-reviews/" + checkpoint.review_id, timeout=3)
    except BackendError as error:
        if "AuthorizationRequired" in error.safe_code or "OAuthInvalid" in error.safe_code:
            _update(service, item["id"], phase="blocked", reason="review_metadata_authorization_failed")
        return dispatched
    except httpx.HTTPError:
        return dispatched
    if (review.get("execution_id") != snapshot.id or review.get("review_id") != checkpoint.review_id
            or review.get("image_sha256") != checkpoint.image_sha256
            or review.get("context_sha256") != context_sha256(request.social)
            or review.get("expires_at") != checkpoint.expires_at):
        _update(service, item["id"], phase="blocked", reason="visual_review_correlation_mismatch")
    elif review.get("phase") == "reviewed" and review.get("decision_sha256"):
        _update(service, item["id"], phase="ready", review_decision_sha256=review["decision_sha256"],
                review_synced_at=review["updated_at"], reason=None)
    elif review.get("phase") in {"rejected", "expired", "closed_by_stop"}:
        _update(service, item["id"], phase="expired" if review["phase"] == "expired" else "blocked", reason=review["phase"])
    return dispatched


def prepare_task(engine, task_id, manifest_hash, workflow_id):
    from .account_control_state import snapshot as control
    from .core import Conflict

    task = engine.task(task_id)
    link = engine.store.one("SELECT * FROM temporal_task_links WHERE task_id=?", (task_id,))
    if (not link or link["manifest_hash"] != manifest_hash or link["workflow_id"] != workflow_id
            or task["manifest_hash"] != manifest_hash or link["requested_at"] is None):
        raise Conflict("TemporalTaskCorrelationRequired")
    value = summary(engine, task_id)
    service = getattr(engine.adapters, "phone_social", None)
    if not service or not getattr(service.config, "async_visual_review_enabled", False):
        return {**value, "waiting_for": "async_review_not_enabled"}
    if task["state"] != "active" or task["phase"] != "ready" or engine.clock() >= epoch(task["policy"]["ends_at"]):
        return {**value, "waiting_for": "task_stopped_or_expired"}
    first = {}
    for item in task["items"]:
        if item["status"] not in TERMINAL:
            first.setdefault(item["account_id"], item)
    row_index = {row["item_id"]: row for row in rows(engine, task_id)}
    retry_at, waits = None, []
    for account_id, item in first.items():
        row = row_index.get(item["id"])
        if not row or item["status"] != "not_attempted":
            continue
        permission = control(engine, account_id, task_id=task_id)
        if not permission["allowed_read"]:
            waits.append({"account_id": account_id, "reason": permission["reason"]})
            continue
        # Only a baseline read with no persisted native intent may wait/recover.
        # A busy observer is a per-account queue condition, never a failed submit.
        if (row["phase"] == "blocked" and row["reason"] == "AccountBusy"
                and not row["request"] and not row["native_execution_id"]):
            _update(service, item["id"], phase="waiting_account", reason="AccountBusy")
            row = service.store.one("SELECT * FROM phone_visual_preparations WHERE item_id=?", (item["id"],))
        if not row["request"]:
            if row["phase"] == "waiting_account" and engine.clock() - row["updated_at"] >= 120:
                _update(service, item["id"], phase="blocked", reason="account_read_wait_timeout")
                continue
            lane = service.store.one("SELECT 1 occupied FROM account_lanes WHERE account_id=? AND expires_at>?",
                                     (account_id, engine.clock()))
            if lane:
                _update(service, item["id"], phase="waiting_account", reason="AccountBusy")
                waits.append({"account_id":account_id,"reason":"AccountBusy"})
                retry_at = min(retry_at or engine.clock()+2, engine.clock()+2)
                continue
        due = item.get("due_at") or engine.clock()
        if due > engine.clock() + 30 and not row["request"]:
            retry_at = min(retry_at or due, due - 30)
            continue
        try:
            dispatched = _step_item(service, task, item, row)
        except BackendError as error:
            if error.safe_code == "AccountBusy" and not row["request"] and not row["native_execution_id"]:
                _update(service, item["id"], phase="waiting_account", reason="AccountBusy")
                waits.append({"account_id":account_id,"reason":"AccountBusy"})
                retry_at = min(retry_at or engine.clock()+2, engine.clock()+2)
            else:
                _update(service, item["id"], phase="blocked", reason=error.safe_code)
            dispatched = False
        except (ValueError, TypeError, KeyError) as error:
            _update(service, item["id"], phase="blocked", reason="visual_preparation_invalid_" + type(error).__name__)
            dispatched = False
        if dispatched:
            break  # At most one native enqueue/lookup per bounded coordination call.
    return {**summary(engine, task_id), "account_waits": waits, "retry_at": retry_at}
