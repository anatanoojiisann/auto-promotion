"""Cloud first, with a fenced, quota-only local execution channel.

Both channels consume the same durable queue. A server deployment must bind the
workspace to this node and database before local takeover can be enabled.
"""

import hashlib
import json
import os
import socket
import time
import uuid

from .cloud_control import CloudControl, CloudUnavailable
from .models import canonical, epoch
from .store import Store


def read_state(store):
    row = store.one("SELECT value FROM meta WHERE key='execution_channel'")
    return json.loads(row["value"]) if row else {"active": "cloud"}


def offline_only(store, account_id=None):
    """A scoped local-only instruction cannot change an unrelated account's route."""
    state = read_state(store)
    return state.get("offline_only") is True and (
        account_id is None or not state.get("account_ids") or account_id in state["account_ids"]
    )


def binding(engine):
    existing = engine.store.one("SELECT value FROM meta WHERE key='execution_store_id'")
    if existing:
        return existing["value"]
    with engine.store.connect(write=True) as db:
        db.execute("INSERT OR IGNORE INTO meta VALUES('execution_store_id',?)", (uuid.uuid4().hex,))
        return db.execute("SELECT value FROM meta WHERE key='execution_store_id'").fetchone()[0]


def enabled(store):
    row = store.one("SELECT value FROM meta WHERE key='execution_channel_policy'")
    return bool(row and row["value"] == "cloud_then_local")


def independent_scope(db):
    row = db.execute("SELECT value FROM meta WHERE key='local_channel_authority'").fetchone()
    return json.loads(row[0]) if row else None


def independent_active(engine):
    state = read_state(engine.store)
    return bool(state.get("authorization") == "local_queue" and local_active(engine, None))


def local_account_scope(db, now):
    row = db.execute("SELECT value FROM meta WHERE key='execution_channel'").fetchone()
    state = json.loads(row[0]) if row else {}
    if state.get("active") == "local" and state.get("authorization") == "local_queue":
        return state.get("account_ids", []) if state.get("until", 0) > now else []
    return None


def configure_independent(
    engine,
    account_ids,
    until,
    *,
    source="explicit_user_local",
    through_task_ids=None,
    offline_only_requested=False,
):
    """Local ownership handoff, authorized by the user, with no server request.

    Claims, quota accounting and provider authorization stay in the same SQLite
    database. No attempt/outcome or publication switch is reset by this handoff.
    """
    now = engine.clock()
    if not account_ids or not now < until:
        raise ValueError("InvalidLocalChannelScope")
    for account_id in account_ids:
        engine.account(account_id)
    continuation = sorted(set(through_task_ids or []))
    if continuation:
        deadlines = []
        for task_id in continuation:
            task = engine.store.one("SELECT * FROM tasks WHERE id=?", (task_id,))
            if (
                not task
                or task["state"] not in {"active", "completed"}
                or task["phase"] not in {"starting", "ready", "discovered"}
                or not set(json.loads(task["account_ids"])).issubset(account_ids)
            ):
                raise ValueError("InvalidLocalContinuationTask")
            deadlines.append(epoch(json.loads(task["policy"])["ends_at"]))
        if until > max(deadlines):
            raise ValueError("LocalContinuationExceedsTaskWindow")
    elif until > now + 86400:
        raise ValueError("InvalidLocalChannelScope")
    store_id = binding(engine)
    authority = {"account_ids": sorted(set(account_ids)), "source": source, "at": now}
    if continuation:
        authority.update(through_task_ids=continuation, through_until=until)
    state = {
        "active": "local",
        "authorization": "local_queue",
        "reason": source,
        "since": now,
        "until": until,
        "node_id": os.getenv("AP_NODE_ID", socket.gethostname()),
        "store_id": store_id,
        "generation": uuid.uuid4().hex,
        "account_ids": authority["account_ids"],
    }
    if offline_only_requested or offline_only(engine.store):
        state["offline_only"] = True
    if continuation:
        state.update(through_task_ids=continuation)
    with engine.store.connect(write=True) as db:
        if any(
            db.execute(sql, params).fetchone()
            for sql, params in (
                (
                    "SELECT id FROM execution_jobs WHERE state='leased' AND lease_until>? LIMIT 1",
                    (now,),
                ),
                ("SELECT id FROM items WHERE status='executing' LIMIT 1", ()),
                ("SELECT id FROM candidates WHERE state='executing' LIMIT 1", ()),
            )
        ):
            raise CloudUnavailable("LocalChannelHandoffBusy")
        if db.execute("SELECT 1 FROM meta WHERE key='local_fallback_expiry_requested'").fetchone():
            raise CloudUnavailable("LocalChannelOtherHandoffPending")
        if state.get("offline_only") is True:
            from .task_channels import binding_matches, channel_binding

            fences = {}
            for account_id in authority["account_ids"]:
                prior = db.execute(
                    "SELECT * FROM account_task_channels WHERE account_id=?", (account_id,)
                ).fetchone()
                bound = channel_binding(db, account_id)
                if (
                    not prior
                    or prior["state"] != "ready"
                    or prior["error"]
                    or prior["retry_at"] > now
                ):
                    raise CloudUnavailable("OfflinePriorOwnershipUnresolved")
                if not binding_matches(prior, bound, state["node_id"], store_id):
                    raise CloudUnavailable("OfflinePriorOwnershipUnverified")
                if prior["channel"] == "cloud":
                    if bound.get("fence_source") != "cloud_acknowledged" or prior["until_at"] > now:
                        raise CloudUnavailable("OfflinePriorCloudStillActive")
                    fences[account_id] = {
                        "generation": prior["generation"],
                        "until_at": prior["until_at"],
                    }
                elif prior["channel"] != "local":
                    raise CloudUnavailable("OfflinePriorOwnershipUnverified")
                if db.execute(
                    "SELECT 1 FROM cloud_receipt_outbox r LEFT JOIN execution_jobs j ON j.id=r.job_id "
                    "WHERE json_extract(r.lease,'$.account_id')=? OR j.account_id=? LIMIT 1",
                    (account_id, account_id),
                ).fetchone():
                    raise CloudUnavailable("LocalChannelReceiptPending")
            state["offline_fences"] = fences
        # Renew acknowledged local routes within the user's explicit scope.
        # Cloud-owned or unresolved routes retain their normal handoff checks.
        for account_id in authority["account_ids"]:
            prior = db.execute(
                "SELECT * FROM account_task_channels WHERE account_id=?", (account_id,)
            ).fetchone()
            if (
                prior
                and prior["channel"] == "local"
                and prior["state"] == "ready"
                and not prior["error"]
                and prior["retry_at"] <= now
                and prior["until_at"] < until
            ):
                generation = uuid.uuid4().hex
                db.execute(
                    "UPDATE account_task_channels SET until_at=?,generation=?,"
                    "fence_source='independent_local',updated_at=? WHERE account_id=?",
                    (until, generation, now, account_id),
                )
                Store.audit(
                    db,
                    "execution.local_account_authority_renewed",
                    {
                        "account_id": account_id,
                        "previous_until": prior["until_at"],
                        "until": until,
                        "generation": generation,
                        "source": source,
                    },
                    now=now,
                )
        for key, value in (
            ("execution_channel_policy", "cloud_then_local"),
            ("local_channel_authority", canonical(authority)),
            ("execution_channel", canonical(state)),
            ("execution_runtime", "pooled"),
        ):
            db.execute(
                "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        Store.audit(
            db,
            "execution.local_authority_enabled",
            {**state, "source": source, "live_switch_changed": False, "global_stop_changed": False},
            now=now,
        )
    return state


def extend_offline_scope(engine, account_ids, *, source="explicit_user_local_retry"):
    """Add explicitly requested accounts without disturbing another local scope."""
    from .task_channels import binding_matches, channel_binding

    requested = sorted(set(account_ids))
    if not requested or source not in {"explicit_user_local_retry", "explicit_user_local_task"}:
        raise ValueError("OfflineScopeRequiresExplicitUser")
    now = engine.clock()
    with engine.store.connect(write=True) as db:
        row = db.execute("SELECT value FROM meta WHERE key='execution_channel'").fetchone()
        scope = independent_scope(db)
        state = json.loads(row[0]) if row else {}
        if not (
            scope
            and state.get("active") == "local"
            and state.get("authorization") == "local_queue"
            and state.get("offline_only") is True
            and state.get("until", 0) > now
            and sorted(state.get("account_ids", [])) == sorted(scope.get("account_ids", []))
            and not state.get("through_task_ids")
            and state.get("generation")
        ):
            raise CloudUnavailable("OfflineScopeUnavailable")
        store = db.execute("SELECT value FROM meta WHERE key='execution_store_id'").fetchone()
        if not store or store[0] != state.get("store_id"):
            raise CloudUnavailable("LocalChannelBindingMismatch")
        if db.execute("SELECT 1 FROM meta WHERE key='local_fallback_expiry_requested'").fetchone():
            raise CloudUnavailable("LocalChannelOtherHandoffPending")
        added, fences = [], dict(state.get("offline_fences", {}))
        authorizations = dict(state.get("account_authorizations", {}))
        for account_id in requested:
            if account_id in state["account_ids"]:
                continue
            account = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if (
                not account
                or account["platform"] != "x"
                or account["mode"] != "live"
                or account["status"] != "healthy"
                or account["disabled"]
                or account["cooldown_until"] > now
            ):
                raise CloudUnavailable("OfflineAccountUnavailable")
            if db.execute(
                "SELECT 1 FROM account_blocks WHERE account_id=? AND state='active' AND (until_at IS NULL OR until_at>?)",
                (account_id, now),
            ).fetchone():
                raise CloudUnavailable("OfflineAccountBlocked")
            prior = db.execute(
                "SELECT * FROM account_task_channels WHERE account_id=?", (account_id,)
            ).fetchone()
            bound = channel_binding(db, account_id)
            if (
                not prior
                or prior["state"] != "ready"
                or prior["retry_at"] > now
                or prior["error"] not in (None, "OperationalError")
                or not binding_matches(prior, bound, state["node_id"], store[0])
            ):
                raise CloudUnavailable("OfflinePriorOwnershipUnverified")
            ack = db.execute(
                "SELECT id,details FROM audit WHERE kind='execution.account_channel_changed' AND json_extract(details,'$.account_id')=? ORDER BY id DESC LIMIT 1",
                (account_id,),
            ).fetchone()
            if not ack:
                raise CloudUnavailable("OfflinePriorOwnershipUnverified")
            event = json.loads(ack["details"])
            if (
                event.get("channel") != prior["channel"]
                or event.get("generation") != prior["generation"]
            ):
                raise CloudUnavailable("OfflinePriorOwnershipUnverified")
            if db.execute(
                "SELECT 1 FROM audit WHERE id>? AND kind='execution.account_handoff_started' AND json_extract(details,'$.account_id')=? LIMIT 1",
                (ack["id"], account_id),
            ).fetchone():
                raise CloudUnavailable("OfflinePriorOwnershipUnresolved")
            if prior["channel"] == "cloud":
                if bound.get("fence_source") != "cloud_acknowledged" or prior["until_at"] > now:
                    raise CloudUnavailable("OfflinePriorCloudStillActive")
                fences[account_id] = {
                    "generation": prior["generation"],
                    "until_at": prior["until_at"],
                }
            elif prior["channel"] != "local":
                raise CloudUnavailable("OfflinePriorOwnershipUnverified")
            if db.execute(
                "SELECT 1 FROM execution_jobs WHERE account_id=? AND state='leased' AND lease_until>?",
                (account_id, now),
            ).fetchone():
                raise CloudUnavailable("LocalChannelHandoffBusy")
            if db.execute(
                "SELECT 1 FROM items WHERE account_id=? AND status='executing'", (account_id,)
            ).fetchone():
                raise CloudUnavailable("LocalChannelHandoffBusy")
            if db.execute(
                "SELECT 1 FROM cloud_receipt_outbox r LEFT JOIN execution_jobs j ON j.id=r.job_id WHERE json_extract(r.lease,'$.account_id')=? OR j.account_id=? LIMIT 1",
                (account_id, account_id),
            ).fetchone():
                raise CloudUnavailable("LocalChannelReceiptPending")
            added.append(account_id)
            authorizations[account_id] = {"at": now, "source": source, "prior_ack_id": ack["id"]}
        merged = sorted(set(state["account_ids"]) | set(requested))
        state.update(
            account_ids=merged, offline_fences=fences, account_authorizations=authorizations
        )
        scope["account_ids"] = merged
        db.execute("UPDATE meta SET value=? WHERE key='execution_channel'", (canonical(state),))
        db.execute(
            "UPDATE meta SET value=? WHERE key='local_channel_authority'", (canonical(scope),)
        )
        if added:
            Store.audit(
                db,
                "execution.local_scope_extended",
                {"account_ids": added, "source": source, "offline_only": True},
                now=now,
            )
    return {"added": added, "account_ids": merged, "until": state["until"], "offline_only": True}


def recover_local_renewal(engine, account_id):
    """Finish an interrupted local-to-local renewal with durable ownership proof.

    An intervening cloud handoff, outstanding lease/receipt or publication attempt
    prevents recovery. This does not authorize a cloud-to-local offline switch.
    """
    now = engine.clock()
    if not independent_active(engine):
        raise ValueError("IndependentLocalAuthorityRequired")
    with engine.store.connect(write=True) as db:
        authority = json.loads(
            db.execute("SELECT value FROM meta WHERE key='execution_channel'").fetchone()[0]
        )
        route = db.execute(
            "SELECT * FROM account_task_channels WHERE account_id=?", (account_id,)
        ).fetchone()
        if (
            account_id not in authority.get("account_ids", [])
            or not route
            or route["channel"] != "local"
            or route["state"] != "handoff"
            or route["until_at"] > authority["until"]
        ):
            raise ValueError("LocalRenewalNotInScope")
        ack = db.execute(
            "SELECT id,details FROM audit WHERE kind='execution.account_channel_changed' AND json_extract(details,'$.account_id')=? ORDER BY id DESC LIMIT 1",
            (account_id,),
        ).fetchone()
        if not ack or json.loads(ack["details"]).get("channel") != "local":
            raise ValueError("LocalOwnershipNotProven")
        if db.execute(
            "SELECT 1 FROM audit WHERE id>? AND kind='execution.account_handoff_started' AND json_extract(details,'$.account_id')=? AND json_extract(details,'$.channel')<>'local'",
            (ack["id"], account_id),
        ).fetchone():
            raise ValueError("InterveningCloudHandoff")
        task = db.execute(
            "SELECT state,policy FROM tasks WHERE id=?", (route["task_id"],)
        ).fetchone()
        if (
            not task
            or task["state"] not in {"active", "paused"}
            or json.loads(task["policy"]).get("execution_channel") != "local"
        ):
            raise ValueError("LocalRenewalTaskMismatch")
        if (
            db.execute(
                "SELECT 1 FROM execution_jobs WHERE account_id=? AND state='leased' AND lease_until>?",
                (account_id, now),
            ).fetchone()
            or db.execute(
                "SELECT 1 FROM items WHERE task_id=? AND attempt_id IS NOT NULL",
                (route["task_id"],),
            ).fetchone()
            or db.execute(
                "SELECT 1 FROM cloud_receipt_outbox r JOIN execution_jobs j ON j.id=r.job_id WHERE j.account_id=?",
                (account_id,),
            ).fetchone()
        ):
            raise ValueError("LocalRenewalHasOutstandingWork")
        db.execute(
            "UPDATE account_task_channels SET state='ready',fence_source='independent_local',updated_at=?,error=NULL,retry_at=0 WHERE account_id=? AND generation=?",
            (now, account_id, route["generation"]),
        )
        Store.audit(
            db,
            "execution.local_renewal_recovered",
            {
                "account_id": account_id,
                "generation": route["generation"],
                "previous_local_ack_id": ack["id"],
                "until": route["until_at"],
                "attempts_changed": False,
            },
            route["task_id"],
            now,
        )
        return {
            "account_id": account_id,
            "state": "ready",
            "channel": "local",
            "generation": route["generation"],
            "until": route["until_at"],
        }


def local_context_valid(db, context, *, now=None):
    """Called again by the provider, using durable state, never an env bypass."""
    if not context or context.get("channel") != "local":
        return False
    if context.get("task_channel"):
        from .task_channels import valid_context

        return valid_context(db, context, now=time.time() if now is None else now)
    policy = db.execute("SELECT value FROM meta WHERE key='execution_channel_policy'").fetchone()
    row = db.execute("SELECT value FROM meta WHERE key='execution_channel'").fetchone()
    if not policy or policy[0] != "cloud_then_local" or not row:
        return False
    state = json.loads(row[0])
    independent = independent_scope(db)
    reason_allowed = state.get("reason") == "CloudWriteQuotaExceeded" or (
        independent
        and state.get("authorization") == "local_queue"
        and state.get("reason") == "explicit_user_local"
    )
    return bool(
        state.get("active") == "local"
        and reason_allowed
        and state.get("until", 0) > (time.time() if now is None else now)
        and context.get("generation") == state.get("generation")
        and context.get("store_id") == state.get("store_id")
        and context.get("node_id") == state.get("node_id")
    )


def check_standby(engine, cloud, job=None):
    """Read-only server fencing/stop/ledger check; cannot publish or lease remotely."""
    if not cloud:
        raise CloudUnavailable("CloudControlConfigurationMissing")
    job_input = None
    if job:
        job_input = {k: job[k] for k in ("id", "account_id", "task_id", "pool", "version")}
        if job["kind"] == "send":
            item = engine.store.one("SELECT payload FROM items WHERE id=?", (job["reference_id"],))
            payload = json.loads(item["payload"])
            job_input.update(
                {"action": payload["action"], "manual": payload.get("execution_mode") == "manual"}
            )
        else:
            job_input.update({"action": "join", "manual": False})
    cached = engine.store.one("SELECT value FROM meta WHERE key='cloud_standby_permit'")
    result = cloud.request(
        "/v1/standby-check",
        {
            "store_id": binding(engine),
            "job": job_input,
            "permit": json.loads(cached["value"]) if cached else None,
        },
    )
    if not isinstance(result, dict) or result.get("allowed") is not True:
        raise CloudUnavailable("LocalChannelFenceUnavailable")
    if result.get("node_id") != cloud.node_id or result.get("store_id") != binding(engine):
        raise CloudUnavailable("LocalChannelBindingMismatch")
    settings = engine.execution_settings()
    remote = result["settings"]
    for key in ("daily", "hourly"):
        if settings["global_limit"][key] > remote["global_limit"][key]:
            raise CloudUnavailable("LocalChannelLimitsDiffer")
    if settings["max_workers"] > remote["max_workers"]:
        raise CloudUnavailable("LocalChannelLimitsDiffer")
    for section in ("pool_workers", "platform_workers"):
        for key, limit in remote.get(section, {}).items():
            default = 1 if section == "pool_workers" else settings["max_workers"]
            if settings.get(section, {}).get(key, default) > limit:
                raise CloudUnavailable("LocalChannelLimitsDiffer")
    for platform, limit in remote.get("platform_limits", {}).items():
        local = settings.get("platform_limits", {}).get(platform, settings["global_limit"])
        if any(local[key] > limit[key] for key in ("daily", "hourly")):
            raise CloudUnavailable("LocalChannelLimitsDiffer")
    # A different or incomplete local ledger must never take over cloud work.
    with engine.store.connect() as db:
        for attempt in result["attempts"]:
            table, column = (
                ("attempts", "item_id")
                if attempt["kind"] == "send"
                else ("join_attempts", "candidate_id")
            )
            if not db.execute(
                f"SELECT 1 FROM {table} WHERE id=? AND {column}=? AND account_id=?",
                (attempt["attempt_id"], attempt["reference_id"], attempt["account_id"]),
            ).fetchone():
                raise CloudUnavailable("LocalChannelLedgerIncomplete")
        for attempt_id in result.get("historical_attempts", []):
            if not db.execute(
                "SELECT 1 FROM attempts WHERE id=? UNION ALL SELECT 1 FROM join_attempts WHERE id=?",
                (attempt_id, attempt_id),
            ).fetchone():
                raise CloudUnavailable("LocalChannelLedgerIncomplete")
        if (
            job
            and db.execute(
                "SELECT 1 FROM cloud_receipt_outbox WHERE job_id=?", (job["id"],)
            ).fetchone()
        ):
            raise CloudUnavailable("LocalChannelReceiptPending")
    if job:
        account = engine.account(job["account_id"])
        remote_account = result["account"]
        if (
            account["daily_limit"] > remote_account["limits"]["daily"]
            or account["hourly_limit"] > remote_account["limits"]["hourly"]
            or account["min_interval"] < remote_account["min_interval"]
        ):
            raise CloudUnavailable("LocalChannelLimitsDiffer")
    if not job and result.get("offline_permit"):
        # Persist only after the complete local ledger and limits checks above.
        with engine.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO meta VALUES('cloud_standby_permit',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (canonical(result["offline_permit"]),),
            )
    return result


def activate_local(engine, cloud, retry):
    if not enabled(engine.store) or retry.get("reason") != "CloudWriteQuotaExceeded":
        return False
    now = engine.clock()
    # Stop before the provider's daily reset. Never extend takeover on a timer.
    until = min(retry["until"], (int(now // 86400) + 1) * 86400 - 5)
    if until <= now:
        return False
    with engine.store.connect() as db:
        authority = independent_scope(db)
    if authority:
        configure_independent(
            engine, authority["account_ids"], until, source="CloudWriteQuotaExceeded"
        )
        return True
    check_standby(engine, cloud)
    state = {
        "active": "local",
        "reason": retry["reason"],
        "since": now,
        "until": until,
        "node_id": cloud.node_id,
        "store_id": binding(engine),
        "destination": hashlib.sha256(cloud.url.encode()).hexdigest(),
        "generation": uuid.uuid4().hex,
    }
    with engine.store.connect(write=True) as db:
        db.execute(
            "INSERT INTO meta VALUES('execution_channel',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (canonical(state),),
        )
        Store.audit(db, "execution.channel_changed", state, now=now)
    return True


def local_active(engine, cloud):
    state = read_state(engine.store)
    if state.get("active") != "local":
        return False
    if state.get("authorization") == "local_queue":
        with engine.store.connect() as db:
            authority = independent_scope(db)
        if (
            not enabled(engine.store)
            or not authority
            or state.get("store_id") != binding(engine)
            or sorted(state.get("account_ids", [])) != authority["account_ids"]
        ):
            raise CloudUnavailable("LocalChannelBindingMismatch")
        return state["until"] > engine.clock()
    if not cloud or not enabled(engine.store):
        raise CloudUnavailable("LocalChannelBindingMismatch")
    if (
        state["node_id"] != cloud.node_id
        or state["store_id"] != binding(engine)
        or state["destination"] != hashlib.sha256(cloud.url.encode()).hexdigest()
    ):
        raise CloudUnavailable("LocalChannelBindingMismatch")
    return state["until"] > engine.clock()


def use_cloud(engine):
    previous = read_state(engine.store)
    if previous.get("active") == "cloud":
        return
    state = {"active": "cloud", "since": engine.clock(), "reason": "cloud_reconciled"}
    with engine.store.connect(write=True) as db:
        db.execute("UPDATE meta SET value=? WHERE key='execution_channel'", (canonical(state),))
        Store.audit(db, "execution.channel_changed", state, now=engine.clock())


def guard_local(engine, context, cloud=None):
    if context and context.get("task_channel"):
        from .task_channels import guard_context

        guard_context(engine, context)
        return
    with engine.store.connect() as db:
        if not local_context_valid(db, context, now=engine.clock()):
            raise CloudUnavailable("LocalChannelClaimExpired")
    independent = independent_active(engine)
    cloud = None if independent else cloud or CloudControl.configured()
    if not local_active(engine, cloud):
        raise CloudUnavailable("LocalChannelClaimExpired")
    job = engine.store.one("SELECT * FROM execution_jobs WHERE id=?", (context["id"],))
    if (
        not job
        or job["owner"] != context.get("owner")
        or job["state"] != "leased"
        or job["lease_until"] <= engine.clock()
    ):
        raise CloudUnavailable("PersistentJobClaimExpired")
    if independent:
        state = read_state(engine.store)
        if job["account_id"] not in state["account_ids"]:
            raise CloudUnavailable("LocalChannelAccountNotAllowed")
        if engine.store.one("SELECT job_id FROM cloud_receipt_outbox WHERE job_id=?", (job["id"],)):
            raise CloudUnavailable("LocalChannelReceiptPending")
    else:
        check_standby(engine, cloud, job)
