"""Read-only adapters for completion evidence across the user's existing workspaces."""

import json
import math
from datetime import datetime
from pathlib import Path

INTERACTION_ACTIONS = {
    "reply",
    "comment",
    "manual_comment",
    "like",
    "repost",
    "quote",
    "send_message",
    "send_file",
    "schedule_message",
    "schedule_file",
}
UNCERTAIN = {"sending", "submitting", "executing", "unknown", "pending_review", "scheduled"}
ENDED = {"completed", "cancelled", "canceled", "stopped", "stopped_by_user", "user_stopped"}
ITEM_ENDED = {
    "sent",
    "confirmed",
    "public_verified",
    "skipped",
    "cancelled",
    "canceled",
    "failed_known",
    "completed",
    "not_applicable",
    "expired",
    "no_match",
}


def timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) and value > 0 else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.timestamp() if parsed.tzinfo is not None else None
        except ValueError:
            pass
    return None


def external_evidence(data, *, account_label, now):
    """Unknown overrides an old batch's stop; finite follow-up observation remains busy."""
    if not isinstance(data, dict):
        return "unknown", None
    items = data.get("items", [])
    if isinstance(items, dict):
        items = list(items.values())
    items = [
        i for i in items if isinstance(i, dict) and i.get("account", account_label) == account_label
    ]
    inflight = data.get("coordination_inflight") or {}
    if isinstance(inflight, dict) and inflight.get("status") in UNCERTAIN:
        return "unknown", None
    if any(i.get("status") in UNCERTAIN or i.get("followup_status") in UNCERTAIN for i in items):
        return "unknown", None
    overall = data.get("status", data.get("phase", "unknown"))
    if overall in ENDED:
        # The observed bound is conservative when historical completion time is absent.
        at = timestamp(
            data.get("completed_at") or data.get("stopped_at") or data.get("user_stopped_at")
        )
        return ("stopped" if "stop" in overall else "complete"), min(at or now, now)
    if overall in {
        "observing_replies",
        "reply_observation",
        "running",
        "active",
        "executing",
        "paused",
        "waiting",
        "scheduled",
    }:
        return "busy", None
    if not items or any(
        i.get("status") not in ITEM_ENDED
        or i.get("followup_status") in {"pending", "awaiting_response", "watching", "scheduled"}
        for i in items
    ):
        return "busy", None
    at = max(
        [
            timestamp(data.get("last_sent_at") or data.get("last_outbound_at")) or 0,
            *[timestamp(i.get("verified_at") or i.get("sent_at")) or 0 for i in items],
        ]
    )
    return "complete", min(at or now, now)


def refresh_sources(cycles):
    """Enroll every real native account, and read registered external state files only."""
    store, now = cycles.store, cycles.clock()
    for account in store.rows("SELECT id,platform,label FROM accounts WHERE mode='live'"):
        meta = store.one(
            "SELECT external_id,details FROM meta_browser_accounts WHERE account_id=?",
            (account["id"],),
        )
        cycles.enroll(
            meta["external_id"] if meta else account["id"],
            account["platform"],
            account["label"],
            details={
                **(json.loads(meta["details"]) if meta else {}),
                "native_account_id": account["id"],
                "connector": "auto_promotion",
            },
        )
    for account in store.rows("SELECT * FROM completion_cycle_accounts"):
        details = json.loads(account["details"])
        native = details.get("native_account_id")
        if native:
            linked = {
                r["task_id"]
                for r in store.rows(
                    "SELECT task_id FROM completion_cycle_candidates WHERE account_id=? AND task_id IS NOT NULL",
                    (account["id"],),
                )
            }
            items = store.rows(
                "SELECT i.*,t.state task_state,t.updated_at task_updated,t.policy FROM items i "
                "JOIN tasks t ON t.id=i.task_id WHERE i.account_id=? AND t.state<>'draft' "
                "AND NOT EXISTS(SELECT 1 FROM meta_browser_history h WHERE h.item_id=i.id)",
                (native,),
            )
            relevant = [
                i
                for i in items
                if i["task_id"] not in linked
                and json.loads(i["payload"]).get("action") in INTERACTION_ACTIONS
            ]
            uncertain = any(i["status"] == "unknown" for i in relevant)
            busy = any(
                i["status"] in {"executing", "scheduled", "manual_attention"}
                or (
                    i["task_state"] in {"active", "paused"}
                    and i["status"] not in {"confirmed", "failed_known", "missed", "cancelled"}
                )
                for i in relevant
            )
            # A join-only task can still be outstanding without any outbound items.
            for task in store.rows("SELECT * FROM tasks WHERE state IN ('active','paused')"):
                if (
                    task["id"] not in linked
                    and native in json.loads(task["account_ids"])
                    and json.loads(task["policy"]).get("max_joins")
                ):
                    busy = True
            old = store.one(
                "SELECT * FROM completion_cycle_workflows WHERE account_id=? AND reference='native-ledger'",
                (account["id"],),
            )
            completed = max(
                [
                    account["enrolled_at"],
                    *[i["task_updated"] for i in relevant],
                    (old or {}).get("completed_at") or 0,
                ]
            )
            cycles.observe(
                account["id"],
                "native-ledger",
                "unknown" if uncertain else "busy" if busy else "complete",
                completed_at=None if uncertain or busy else min(completed, now),
                evidence=json.dumps(
                    {
                        "source": "native tasks/items; original publications excluded",
                        "task_ids": sorted({i["task_id"] for i in relevant}),
                        "uncertain_task_ids": sorted(
                            {
                                i["task_id"]
                                for i in relevant
                                if i["status"]
                                in {"unknown", "executing", "scheduled", "manual_attention"}
                            }
                        ),
                    }
                ),
            )
        for raw in details.get("workflow_paths", []):
            path = Path(raw)
            reference = str(path.resolve())
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                state, completed = external_evidence(
                    data, account_label=details.get("scope", account["label"]), now=now
                )
                old = store.one(
                    "SELECT * FROM completion_cycle_workflows WHERE account_id=? AND reference=?",
                    (account["id"], reference),
                )
                # Missing old timestamps must not resample the clock on each poll.
                if (
                    state in {"complete", "stopped"}
                    and old
                    and old["state"] == state
                    and completed == now
                ):
                    completed = old["completed_at"]
                cycles.observe(
                    account["id"],
                    reference,
                    state,
                    completed_at=completed,
                    evidence="read-only workflow state: " + reference,
                )
            except (OSError, ValueError, TypeError):
                cycles.observe(
                    account["id"],
                    reference,
                    "unknown",
                    evidence="external_state_unreadable:" + reference,
                )
    return cycles.status()
