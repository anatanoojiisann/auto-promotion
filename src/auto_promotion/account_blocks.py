"""Durable account outbound stops, independent of once-only item outcomes."""

from __future__ import annotations

import json
import re
import uuid

from .models import canonical, digest
from .store import Store

FIELDS = (
    "account_block_reason",
    "account_block_since",
    "account_block_until",
    "account_block_generation",
    "account_block_event_id",
    "account_block_cached",
    "account_restriction",
    "failure_class",
    "browser_page_anomaly",
    "confirmed_not_submitted",
    "confirmed_rejected",
    "rpc_counts",
    "rpc_count_source",
    "cooldown_source",
    "disconnect_error",
    "guard_persistence_failed",
    "runtime_git_commit_at_start",
    "runtime_source_fingerprint",
    "runtime_started_at",
)
MANUAL_REASONS = {
    "x_challenge",
    "x_login_required",
    "x_account_restricted",
    "telegram_speaking_restricted",
    "account_guard_unavailable",
}

LEGACY_X_REASONS = {
    "BrowserVerificationRequired": "x_challenge",
    "XChallengeRequired": "x_challenge",
    "CaptchaRequired": "x_challenge",
    "ChallengeRequired": "x_challenge",
    "BrowserLoginRequired": "x_login_required",
    "BrowserAccountRestricted": "x_account_restricted",
}


def normalize_evidence(result, platform):
    """Older providers may return only a reason; it still denotes a durable stop."""
    if platform == "x" and not result.get("account_block_reason"):
        reason = LEGACY_X_REASONS.get(result.get("reason"))
        if reason:
            result["account_block_reason"] = reason
    return result


def blocked_health(block):
    """A normal identity probe cannot certify that a manual stop was resolved."""
    reason = {
        "x_challenge": "BrowserVerificationRequired",
        "x_login_required": "BrowserLoginRequired",
        "x_account_restricted": "BrowserAccountRestricted",
    }.get(block["reason"], "AccountOutboundBlocked")
    return {
        "healthy": False,
        "service_ready": True,
        "identity_verified": False,
        "permissions": [],
        "reason": reason,
        "account_block_reason": block["reason"],
        "account_block_since": block["since"],
        "account_block_until": block["until"],
        "account_block_generation": block["generation"],
        "account_block_cached": True,
    }


def active_block(db, account_id, now):
    row = db.execute("SELECT * FROM account_blocks WHERE account_id=?", (account_id,)).fetchone()
    if (
        not row
        or row["state"] != "active"
        or (row["until_at"] is not None and row["until_at"] <= now)
    ):
        return None
    tasks = [
        r[0]
        for r in db.execute(
            "SELECT DISTINCT task_id FROM items WHERE account_id=? AND status IN ('not_attempted','executing','unknown')",
            (account_id,),
        )
    ]
    details = json.loads(row["details"])
    tasks = sorted(set(tasks) | set(details.get("affected_tasks", [])))
    return {
        "reason": row["reason"],
        "since": row["since"],
        "until": row["until_at"],
        "generation": row["generation"],
        "evidence": details,
        "affected_tasks": tasks,
        "health_checked_at": row["health_checked_at"],
        "health_ok": bool(row["health_ok"]),
    }


def observe(db, account_id, result, *, now, operation, payload=None, task_id=None):
    """Call inside the account lane, before releasing it; no external calls here."""
    payload = payload or {}
    if result.get("account_block_cached") is True:
        # A local stop view is not a new platform observation. In particular it
        # must not invalidate a fresh, explicit recovery probe or replay a stop
        # which was already cleared.
        return active_block(db, account_id, now)
    if result.get("reason") in LEGACY_X_REASONS:
        account = db.execute("SELECT platform FROM accounts WHERE id=?", (account_id,)).fetchone()
        normalize_evidence(result, account[0] if account else None)
    reason = result.get("account_block_reason")
    if reason not in MANUAL_REASONS and reason != "browser_page_anomaly":
        reason = None
    anomaly = operation == "execute" and result.get("browser_page_anomaly") is True
    restricted = (
        operation == "execute"
        and result.get("status") == "failed_known"
        and not (result.get("message_ids") or result.get("external_reference"))
        and result.get("account_restriction") == "telegram_speaking_restricted"
    )
    if restricted or anomaly:
        kind = "telegram_speaking_restricted" if restricted else "browser_page_anomaly"
        key = digest([account_id, kind, payload.get("item_id"), payload.get("attempt_id")])
        target = digest(str((payload.get("payload") or {}).get("target", "")))
        db.execute("DELETE FROM account_block_events WHERE at<?", (now - 86400,))
        db.execute(
            "INSERT OR IGNORE INTO account_block_events VALUES(?,?,?,?,?,?)",
            (key, account_id, kind, now, target, task_id),
        )
        count = db.execute(
            "SELECT count(DISTINCT target_key) n FROM account_block_events WHERE account_id=? AND reason=? AND at>=?"
            if restricted
            else "SELECT count(*) n FROM account_block_events WHERE account_id=? AND reason=? AND at>=?",
            (account_id, kind, now - 900),
        ).fetchone()[0]
        if count >= (2 if restricted else 3):
            reason = kind
    elif operation == "execute" and result.get("status") in {"confirmed", "scheduled"}:
        db.execute("DELETE FROM account_block_events WHERE account_id=?", (account_id,))
    new_evidence = False
    if reason:
        event = result.get("account_block_event_id")
        if not isinstance(event, str) or not re.fullmatch(r"[a-f0-9]{32,64}", event):
            event = (
                digest(
                    [
                        account_id,
                        operation,
                        reason,
                        payload.get("item_id"),
                        payload.get("attempt_id"),
                    ]
                )
                if payload.get("item_id")
                else uuid.uuid4().hex
            )
        result["account_block_event_id"] = event
        cursor = db.execute(
            "INSERT OR IGNORE INTO account_block_events VALUES(?,?,?,?,?,?)",
            (event, account_id, "evidence_" + reason, now, "", task_id),
        )
        new_evidence = bool(cursor.rowcount)
    current = active_block(db, account_id, now)
    if reason:
        # A short page circuit must not downgrade a manual intervention stop.
        if current and (
            current["reason"] == reason
            or (
                current["reason"] in MANUAL_REASONS
                and current["reason"] != "account_guard_unavailable"
            )
        ):
            # Fence even the FIRST in-flight recovery probe. It has no saved
            # health_checked_at yet, but its late success must not verify an
            # independent challenge observed after that probe began.
            if new_evidence:
                db.execute(
                    "UPDATE account_blocks SET generation=generation+1,health_ok=0,health_checked_at=NULL,"
                    "health_identity_hash=NULL,updated_at=? WHERE account_id=?",
                    (now, account_id),
                )
                current = active_block(db, account_id, now)
            block = current
        else:
            previous = db.execute(
                "SELECT generation FROM account_blocks WHERE account_id=?", (account_id,)
            ).fetchone()
            generation = previous[0] + 1 if previous else 1
            until = now + 900 if reason == "browser_page_anomaly" else None
            details = {
                "source": "platform_response" if restricted else "browser_guard",
                "stage": str(result.get("execution_step", "unknown"))[:80],
            }
            code = result.get("reason")
            if isinstance(code, str) and code.isidentifier() and len(code) <= 80:
                details["error_code"] = code
            db.execute(
                "INSERT INTO account_blocks VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET "
                "state='active',reason=excluded.reason,since=excluded.since,until_at=excluded.until_at,"
                "generation=excluded.generation,details=excluded.details,health_checked_at=NULL,health_ok=0,health_identity_hash=NULL,updated_at=excluded.updated_at",
                (
                    account_id,
                    "active",
                    reason,
                    now,
                    until,
                    generation,
                    canonical(details),
                    None,
                    0,
                    None,
                    now,
                ),
            )
            Store.audit(
                db,
                "account.outbound_blocked",
                {
                    "account_id": account_id,
                    "reason": reason,
                    "generation": generation,
                    "until": until,
                },
                task_id,
                now,
            )
            block = active_block(db, account_id, now)
        result.update(
            account_block_reason=block["reason"],
            account_block_since=block["since"],
            account_block_until=block["until"],
            account_block_generation=block["generation"],
        )
        return block
    return current


def private_stop_path(platform, label, *, accounts_dir=None):
    from .backends.common import profile_path

    return profile_path(platform, label, accounts_dir=accounts_dir) / "outbound-stops"


def _sync_directory(path):
    import os

    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def save_private_stop(platform, label, result):
    """Immutable per-event files prevent an older recovery deleting newer evidence."""
    import os
    import time

    root = private_stop_path(platform, label)
    root.mkdir(parents=True, exist_ok=True)
    event = result.get("account_block_event_id")
    if not isinstance(event, str) or not re.fullmatch(r"[a-f0-9]{32,64}", event):
        event = uuid.uuid4().hex
        result["account_block_event_id"] = event
    path = root / (event + ".json")
    safe = {key: result[key] for key in FIELDS + ("reason", "execution_step") if key in result}
    value = {"event": event, "observed_at": time.time(), "result": safe}
    staging = root / (event + "." + uuid.uuid4().hex + ".tmp")
    try:
        with staging.open("x", encoding="utf-8") as handle:
            handle.write(canonical(value))
            handle.flush()
            os.fsync(handle.fileno())
        staging.chmod(0o600)
        staging.replace(path)
        _sync_directory(root)
    finally:
        staging.unlink(missing_ok=True)
    return event


def read_private_stop(platform, label, *, accounts_dir=None):
    root = private_stop_path(platform, label, accounts_dir=accounts_dir)
    try:
        paths = sorted(root.iterdir())
    except FileNotFoundError:
        return None
    except OSError:
        return {
            "event": "unreadable",
            "result": {"account_block_reason": "account_guard_unavailable"},
        }
    for path in paths:
        if re.fullmatch(r"[a-f0-9]{32,64}\.[a-f0-9]{32}\.tmp", path.name):
            # A killed writer can leave a partial or fsynced staging record.
            # Its missing rename is not evidence that the account is writable.
            return {
                "event": "unreadable",
                "result": {"account_block_reason": "account_guard_unavailable"},
            }
        if path.suffix != ".json":
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(value, dict)
                or not isinstance(value.get("result"), dict)
                or not re.fullmatch(r"[a-f0-9]{32,64}", str(value.get("event", "")))
                or path.name != value["event"] + ".json"
            ):
                raise ValueError
            return value
        except FileNotFoundError:
            continue  # Another recovery consumed this exact event.
        except (OSError, ValueError, TypeError):
            return {
                "event": "unreadable",
                "result": {"account_block_reason": "account_guard_unavailable"},
            }
    return None


def clear_private_stop(platform, label, event, *, accounts_dir=None):
    if not isinstance(event, str) or not re.fullmatch(r"[a-f0-9]{32,64}", event):
        return
    root = private_stop_path(platform, label, accounts_dir=accounts_dir)
    try:
        (root / (event + ".json")).unlink()
        _sync_directory(root)
    except FileNotFoundError:
        pass


def recover_private_stop(store, account, *, now):
    """Replay only restriction evidence, never a submission or task item."""
    import os
    from pathlib import Path

    if account["mode"] != "live":
        return
    root = os.getenv("AP_ACCOUNTS_DIR", str(Path(store.path).parent / "accounts"))
    while value := read_private_stop(
        account["platform"], account["backend_label"], accounts_dir=root
    ):
        result = dict(value["result"])
        # A shared DB failure must never turn a lost guard event into permission.
        reason = result.get("account_block_reason")
        if reason not in MANUAL_REASONS:
            reason = (
                "browser_page_anomaly"
                if result.get("browser_page_anomaly")
                else "account_guard_unavailable"
            )
        result["account_block_reason"] = reason
        with store.connect(write=True) as db:
            observe(db, account["id"], result, now=now, operation="recovery")
        clear_private_stop(
            account["platform"], account["backend_label"], value["event"], accounts_dir=root
        )
        if value["event"] == "unreadable":
            break


def merge_remote_block(db, account_id, value, *, now):
    """Merge authenticated control-plane evidence without granting local recovery."""
    import math

    if not isinstance(value, dict):
        return False
    reason, state, generation = value.get("reason"), value.get("state"), value.get("generation")
    if (
        reason not in MANUAL_REASONS | {"browser_page_anomaly"}
        or state not in {"active", "cleared"}
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 1
    ):
        return False
    since = value.get("since")
    until = value.get("until")
    if not isinstance(since, (float, int)) or isinstance(since, bool) or not math.isfinite(since):
        return False
    if reason in MANUAL_REASONS:
        until = None
    elif (
        not isinstance(until, (float, int))
        or isinstance(until, bool)
        or not math.isfinite(until)
        or not since <= until <= since + 900
    ):
        return False
    current = db.execute(
        "SELECT * FROM account_blocks WHERE account_id=?", (account_id,)
    ).fetchone()
    if current and generation <= current["generation"]:
        return False
    if state == "cleared":
        # A remote clear is useful synchronization evidence, but cannot clear an
        # existing local stop without this device's fresh identity probe.
        if current and current["state"] == "active":
            return False
        if value.get("health_ok") is not True:
            return False
    tasks = value.get("affected_tasks", [])
    tasks = (
        [
            task
            for task in tasks[:1000]
            if isinstance(task, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", task)
        ]
        if isinstance(tasks, list)
        else []
    )
    details = {"source": "cloud_control", "stage": "remote_observation", "affected_tasks": tasks}
    db.execute(
        "INSERT INTO account_blocks VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET "
        "state=excluded.state,reason=excluded.reason,since=excluded.since,until_at=excluded.until_at,"
        "generation=excluded.generation,details=excluded.details,health_checked_at=NULL,health_ok=0,health_identity_hash=NULL,updated_at=excluded.updated_at",
        (
            account_id,
            state,
            reason,
            since,
            until,
            generation,
            canonical(details),
            None,
            0,
            None,
            now,
        ),
    )
    Store.audit(
        db,
        "account.outbound_blocked" if state == "active" else "account.remote_clear_observed",
        {
            "account_id": account_id,
            "reason": reason,
            "generation": generation,
            "source": "cloud_control",
        },
        now=now,
    )
    return True
