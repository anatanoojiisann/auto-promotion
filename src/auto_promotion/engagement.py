"""Periodic, read-only metrics for our verified publications, with immutable samples."""

from __future__ import annotations

import json
import math
import uuid

from .execution_diagnostics import safe_code
from .models import canonical
from .statistics import REPLYABLE

INTERVAL = 3 * 3600
METRICS = ("views", "likes", "replies", "reposts", "reactions")
SUPPORTED = {"x", "telegram"}


def counter(value):
    return value if type(value) is int and 0 <= value < 2**63 else None


class Engagement:
    def __init__(self, engine):
        self.engine, self.store, self.clock = engine, engine.store, engine.clock

    def publications(self):
        rows = self.store.rows(
            "SELECT i.id item_id,i.task_id,i.account_id,i.payload,i.result,i.external_reference,"
            "i.due_at,a.platform,a.label account_label,t.name task_name,p.published_at FROM items i "
            "JOIN accounts a ON a.id=i.account_id JOIN tasks t ON t.id=i.task_id "
            "LEFT JOIN publication_evidence p ON p.item_id=i.id "
            "WHERE a.mode='live' AND t.mode='live' AND i.status='confirmed' "
            "AND i.external_reference IS NOT NULL ORDER BY COALESCE(p.published_at,i.due_at) DESC,i.id"
        )
        return [
            {**r, "payload": json.loads(r["payload"]), "result": json.loads(r["result"])}
            for r in rows
            if json.loads(r["result"]).get("verified") is True
            and not r["external_reference"].startswith("simulation://")
        ]

    @staticmethod
    def capability(row):
        if row["payload"].get("action") not in REPLYABLE:
            return "not_applicable"
        return "supported" if row["platform"] in SUPPORTED else "unavailable"

    def save(self, row, result):
        now = self.clock()
        verified = (
            result.get("verified") is True
            and result.get("external_reference") == row["external_reference"]
            and result.get("engagement_subject_verified") is True
        )
        observed = result.get("engagement_observed_at")
        if not (
            type(observed) in {int, float}
            and math.isfinite(observed)
            and now - 300 <= observed <= now + 1
        ):
            verified = False
        raw = result.get("engagement_metrics")
        values = {
            k: counter(raw.get(k)) if isinstance(raw, dict) and verified else None for k in METRICS
        }
        status = (
            "complete"
            if all(values[k] is not None for k in ("views", "likes", "replies"))
            else ("partial" if any(v is not None for v in values.values()) else "unavailable")
        )
        raw_estimates = result.get("engagement_estimated")
        estimated = (
            [k for k in raw_estimates if k in METRICS and values[k] is not None]
            if verified and isinstance(raw_estimates, list)
            else []
        )
        reason = (
            safe_code(result.get("reason"), "MetricsUnavailable") if result.get("reason") else None
        )
        if not verified:
            reason = reason or "MetricsSubjectUnverified"
        with self.store.connect(write=True) as db:
            # Collection must never update a receipt, task, attempt, manifest or feedback coverage.
            db.execute(
                "INSERT INTO engagement_snapshots(item_id,observed_at,external_reference,source,status,metrics,estimated,reason) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    row["item_id"],
                    observed if verified else now,
                    row["external_reference"],
                    safe_code(result.get("engagement_source"), "unavailable"),
                    status,
                    canonical(values),
                    canonical(estimated),
                    reason,
                ),
            )
        return {"item_id": row["item_id"], "status": status, "metrics": values, "reason": reason}

    def run_due(self, *, max_items=5):
        if type(max_items) is not int or not 1 <= max_items <= 100:
            raise ValueError("InvalidEngagementLimit")
        now, processed, accounts = self.clock(), [], set()
        polls = {r["item_id"]: r for r in self.store.rows("SELECT * FROM engagement_polls")}
        rows = sorted(
            self.publications(),
            key=lambda r: (polls.get(r["item_id"], {}).get("checked_at", 0), -r["due_at"]),
        )
        for row in rows:
            if len(processed) >= max_items:
                break
            if self.capability(row) != "supported" or row["account_id"] in accounts:
                continue
            poll = polls.get(row["item_id"], {})
            if poll.get("next_at", 0) > now or poll.get("lease_until", 0) > now:
                continue
            account = self.engine.account(row["account_id"])
            paced = self.store.one(
                "SELECT value FROM meta WHERE key=?", ("engagement_read_after:" + account["id"],)
            )
            if paced and float(paced["value"]) > now:
                continue
            if (
                account["disabled"]
                or account["cooldown_until"] > now
                or account.get("outbound_block")
            ):
                continue
            # Sending keeps priority; do not open a browser just before queued work is due.
            if self.store.one(
                "SELECT 1 FROM execution_jobs WHERE account_id=? AND state IN ('pending','running') "
                "AND available_at<=? AND expires_at>? LIMIT 1",
                (account["id"], now + 120, now),
            ):
                continue
            owner = uuid.uuid4().hex
            with self.store.connect(write=True) as db:
                db.execute(
                    "INSERT OR IGNORE INTO engagement_polls(item_id) VALUES(?)", (row["item_id"],)
                )
                acquired = db.execute(
                    "UPDATE engagement_polls SET lease_owner=?,lease_until=? WHERE item_id=? AND next_at<=? AND lease_until<=?",
                    (owner, now + 600, row["item_id"], now, now),
                ).rowcount
            if not acquired:
                continue
            accounts.add(row["account_id"])
            next_at = now + INTERVAL
            account_next_at = now + 60
            try:
                result = self.engine.call(
                    account,
                    "engagement",
                    {
                        "item_id": row["item_id"],
                        "payload": row["payload"],
                        "result": row["result"],
                        "external_reference": row["external_reference"],
                        "read_only": True,
                    },
                    row["task_id"],
                )
                processed.append(self.save(row, result))
                if result.get("verified") is not True:
                    account_next_at = self.clock() + 900
                delay = result.get("retry_after_seconds")
                if type(delay) in {int, float} and math.isfinite(delay):
                    next_at = max(next_at, self.clock() + max(0, delay))
                    account_next_at = max(account_next_at, self.clock() + max(0, delay))
            except Exception as exc:
                code = safe_code(getattr(exc, "safe_code", type(exc).__name__))
                if code == "AccountBusy":
                    next_at = self.clock() + 60
                else:
                    processed.append(self.save(row, {"reason": code}))
                    account_next_at = self.clock() + 900
            finally:
                with self.store.connect(write=True) as db:
                    db.execute(
                        "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=CAST(MAX(CAST(value AS REAL),CAST(excluded.value AS REAL)) AS TEXT)",
                        ("engagement_read_after:" + account["id"], str(account_next_at)),
                    )
                    db.execute(
                        "UPDATE engagement_polls SET checked_at=?,next_at=?,lease_owner=NULL,lease_until=0 "
                        "WHERE item_id=? AND lease_owner=?",
                        (self.clock(), next_at, row["item_id"], owner),
                    )
        return {"processed": processed, "interval_seconds": INTERVAL, "read_only": True}

    def items(self):
        """Build the saved read model without pagination or platform calls."""
        rows = self.publications()
        latest = {}
        for sample in self.store.rows(
            "SELECT * FROM engagement_snapshots ORDER BY observed_at DESC,id DESC"
        ):
            if len(latest.setdefault(sample["item_id"], [])) < 2:
                latest[sample["item_id"]].append(sample)
        polls = {r["item_id"]: r for r in self.store.rows("SELECT * FROM engagement_polls")}
        items = []
        for row in rows:
            samples = latest.get(row["item_id"], [])
            sample = samples[0] if samples else None
            values = json.loads(sample["metrics"]) if sample else {k: None for k in METRICS}
            estimates = json.loads(sample["estimated"]) if sample else []
            previous = json.loads(samples[1]["metrics"]) if len(samples) > 1 else {}
            capability = self.capability(row)
            poll = polls.get(row["item_id"], {})
            items.append(
                {
                    "item_id": row["item_id"],
                    "task_id": row["task_id"],
                    "task_name": row["task_name"],
                    "account_id": row["account_id"],
                    "platform": row["platform"],
                    "account_label": row["account_label"],
                    "text": row["payload"].get("text", ""),
                    "title": row["payload"].get("title", ""),
                    "target": row["payload"].get("target", ""),
                    "action": row["payload"].get("action", ""),
                    "external_reference": row["external_reference"],
                    "published_at": row["published_at"],
                    "capability": capability,
                    "status": sample["status"]
                    if sample
                    else ("pending" if capability == "supported" else capability),
                    "metrics": values,
                    "estimated": estimates,
                    "delta": {
                        k: values[k] - previous[k]
                        if values[k] is not None
                        and previous.get(k) is not None
                        and k not in estimates
                        and k
                        not in (json.loads(samples[1]["estimated"]) if len(samples) > 1 else [])
                        else None
                        for k in METRICS
                    },
                    "observed_at": sample["observed_at"] if sample else None,
                    "previous_observed_at": samples[1]["observed_at"] if len(samples) > 1 else None,
                    "stale": bool(sample and self.clock() - sample["observed_at"] > INTERVAL + 600),
                    "reason": sample["reason"] if sample else None,
                    "next_at": poll.get("next_at"),
                }
            )
        return items

    @staticmethod
    def totals(items):
        totals = {
            k: {"observed_total": 0, "available_items": 0, "estimated_items": 0} for k in METRICS
        }
        for item in items:
            for key in METRICS:
                if item["metrics"][key] is not None:
                    totals[key]["observed_total"] += item["metrics"][key]
                    totals[key]["available_items"] += 1
                    totals[key]["estimated_items"] += key in item["estimated"]
        return totals

    def snapshot(self, *, limit=50, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ValueError("InvalidEngagementPage")
        items = self.items()
        return {
            "items": items[offset : offset + limit],
            "total": len(items),
            "limit": limit,
            "offset": offset,
            "totals": self.totals(items),
            "interval_seconds": INTERVAL,
            "supported_platforms": sorted(SUPPORTED),
            "scope": "our_verified_publications_all_dates",
            "observed_at": self.clock(),
        }

    def history(self, item_id, *, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("InvalidEngagementHistoryLimit")
        return [
            {**r, "metrics": json.loads(r["metrics"]), "estimated": json.loads(r["estimated"])}
            for r in self.store.rows(
                "SELECT * FROM engagement_snapshots WHERE item_id=? ORDER BY observed_at DESC,id DESC LIMIT ?",
                (item_id, limit),
            )
        ]
