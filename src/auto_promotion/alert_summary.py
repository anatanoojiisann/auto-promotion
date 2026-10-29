"""One compact platform summary per three-hour boundary; no revision pushes."""

import json
from collections import Counter
from datetime import datetime

from .alert_templates import PLATFORMS, ZONE

SUMMARY_POLICY = "three_hour_counts_v1"
SUMMARY_FORMAT = "platform_counts_planned_queue_v2"
WINDOW_SECONDS = 3 * 3600
# A missed boundary remains eligible until the next boundary, not just five
# minutes. Only the latest window is ever queued, so recovery cannot fan out.
MAX_LATENESS = WINDOW_SECONDS
ON_TIME_GRACE = 300
# Reporting vocabulary only. This does not add a private-message sender.
TIKTOK_DM_ACTIONS = {"direct_message", "send_direct_message", "reply_direct_message"}
DELIVERY_ACTIONS = {
    "send_message",
    "send_file",
    "schedule_message",
    "schedule_file",
    "reply",
    "post",
    "publish",
    "manual_comment",
    "comment",
    "quote",
    "repost",
}


def is_tiktok_direct_message(row):
    """Public replies and recipient IDs alone never establish a private message."""
    return row["platform"] == "tiktok" and (
        row["action"] in TIKTOK_DM_ACTIONS
        or (
            row["interaction_channel"] == "direct_message"
            and row["action"] in {"send_message", "send_file", "reply"}
        )
    )


def summary_cutoff(now):
    local = datetime.fromtimestamp(now, ZONE)
    return int(
        local.replace(hour=local.hour // 3 * 3, minute=0, second=0, microsecond=0).timestamp()
    )


def collect_queued(db, observed_at):
    """Count real queued outbound items, once each, without touching the queue."""
    counts = Counter()
    rows = db.execute(
        "SELECT DISTINCT i.id,a.platform,json_extract(i.payload,'$.action') action,"
        "json_extract(i.payload,'$.interaction_channel') interaction_channel "
        "FROM execution_jobs j JOIN items i ON i.id=j.reference_id "
        "JOIN accounts a ON a.id=i.account_id JOIN tasks t ON t.id=i.task_id "
        "WHERE j.kind='send' AND j.state IN ('pending','leased') "
        "AND j.account_id=i.account_id AND j.task_id=i.task_id "
        "AND j.expires_at>? AND a.mode='live' AND t.mode='live' "
        "AND t.state='active' AND t.phase='ready' "
        "AND i.status='not_attempted' AND i.attempt_id IS NULL "
        "AND NOT EXISTS(SELECT 1 FROM attempts at WHERE at.item_id=i.id)",
        (observed_at,),
    )
    for row in rows:
        if row["platform"] in PLATFORMS and (
            row["action"] in DELIVERY_ACTIONS or is_tiktok_direct_message(row)
        ):
            counts[row["platform"]] += 1
    return counts


def collect_summary(db, cutoff, *, queue_at=None):
    """Count definitive outbound results confirmed in this half-open window.

    Pending/unknown outcomes are counted only when they become definitive, so late
    receipts enter a later window instead of generating revised old notifications.
    """
    start = cutoff - WINDOW_SECONDS
    success, failure = Counter(), Counter()
    queue_at = cutoff if queue_at is None else queue_at
    queued = collect_queued(db, queue_at)
    dm_interactions, dm_evidence_seen = 0, False
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tiktok_dm_collections'"
    ).fetchone():
        dm_evidence_seen = (
            db.execute(
                "SELECT 1 FROM tiktok_dm_collections c JOIN accounts a ON a.id=c.account_id "
                "WHERE a.platform='tiktok' AND a.mode='live' AND c.connected_at<? LIMIT 1",
                (cutoff,),
            ).fetchone()
            is not None
        )
    rows = db.execute(
        "SELECT a.platform,i.status,i.result,i.external_reference,"
        "json_extract(i.payload,'$.action') action,"
        "json_extract(i.payload,'$.interaction_channel') interaction_channel,"
        "p.verified_at,at.finished_at "
        "FROM items i JOIN accounts a ON a.id=i.account_id "
        "JOIN tasks t ON t.id=i.task_id "
        "LEFT JOIN publication_evidence p ON p.item_id=i.id "
        "LEFT JOIN attempts at ON at.item_id=i.id AND at.mode='live' "
        "WHERE a.mode='live' AND t.mode='live' AND i.status IN ('confirmed','failed_known')"
    )
    for row in rows:
        platform = row["platform"]
        private_message = is_tiktok_direct_message(row)
        if platform not in PLATFORMS or (
            row["action"] not in DELIVERY_ACTIONS and not private_message
        ):
            continue
        if row["status"] == "confirmed":
            result = json.loads(row["result"])
            reference = row["external_reference"]
            if (
                result.get("verified") is not True
                or not reference
                or reference.startswith("simulation://")
            ):
                continue
            at = row["verified_at"] or row["finished_at"]
            if private_message and at is not None and at < cutoff:
                dm_evidence_seen = True
            if at is not None and start <= at < cutoff:
                success[platform] += 1
                if private_message:
                    dm_interactions += 1
        else:
            at = row["finished_at"]
            if private_message and at is not None and at < cutoff:
                dm_evidence_seen = True
            if at is not None and start <= at < cutoff:
                failure[platform] += 1
    return {
        "since": start,
        "cutoff": cutoff,
        "queue_observed_at": queue_at,
        "platforms": [
            {
                "platform": p,
                "sent": success[p] + failure[p],
                "success": success[p],
                "failure": failure[p],
                "queued": queued[p],
                **(
                    {"direct_message_interactions": dm_interactions if dm_evidence_seen else None}
                    if p == "tiktok"
                    else {}
                ),
            }
            for p in PLATFORMS
        ],
    }


def summary_notice(summary):
    counts = {p["platform"]: p for p in summary["platforms"]}
    lines = []
    for platform, label in PLATFORMS.items():
        p = counts.get(platform, {"sent": 0, "success": 0, "failure": 0})
        line = f"{label}：发送 {p['sent']} 条｜成功 {p['success']} 条｜失败 {p['failure']} 条"
        if platform == "tiktok":
            count = p.get("direct_message_interactions")
            if count is not None and (type(count) is not int or count < 0):
                raise ValueError("InvalidTikTokDirectMessageCount")
            line += "｜私信互动 " + ("未接入" if count is None else f"{count} 条")
        queued = p.get("queued")
        if queued is not None and (type(queued) is not int or queued < 0):
            raise ValueError("InvalidPlatformQueuedCount")
        line += "｜队列中 " + ("未统计" if queued is None else f"{queued} 条")
        lines.append(line)
    return "\n\n".join(lines)


class ThreeHourSummaryRules:
    def __init__(self, store):
        self.store = store

    def observe(self, sample):
        now, summary, s = sample["at"], sample["summary"], self.store
        cutoff = summary_cutoff(now)
        if summary["cutoff"] != cutoff or summary["since"] != cutoff - WINDOW_SECONDS:
            raise ValueError("SummaryWindowMismatch")
        with s.connect(write=True) as db:
            cursor = s.meta(db, "summary_cursor")
            if s.meta(db, "delivery_policy") != SUMMARY_POLICY or cursor is None:
                # A new install/upgrade is quiet. Do not replay elapsed boundaries.
                s.set_meta(db, "delivery_policy", SUMMARY_POLICY)
                s.set_meta(db, "summary_cursor", max(cursor or cutoff, cutoff))
                db.execute(
                    "UPDATE alert_outbox SET state='expired',reason='summary_policy_replaced' WHERE state='pending'"
                )
            elif cutoff > cursor:
                # Resume at the latest boundary only; never fan out historical slots.
                s.set_meta(db, "summary_cursor", cutoff)
                if 0 <= now - cutoff < MAX_LATENESS:
                    s.enqueue(
                        db,
                        f"platform_summary:{cutoff}",
                        "platform_summary",
                        summary_notice(summary),
                        ttl=MAX_LATENESS - (now - cutoff),
                    )
                    if now - cutoff >= ON_TIME_GRACE:
                        s.set_meta(
                            db,
                            "last_recovered_summary",
                            {
                                "cutoff": cutoff,
                                "observed_at": now,
                                "late_seconds": now - cutoff,
                                "reason": "latest_window_recovered",
                            },
                        )
                else:
                    # Record why a boundary was intentionally skipped instead of
                    # silently advancing the cursor after standby or an outage.
                    s.set_meta(
                        db,
                        "last_skipped_summary",
                        {
                            "cutoff": cutoff,
                            "observed_at": now,
                            "late_seconds": now - cutoff,
                            "reason": "outside_delivery_window",
                        },
                    )
            s.set_meta(db, "last_observed_at", now)
            s.set_meta(db, "last_collection_error", None)
            s.set_meta(db, "queue_sources_state", summary.get("queue_sources", []))
            s.set_meta(db, "summary_recovery_policy", "latest_only_until_next_boundary")
            s.set_meta(
                db, "next_summary_at", max(cutoff, s.meta(db, "summary_cursor")) + WINDOW_SECONDS
            )
