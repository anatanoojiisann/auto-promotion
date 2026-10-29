"""Explicitly acknowledge one legacy unknown without changing its outcome.

The acknowledgment applies only to the named local task scope and target
events. It never changes the legacy source, item, attempt, contact reservation,
account limits or stop state.
"""

import json
import math
import time
from pathlib import Path

from .completion_sources import UNCERTAIN
from .models import canonical, digest
from .store import Store


def _source_unchanged(source, account_label, expected_external):
    try:
        data = json.loads(Path(source["path"]).read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or digest(data) != source["fingerprint"]:
            return False
        if data.get("status") not in {
            "stopped",
            "stopped_by_user",
            "user_stopped",
            "cancelled",
            "canceled",
        }:
            return False
        inflight = data.get("coordination_inflight") or {}
        if isinstance(inflight, dict) and inflight.get("status") in UNCERTAIN:
            return False
        items = data.get("items", [])
        if isinstance(items, dict):
            items = list(items.values())
        uncertain = [
            str(x.get("id", ""))
            for x in items
            if isinstance(x, dict)
            and x.get("account", account_label) == account_label
            and (x.get("status") in UNCERTAIN or x.get("followup_status") in UNCERTAIN)
        ]
        return uncertain == [expected_external]
    except (OSError, ValueError, TypeError, KeyError):
        return False


def record_legacy_skip(store, item_id, authorization, targets, task_name_prefix, *, now=None):
    """Record the user's scoped skip; the original publication stays unknown."""
    from .meta_browser import event_key

    if not isinstance(authorization, str) or not authorization.strip() or len(authorization) > 2000:
        raise ValueError("ExplicitSkipInstructionRequired")
    if (
        not isinstance(task_name_prefix, str)
        or not task_name_prefix.strip()
        or len(task_name_prefix) > 120
    ):
        raise ValueError("ExplicitTaskScopeRequired")
    if not isinstance(targets, list) or not 1 <= len(targets) <= 50:
        raise ValueError("ExplicitTargetScopeRequired")
    now = time.time() if now is None else now
    with store.connect(write=True) as db:
        row = db.execute(
            "SELECT i.id,i.account_id,i.status,i.payload_hash,i.payload,h.source_id,h.external_id,h.details "
            "FROM items i JOIN meta_browser_history h ON h.item_id=i.id WHERE i.id=?",
            (item_id,),
        ).fetchone()
        if not row or row["status"] != "unknown":
            raise ValueError("LegacyUnknownRequired")
        history = json.loads(row["details"])
        source = db.execute(
            "SELECT * FROM meta_browser_sources WHERE id=? AND account_id=?",
            (row["source_id"], row["account_id"]),
        ).fetchone()
        account = db.execute("SELECT * FROM accounts WHERE id=?", (row["account_id"],)).fetchone()
        if (
            not source
            or source["state"] != "unknown"
            or not account
            or account["platform"] != "facebook"
        ):
            raise ValueError("StoppedLegacySourceRequired")
        details = json.loads(source["details"])
        if not (
            details.get("ownership") == "legacy"
            and details.get("read_only") is True
            and details.get("legacy_status")
            in {"stopped", "stopped_by_user", "user_stopped", "cancelled", "canceled"}
            and not details.get("native_item_id")
            and history.get("attempted") is True
            and _source_unchanged(source, account["label"], row["external_id"])
        ):
            raise ValueError("LegacySourceStillActiveOrChanged")
        old_target = json.loads(row["payload"])["target"]
        old_event = event_key(account["platform"], old_target)
        contact = db.execute(
            "SELECT item_id,state FROM meta_browser_contacts WHERE platform=? AND event_key=?",
            (account["platform"], old_event),
        ).fetchone()
        if not contact or contact["item_id"] != item_id:
            raise ValueError("LegacyTargetReservationMissing")
        allowed = {}
        for target in targets:
            if not isinstance(target, dict) or not isinstance(target.get("target"), str):
                raise ValueError("TargetDeadlineRequired")
            deadline = target.get("not_after")
            if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= now:
                raise ValueError("TargetDeadlineRequired")
            key = event_key(account["platform"], target["target"])
            allowed[key] = min(allowed.get(key, deadline), deadline)
        if old_event in allowed:
            raise ValueError("LegacyTargetCannotBeRetried")
        if (
            db.execute(
                "SELECT 1 FROM execution_jobs WHERE account_id=? AND state='leased' AND lease_until>?",
                (account["id"], now),
            ).fetchone()
            or db.execute(
                "SELECT 1 FROM items WHERE account_id=? AND status='executing'", (account["id"],)
            ).fetchone()
        ):
            raise ValueError("AccountHasActiveWork")
        marker = {
            "version": 1,
            "account_id": account["id"],
            "source_id": source["id"],
            "source_fingerprint": source["fingerprint"],
            "item_id": item_id,
            "external_id": row["external_id"],
            "payload_hash": row["payload_hash"],
            "old_event": old_event,
            "allowed_events": allowed,
            "task_name_prefix": task_name_prefix,
            "authorization": authorization.strip(),
            "acknowledged_at": now,
            "outcome": "unknown",
            "old_target_retry_allowed": False,
        }
        key = "meta_legacy_skip:" + source["id"]
        existing = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if existing:
            previous = json.loads(existing["value"])
            comparable = {k: v for k, v in marker.items() if k != "acknowledged_at"}
            if {k: v for k, v in previous.items() if k != "acknowledged_at"} != comparable:
                raise ValueError("ExistingSkipScopeDiffers")
            return previous
        db.execute("INSERT INTO meta(key,value) VALUES(?,?)", (key, canonical(marker)))
        Store.audit(
            db,
            "meta_browser.legacy_unknown_user_skipped",
            {
                "account_id": account["id"],
                "source_id": source["id"],
                "item_id": item_id,
                "source_fingerprint": source["fingerprint"],
                "scope_count": len(allowed),
                "outcome": "unknown",
                "target_retained": True,
                "authorization": authorization.strip(),
            },
            now=now,
        )
        return marker


def legacy_skip_item(db, source, account, payload, item_id, now):
    """Return the acknowledged old item only when every scope binding matches."""
    from .meta_browser import event_key

    try:
        saved = db.execute(
            "SELECT value FROM meta WHERE key=?", ("meta_legacy_skip:" + source["id"],)
        ).fetchone()
        if not saved:
            return None
        marker = json.loads(saved["value"])
        if not (
            marker.get("version") == 1
            and marker.get("account_id") == account["id"]
            and marker.get("source_id") == source["id"]
            and marker.get("source_fingerprint") == source["fingerprint"]
            and marker.get("outcome") == "unknown"
            and marker.get("old_target_retry_allowed") is False
            and isinstance(marker.get("authorization"), str)
            and marker["authorization"].strip()
        ):
            return None
        source_details = json.loads(source["details"])
        if not (
            source_details.get("ownership") == "legacy"
            and source_details.get("read_only") is True
            and source_details.get("legacy_status")
            in {"stopped", "stopped_by_user", "user_stopped", "cancelled", "canceled"}
            and not source_details.get("native_item_id")
        ):
            return None
        new_event = event_key(account["platform"], payload["target"])
        allowed = marker.get("allowed_events")
        deadline = allowed.get(new_event) if isinstance(allowed, dict) else None
        if (
            new_event == marker.get("old_event")
            or type(deadline) not in (int, float)
            or not math.isfinite(deadline)
            or now >= deadline
        ):
            return None
        current = db.execute(
            "SELECT i.account_id,t.name,t.policy FROM items i JOIN tasks t ON t.id=i.task_id WHERE i.id=?",
            (item_id,),
        ).fetchone()
        if (
            not current
            or current["account_id"] != account["id"]
            or not current["name"].startswith(marker["task_name_prefix"])
        ):
            return None
        if json.loads(current["policy"]).get("execution_channel") != "local":
            return None
        old = db.execute(
            "SELECT i.status,i.payload_hash,h.external_id,h.details FROM items i "
            "JOIN meta_browser_history h ON h.item_id=i.id WHERE i.id=? AND h.source_id=?",
            (marker["item_id"], source["id"]),
        ).fetchone()
        if (
            not old
            or old["status"] != "unknown"
            or old["payload_hash"] != marker["payload_hash"]
            or old["external_id"] != marker["external_id"]
        ):
            return None
        if json.loads(old["details"]).get("attempted") is not True:
            return None
        if not _source_unchanged(source, account["label"], marker["external_id"]):
            return None
        uncertain = db.execute(
            "SELECT i.id FROM meta_browser_history h JOIN items i ON i.id=h.item_id "
            "WHERE h.source_id=? AND i.status IN ('unknown','executing','scheduled')",
            (source["id"],),
        ).fetchall()
        if len(uncertain) != 1 or uncertain[0]["id"] != marker["item_id"]:
            return None
        return marker["item_id"]
    except (OSError, ValueError, TypeError, KeyError):
        return None
