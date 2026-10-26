"""Account-scoped X read deferral; no platform calls or retry authorizations."""

import math

from .models import canonical

FIELDS = (
    "read_observation_source",
    "read_page_fingerprint",
    "read_primary_request_count",
    "read_primary_response_count",
    "read_primary_requests_blocked",
    "read_resource_failures",
    "read_cleanup_state",
    "read_request_count_source",
    "live_probe",
)


def x_read_until(db, account):
    if account["platform"] != "x":
        return 0
    row = db.execute(
        "SELECT value FROM meta WHERE key=?", ("hf_x_read_after:" + account["id"],)
    ).fetchone()
    legacy = float(row[0]) if row else 0
    row = db.execute(
        "SELECT MAX(until_at) FROM hf_read_cooldowns WHERE scope_id=?",
        ("x:account:" + account["id"],),
    ).fetchone()
    return max(account["cooldown_until"], legacy, row[0] or 0)


def record_x_read_cooldown(db, account, result, now):
    delay = result.get("retry_after_seconds")
    delay = (
        math.ceil(delay)
        if isinstance(delay, (int, float))
        and not isinstance(delay, bool)
        and math.isfinite(delay)
        and delay > 0
        else 900
    )
    reset_at = result.get("rate_limit_reset_at")
    reset_at = (
        reset_at
        if isinstance(reset_at, (int, float))
        and not isinstance(reset_at, bool)
        and math.isfinite(reset_at)
        else 0
    )
    until = max(x_read_until(db, account), now + delay, reset_at)
    db.execute(
        "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=CAST(MAX(CAST(value AS REAL),CAST(excluded.value AS REAL)) AS TEXT)",
        ("hf_x_read_after:" + account["id"], str(until)),
    )
    db.execute(
        "INSERT INTO hf_read_cooldowns VALUES(?,?,'rate_limited') ON CONFLICT(scope_id) DO UPDATE SET until_at=MAX(until_at,excluded.until_at)",
        ("x:account:" + account["id"], until),
    )
    db.execute(
        "UPDATE accounts SET cooldown_until=MAX(cooldown_until,?) WHERE id=?",
        (until, account["id"]),
    )
    return until


def safe_observation(result):
    from .execution_diagnostics import safe_code
    from .telegram_reading import read_diagnostics

    value = read_diagnostics(result)
    value.pop("read_cursor_next", None)
    for field in FIELDS:
        item = result.get(field)
        if field == "read_resource_failures" and isinstance(item, dict):
            value[field] = {
                safe_code(key, "resource_error"): count
                for key, count in item.items()
                if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 1000000
            }
        elif item is None or isinstance(item, (str, bool, int, float)):
            value[field] = item
    if result.get("reason"):
        value["reason"] = safe_code(result["reason"])
    for field in (
        "cooldown_source",
        "account_retry_at",
        "retry_after_seconds",
        "runtime_git_commit_at_start",
        "runtime_source_fingerprint",
    ):
        if field in result:
            value[field] = result[field]
    return value


def observe_x_read(store, account_id, task_id, operation, result, now):
    with store.connect(write=True) as db:
        db.execute(
            "INSERT INTO execution_read_observations(account_id,task_id,operation,at,result) VALUES(?,?,?,?,?)",
            (account_id, task_id, operation, now, canonical(safe_observation(result))),
        )
        db.execute(
            "DELETE FROM execution_read_observations WHERE account_id=? AND id NOT IN "
            "(SELECT id FROM execution_read_observations WHERE account_id=? ORDER BY id DESC LIMIT 1000)",
            (account_id, account_id),
        )
