"""Read-only readiness, with explicit-start identity refresh through owned operations."""

import json
import re
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from .account_blocks import active_block
from .cloud_control import CloudUnavailable
from .core import Conflict, NotFound
from .delivery_policy import late
from .models import digest, epoch
from .phone_protocol import capability_status
from .phone_rounds import PREFLIGHT_SECONDS, TOTAL_SECONDS
from .task_channels import control_preflight, safe_channel_code


def _fleet_capabilities(service):
    try:
        return capability_status(service.request("GET", "/capabilities"))
    except Exception as exc:
        reason = getattr(exc, "safe_code", "")
        if not isinstance(reason, str) or not re.fullmatch(r"Phone[A-Za-z0-9_]{1,90}", reason):
            reason = "PhoneFleetReadinessUnavailable"
        return {"software_ready": False, "device_ready": False, "reason": reason}


def _member_blockers(engine, member, now, service, remote, *, refresh_identity=False):
    store = engine.store
    reasons = []
    item = store.one("SELECT * FROM items WHERE id=?", (member["item_id"],))
    task = store.one("SELECT * FROM tasks WHERE id=?", (member["task_id"],))
    if not task or not item or item["task_id"] != member["task_id"]:
        return ["PhoneRoundOriginalItemMissing"]
    if late(json.loads(task["policy"]), item["due_at"], now):
        reasons.append("PhoneItemDeliveryWindowElapsed")
    account = engine.account(item["account_id"])
    if account["platform"] != member["platform"]:
        return ["PhoneRoundAccountBindingChanged"]
    with store.connect() as db:
        try:
            policy = engine._integrity(db, task, "outbound")
        except Conflict:
            reasons.append("ManifestChanged")
            policy = {}
        if active_block(db, account["id"], now):
            reasons.append("AccountOutboundBlocked")
    if account["disabled"] or (account["status"] != "healthy" and not refresh_identity):
        reasons.append("AccountNotHealthy")
    if max(account["cooldown_until"], account["next_at"]) > now:
        reasons.append("AccountWaitRequired")
    if not policy.get("ends_at") or epoch(policy["ends_at"]) <= now:
        reasons.append("TaskWindowElapsed")
    if service is None or account["id"] not in service.config.bindings:
        reasons.append("PhoneBindingRequired")
    else:
        if not remote.get("software_ready"):
            reasons.append(remote.get("reason") or "PhoneFleetReadinessUnavailable")
        elif not remote.get("device_ready"):
            reasons.append("PhoneFleetDeviceNotReady")
        elif (
            remote.get("operations", {}).get(
                "reddit_submit" if member["platform"] == "reddit" else "social_submit"
            )
            != "supported"
        ):
            reasons.append("PhoneFleetSubmissionUnsupported")
        if not service.config.submit_enabled:
            reasons.append("PhoneSubmissionDisabled")
        authorized = (service.task_authorized(task["id"], account["id"])
                      if hasattr(service, "task_authorized") else task["id"] in service.config.authorized_task_ids)
        if not authorized:
            reasons.append("PhoneTaskOutsideAuthorizedScope")
        try:
            if (refresh_identity and not reasons and not service.fresh(account)
                    and callable(getattr(service, "ensure_identity", None))):
                from .execution_timing import stage
                with stage(engine, task["id"], "phone_identity_refresh", account_id=account["id"]):
                    service.ensure_identity(account["id"], task_id=task["id"])
            if (refresh_identity and not reasons and account["status"] != "healthy"):
                account = engine.check_account(account["id"], force=True, task_id=task["id"])
                if account["status"] != "healthy":
                    reasons.append("AccountNotHealthy")
            if not service.fresh(account):
                reasons.append("FreshPhoneIdentityRequired")
        except Exception:
            reasons.append("FreshPhoneIdentityRequired")
        if member["platform"] == "reddit":
            if not service.config.reddit_read_access_token:
                reasons.append("OfficialReadAuthorizationRequired")
        else:
            binding = service.config.bindings[account["id"]]
            if member["platform"] == "telegram" and binding.receipt_reader == "telegram_api":
                if account["id"] not in service.config.telegram_observers:
                    reasons.append("OfficialReadAuthorizationRequired")
                elif reason := service.telegram_queue_blocker(account["id"]):
                    reasons.append(reason)
            else:
                try:
                    _, _, observation = service.preparation(
                        account["id"],
                        json.loads(item["payload"]),
                        item_id=item["id"],
                        task_id=task["id"],
                    )
                except Exception:
                    observation = None
                proof = ((observation or {}).get("result") or {}).get("social") or {}
                if not (
                    observation
                    and observation.get("status") == "succeeded"
                    and (observation.get("result") or {}).get("simulated") is False
                    and proof.get("receipt_capable") is True
                ):
                    reasons.append("IndependentReceiptCommissioningRequired")
    return reasons


# Leave time in the admission budget to save healthy peers if one provider stalls.
FLEET_CHECK_SECONDS = 15


def preflight(rounds, round_id, *, platforms=None, on_members=None, refresh_identity=False):
    engine, store, now = rounds.engine, rounds.store, rounds.clock()
    started = time.monotonic()
    snapshot = rounds.get(round_id)
    selected = set(snapshot["selected_platforms"])
    if platforms is not None and (not platforms or not set(platforms) <= selected):
        raise NotFound("PhoneRoundPlatformNotSelected")
    scoped = [m for m in snapshot["members"] if platforms is None or m["platform"] in platforms]
    common = []
    if not engine.live_enabled:
        common.append("LiveWritesDisabled")
    if store.one("SELECT value FROM meta WHERE key='global_stop'")["value"] != "0":
        common.append("GlobalStop")
    runtime = store.one("SELECT value FROM meta WHERE key='execution_runtime'")
    if not runtime or runtime["value"] != "pooled":
        common.append("PooledRuntimeRequired")
    # Resolve local control before spending time on remote capability checks.
    # Each member owns its failures; a bad account never hides healthy peers.
    controls, control_errors, control_seconds = {}, {}, {}
    for member in scoped:
        control_start = time.monotonic()
        task = store.one("SELECT policy FROM tasks WHERE id=?", (member["task_id"],))
        item = store.one("SELECT account_id FROM items WHERE id=?", (member["item_id"],))
        try:
            if not task or not item:
                raise CloudUnavailable("PhoneRoundOriginalItemMissing")
            controls[member["task_id"]] = control_preflight(
                engine, member["task_id"], item["account_id"], json.loads(task["policy"])
            )
        except CloudUnavailable as exc:
            control_errors[member["task_id"]] = safe_channel_code(str(exc))
        except Exception:
            control_errors[member["task_id"]] = "PhoneControlReadinessUnavailable"
        control_seconds[member["task_id"]] = max(0, time.monotonic() - control_start)
    sources, platform_services = {}, {}
    for member in scoped:
        service = getattr(
            engine.adapters,
            "phone_reddit" if member["platform"] == "reddit" else "phone_social",
            None,
        )
        platform_services[member["platform"]] = service
        if service is not None and not common and member["task_id"] not in control_errors:
            sources[id(service)] = service
    members, completed = [], set()

    def publish(selected_members, remote, member_reasons=None):
        batch = []
        for member in selected_members:
            identifier = member["task_id"]
            if identifier in completed:
                continue
            service = platform_services[member["platform"]]
            try:
                if identifier in control_errors:
                    reasons = [control_errors[identifier]]
                elif common:
                    reasons = []
                else:
                    reasons = (member_reasons if member_reasons is not None else
                               _member_blockers(engine, member, rounds.clock(), service, remote))
            except Exception:
                reasons = ["PhoneMemberReadinessUnavailable"]
            value = {
                "platform": member["platform"],
                "task_id": identifier,
                "ready": not reasons and not common,
                "account_control": controls.get(identifier),
                "blockers": common + list(dict.fromkeys(reasons)),
                "checked_at": rounds.clock(),
                "timing": {
                    "control_seconds": control_seconds[identifier],
                    "preflight_seconds": max(0, time.monotonic() - started),
                },
            }
            completed.add(identifier)
            batch.append(value)
            members.append(value)
        if batch and on_members:
            on_members(batch)

    # Known local failures are reported immediately, with no remote work.
    publish(
        [
            m
            for m in scoped
            if common
            or m["task_id"] in control_errors
            or id(platform_services[m["platform"]]) not in sources
        ],
        {},
    )
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        futures = {
            pool.submit(_fleet_capabilities, service): key for key, service in sources.items()
        }
        fleet_deadline = time.monotonic() + FLEET_CHECK_SECONDS
        deadline = time.monotonic() + (PREFLIGHT_SECONDS - 1 if refresh_identity else FLEET_CHECK_SECONDS)
        pending = set(futures)
        while pending:
            until = min(deadline, fleet_deadline) if any(
                not isinstance(futures[f], dict) for f in pending) else deadline
            done, pending = wait(
                pending, timeout=max(0, until - time.monotonic()), return_when=FIRST_COMPLETED
            )
            for future in done:
                key = futures[future]
                if isinstance(key, dict):
                    try:
                        reasons = future.result()
                    except Exception:
                        reasons = ["PhoneMemberIdentityRefreshFailed"]
                    publish([key], {}, reasons)
                    continue
                selected = [m for m in scoped if id(platform_services[m["platform"]]) == key]
                remote = future.result()
                if refresh_identity:
                    for member in selected:
                        job = pool.submit(_member_blockers, engine, member, rounds.clock(),
                                          platform_services[member["platform"]], remote,
                                          refresh_identity=True)
                        futures[job] = member
                        pending.add(job)
                else:
                    publish(selected, remote)
            if time.monotonic() >= fleet_deadline:
                for future in list(pending):
                    key = futures[future]
                    if isinstance(key, dict):
                        continue
                    pending.remove(future)
                    future.cancel()
                    publish([m for m in scoped if id(platform_services[m["platform"]]) == key],
                            {"reason": "PhoneFleetReadinessTimeout"})
            if not done and time.monotonic() >= deadline:
                break
        for future in pending:
            key = futures[future]
            publish(
                [key] if isinstance(key, dict) else
                [m for m in scoped if id(platform_services[m["platform"]]) == key],
                {"software_ready": False,
                 "reason": "PhoneIdentityRefreshPending" if isinstance(key, dict) else "PhoneFleetReadinessTimeout"},
            )
    finally:
        # Never abandon an external submit: these futures only read capabilities.
        pool.shutdown(wait=False, cancel_futures=True)
    members.sort(key=lambda m: m["platform"])
    return {
        "round_id": round_id,
        "observed_at": now,
        "read_only": not refresh_identity,
        "readiness_scope": "account_control_local_configuration_and_authenticated_fleet_capabilities",
        "capability_sources_checked": len(sources),
        "ready_count": sum(m["ready"] for m in members),
        "members": members,
        "submission_authorized": False,
    }


def shortlist(engine, account_id, *, limit=3):
    """Reuse existing reviewed content; original planner and sender still recheck every gate."""
    if type(limit) is not int or not 1 <= limit <= 3:
        raise Conflict("PhoneRoundCandidateLimitInvalid")
    account, now = engine.account(account_id), engine.clock()
    if account["mode"] != "live":
        raise Conflict("PhoneRoundLiveAccountRequired")
    found, excluded, targets = [], {}, set()
    with engine.store.connect() as db:
        rows = db.execute(
            "SELECT * FROM opportunities WHERE platform=? ORDER BY updated_at DESC,id LIMIT 2000",
            (account["platform"],),
        ).fetchall()
        for row in rows:
            value = json.loads(row["payload"])
            reason = None
            target = value.get("target", "").casefold()
            try:
                expires = epoch(value["expires_at"]) if value.get("expires_at") else 0
                published = epoch(value["published_at"]) if value.get("published_at") else 0
            except (ValueError, TypeError, OverflowError):
                expires = published = 0
            if not (
                0 <= now - row["updated_at"] <= 900
                and 0 <= now - published <= 72 * 3600
                and expires >= now + TOTAL_SECONDS
            ):
                reason = "stale_or_short_window"
            elif (
                value.get("permission") != "allowed"
                or not value.get("permission_evidence", "").strip()
                or account_id not in value.get("permission_account_ids", [])
            ):
                reason = "account_permission_required"
            elif not value.get("draft_text", "").strip() or not value.get("context", "").strip():
                reason = "contextual_draft_required"
            elif not db.execute(
                "SELECT 1 FROM audit WHERE kind='growth.content_reviewed' AND json_extract(details,'$.opportunity_id')=? AND json_extract(details,'$.revision')=? LIMIT 1",
                (row["id"], row["revision"]),
            ).fetchone():
                reason = "current_revision_review_required"
            elif db.execute(
                "SELECT 1 FROM attempts WHERE target_key=? LIMIT 1",
                (digest([account["platform"], target]),),
            ).fetchone():
                # Includes unknown outcomes, regardless of local account aliases.
                reason = "existing_attempt_query_only"
            elif (
                target in targets
                or db.execute(
                    "SELECT 1 FROM items i JOIN tasks t ON t.id=i.task_id JOIN accounts a ON a.id=i.account_id WHERE a.platform=? AND lower(json_extract(i.payload,'$.target'))=? AND t.state='active' LIMIT 1",
                    (account["platform"], target),
                ).fetchone()
            ):
                reason = "target_already_scheduled"
            if reason:
                excluded[reason] = excluded.get(reason, 0) + 1
                continue
            targets.add(target)
            found.append(
                {
                    "opportunity_id": row["id"],
                    "revision": row["revision"],
                    "content_key": row["content_key"],
                    "payload_hash": digest(value),
                    "target": value["target"],
                    "expires_at": value["expires_at"],
                }
            )
            if len(found) >= limit:
                break
    return {
        "account_id": account_id,
        "platform": account["platform"],
        "observed_at": now,
        "read_only": True,
        "submission_authorized": False,
        "candidates": found,
        "blockers": [] if found else ["ReviewedCandidatePoolEmpty"],
        "refresh_required": not bool(found),
        "next_actions": []
        if found
        else ["collect_fresh_content", "review_account_permission", "prepare_contextual_draft"],
        "excluded": excluded,
        "scan_limit": 2000,
    }
