"""Local-only X account-daily opt-out; other quotas remain in force."""

import json

from .store import Store

KEY = "x_local_account_daily_unlimited_v1"


def enabled(db, platform):
    if platform != "x":
        return False
    row = db.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()
    return bool(row and row[0] == "1")


def item_uses_local_channel(db, item_id, account_id, now):
    """Accept only a current local claim or a scoped independent local route."""
    claim = db.execute(
        "SELECT c.channel FROM execution_jobs j JOIN task_channel_claims c ON c.job_id=j.id "
        "JOIN account_task_channels a ON a.account_id=j.account_id "
        "WHERE j.kind='send' AND j.reference_id=? AND j.account_id=? AND j.state='leased' "
        "AND j.lease_until>? AND c.owner=j.owner AND a.state='ready' AND a.until_at>? "
        "AND c.generation=a.generation AND c.channel=a.channel",
        (item_id, account_id, now, now),
    ).fetchone()
    if claim:
        return claim[0] == "local"
    if db.execute(
        "SELECT 1 FROM account_task_channels WHERE account_id=?", (account_id,)
    ).fetchone():
        return False
    raw = db.execute("SELECT value FROM meta WHERE key='execution_channel'").fetchone()
    item = db.execute("SELECT task_id FROM items WHERE id=?", (item_id,)).fetchone()
    try:
        state = json.loads(raw[0]) if raw else {}
    except (TypeError, ValueError):
        return False
    if (
        not isinstance(state, dict)
        or not isinstance(state.get("until"), (int, float))
        or not isinstance(state.get("account_ids"), list)
        or not isinstance(state.get("through_task_ids", []), list)
    ):
        return False
    tasks = state.get("through_task_ids") or []
    return bool(
        item
        and state.get("active") == "local"
        and state.get("authorization") == "local_queue"
        and state.get("until", 0) > now
        and account_id in state.get("account_ids", [])
        and (not tasks or item[0] in tasks)
    )


def enable(engine):
    """Persist the explicitly requested local opt-out without contacting any server."""
    with engine.store.connect(write=True) as db:
        was_enabled = enabled(db, "x")
        if not was_enabled:
            db.execute(
                "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (KEY, "1"),
            )
            Store.audit(db, "account.local_x_daily_unlimited_enabled", {}, now=engine.clock())
    return {"enabled": True, "platform": "x", "channel": "local", "already_enabled": was_enabled}
