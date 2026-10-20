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
    engine, account_ids, until, *, source="explicit_user_local", through_task_ids=None
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
