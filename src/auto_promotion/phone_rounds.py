"""Opt-in six-app round coordinator. Original tasks remain the only send authority."""

from __future__ import annotations

import json
import re
import uuid

from .core import Conflict, NotFound
from .models import canonical, digest
from .store import Store

PLATFORMS = frozenset({"telegram", "reddit", "instagram", "facebook", "x", "tiktok"})
TOTAL_SECONDS = 600
SUBMIT_SECONDS = 480
SCHEMA = """
CREATE TABLE IF NOT EXISTS phone_rounds(
 id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE, manifest_hash TEXT NOT NULL,
 state TEXT NOT NULL, created_at REAL NOT NULL, started_at REAL, submit_deadline REAL,
 deadline REAL, finished_at REAL, cutoff_snapshot TEXT
);
CREATE TABLE IF NOT EXISTS phone_round_members(
 round_id TEXT NOT NULL REFERENCES phone_rounds(id), task_id TEXT NOT NULL UNIQUE,
 item_id TEXT NOT NULL UNIQUE, platform TEXT NOT NULL, position INTEGER NOT NULL,
 payload_hash TEXT NOT NULL, manifest_hash TEXT NOT NULL, state TEXT NOT NULL,
 start_requested_at REAL, start_lease_until REAL, enqueued_at REAL, error TEXT,
 PRIMARY KEY(round_id,platform)
);
"""


def submission_blocker(db, task_id, now):
    """Called in the original claim/start/final-grant transaction, even if worker is down."""
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='phone_round_members'"
    ).fetchone():
        return None
    row = db.execute(
        "SELECT r.state round_state,r.submit_deadline,m.state member_state,m.payload_hash,"
        "m.manifest_hash,i.payload_hash current_payload,t.manifest_hash current_manifest "
        "FROM phone_round_members m JOIN phone_rounds r ON r.id=m.round_id "
        "JOIN tasks t ON t.id=m.task_id LEFT JOIN items i ON i.id=m.item_id WHERE m.task_id=?",
        (task_id,),
    ).fetchone()
    if not row:
        return None
    if row["round_state"] != "running" or row["member_state"] not in {"starting", "queued"}:
        return "PhoneRoundNotRunning"
    if now >= row["submit_deadline"]:
        return "PhoneRoundSubmissionDeadlineElapsed"
    if (
        row["payload_hash"] != row["current_payload"]
        or row["manifest_hash"] != row["current_manifest"]
    ):
        return "PhoneRoundManifestChanged"
    return None


class PhoneRounds:
    def __init__(self, engine):
        self.engine, self.store, self.clock = engine, engine.store, engine.clock
        with self.store.connect(write=True) as db:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)

    @staticmethod
    def _round(db, round_id):
        row = db.execute("SELECT * FROM phone_rounds WHERE id=?", (round_id,)).fetchone()
        if not row:
            raise NotFound("PhoneRoundNotFound")
        return row

    def create(self, task_ids, request_key):
        if (
            len(task_ids) != 6
            or len(set(task_ids)) != 6
            or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", request_key)
        ):
            raise Conflict("PhoneRoundSixDistinctTasksAndRequestKeyRequired")
        # Sorting is stable across request retries; execution order is explicit platform order.
        manifest = digest(sorted(task_ids))
        now, identifier = self.clock(), "phone-round-" + uuid.uuid4().hex
        with self.store.connect(write=True) as db:
            old = db.execute(
                "SELECT * FROM phone_rounds WHERE request_key=?", (request_key,)
            ).fetchone()
            if old:
                if old["manifest_hash"] != manifest:
                    raise Conflict("PhoneRoundIdempotencyConflict")
                identifier = old["id"]
            else:
                members, platforms = [], set()
                for task_id in task_ids:
                    task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                    if not task or task["state"] != "draft" or not task["manifest_hash"]:
                        raise Conflict("PhoneRoundPreparedDraftRequired")
                    policy = self.engine._integrity(db, task, "outbound")
                    if (
                        policy.get("execution_method") != "phone"
                        or policy.get("execution_channel") != "local"
                    ):
                        raise Conflict("PhoneRoundPhoneTasksRequired")
                    items = db.execute("SELECT * FROM items WHERE task_id=?", (task_id,)).fetchall()
                    if (
                        len(items) != 1
                        or items[0]["status"] != "not_attempted"
                        or items[0]["attempt_id"]
                    ):
                        raise Conflict("PhoneRoundSingleUnattemptedItemRequired")
                    item = items[0]
                    if (
                        json.loads(task["account_ids"]) != [item["account_id"]]
                        or db.execute(
                            "SELECT 1 FROM attempts WHERE item_id=?", (item["id"],)
                        ).fetchone()
                    ):
                        raise Conflict("PhoneRoundSingleUnattemptedItemRequired")
                    account = db.execute(
                        "SELECT * FROM accounts WHERE id=?", (item["account_id"],)
                    ).fetchone()
                    if not account or account["mode"] != "live":
                        raise Conflict("PhoneRoundLiveAccountRequired")
                    if db.execute(
                        "SELECT 1 FROM phone_round_members WHERE task_id=?", (task_id,)
                    ).fetchone():
                        raise Conflict("PhoneRoundTaskAlreadyAssigned")
                    payload = json.loads(item["payload"])
                    actions = {
                        "telegram": "send_message",
                        "reddit": "reply",
                        "x": "reply",
                        "instagram": "comment",
                        "facebook": "comment",
                        "tiktok": "comment",
                    }
                    if payload.get("execution_mode", "automatic") != "automatic" or payload.get(
                        "action"
                    ) != actions.get(account["platform"]):
                        raise Conflict("PhoneRoundAutomaticInteractionRequired")
                    platforms.add(account["platform"])
                    members.append(
                        (
                            task_id,
                            item["id"],
                            account["platform"],
                            item["payload_hash"],
                            task["manifest_hash"],
                        )
                    )
                if platforms != PLATFORMS:
                    raise Conflict("PhoneRoundSixPlatformsRequired")
                db.execute(
                    "INSERT INTO phone_rounds(id,request_key,manifest_hash,state,created_at) VALUES(?,?,?,'draft',?)",
                    (identifier, request_key, manifest, now),
                )
                for position, member in enumerate(sorted(members, key=lambda m: m[2])):
                    task_id, item_id, platform, payload_hash, manifest_hash = member
                    db.execute(
                        "INSERT INTO phone_round_members(round_id,task_id,item_id,platform,position,payload_hash,manifest_hash,state) VALUES(?,?,?,?,?,?,?,'pending')",
                        (
                            identifier,
                            task_id,
                            item_id,
                            platform,
                            position,
                            payload_hash,
                            manifest_hash,
                        ),
                    )
                Store.audit(db, "phone_round.created", {"round_id": identifier}, now=now)
        return self.get(identifier)

    def start(self, round_id):
        # This intent starts the end-to-end clock. Never clears a stop or grants live permission.
        now = self.clock()
        with self.store.connect(write=True) as db:
            row = self._round(db, round_id)
            if row["state"] == "draft":
                runtime = db.execute(
                    "SELECT value FROM meta WHERE key='execution_runtime'"
                ).fetchone()
                if not runtime or runtime[0] != "pooled":
                    raise Conflict("PhoneRoundPooledRuntimeRequired")
                if not self.engine.live_enabled:
                    raise Conflict("PhoneRoundLiveWritesDisabled")
                db.execute(
                    "UPDATE phone_rounds SET state='running',started_at=?,submit_deadline=?,deadline=? WHERE id=?",
                    (now, now + SUBMIT_SECONDS, now + TOTAL_SECONDS, round_id),
                )
                Store.audit(db, "phone_round.started", {"round_id": round_id}, now=now)
        return self.get(round_id)

    def stop(self, round_id):
        now = self.clock()
        with self.store.connect(write=True) as db:
            row = self._round(db, round_id)
            if row["state"] in {"draft", "running"}:
                db.execute(
                    "UPDATE phone_rounds SET state='stopped',finished_at=? WHERE id=?",
                    (now, round_id),
                )
                Store.audit(db, "phone_round.stopped", {"round_id": round_id}, now=now)
        # Existing uncertain submissions and their original receipt lookups are preserved.
        return self.get(round_id)

    def _members(self, db, row, now):
        members = []
        for m in db.execute(
            "SELECT m.*,i.status item_status,i.attempt_id,i.result,i.external_reference,a.started_at attempt_started_at,"
            "a.finished_at attempt_finished_at FROM phone_round_members m JOIN items i ON i.id=m.item_id "
            "LEFT JOIN attempts a ON a.id=i.attempt_id WHERE m.round_id=? ORDER BY m.position",
            (row["id"],),
        ):
            result = json.loads(m["result"])
            verified = bool(
                m["item_status"] == "confirmed"
                and m["attempt_id"]
                and m["attempt_finished_at"] is not None
                and m["external_reference"]
                and result.get("verified") is True
            )
            in_budget = bool(
                verified
                and row["started_at"] is not None
                and row["started_at"]
                <= m["attempt_started_at"]
                <= m["attempt_finished_at"]
                <= row["deadline"]
            )
            stage = (
                "confirmed"
                if verified
                else "unknown"
                if m["item_status"] == "unknown"
                else "executing"
                if m["item_status"] == "executing"
                else "blocked"
                if m["state"] == "blocked"
                else m["state"]
            )
            table = (
                "phone_reddit_authorizations"
                if m["platform"] == "reddit"
                else "phone_social_authorizations"
            )
            native = None
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone():
                native = db.execute(
                    f"SELECT execution_id,used_at FROM {table} WHERE item_id=?", (m["item_id"],)
                ).fetchone()
            submitted_at = native["used_at"] if native else None
            members.append(
                {
                    "platform": m["platform"],
                    "execution_id": native["execution_id"] if native else None,
                    "submit_authorized_at": submitted_at,
                    "before_authorization_seconds": max(0, submitted_at - m["attempt_started_at"])
                    if submitted_at is not None and m["attempt_started_at"] is not None
                    else None,
                    "receipt_wait_seconds": max(0, (m["attempt_finished_at"] or now) - submitted_at)
                    if submitted_at is not None
                    else None,
                    "task_id": m["task_id"],
                    "item_id": m["item_id"],
                    "attempt_id": m["attempt_id"],
                    "stage": stage,
                    "item_status": m["item_status"],
                    "verified": verified,
                    "within_budget": in_budget,
                    "external_reference": m["external_reference"] if verified else None,
                    "reason": m["error"] or result.get("reason"),
                    "start_requested_at": m["start_requested_at"],
                    "enqueued_at": m["enqueued_at"],
                    "attempt_started_at": m["attempt_started_at"],
                    "receipt_verified_at": m["attempt_finished_at"] if verified else None,
                    "queue_seconds": max(0, (m["attempt_started_at"] or now) - m["enqueued_at"])
                    if m["enqueued_at"] is not None
                    else None,
                    "execution_seconds": max(
                        0, (m["attempt_finished_at"] or now) - m["attempt_started_at"]
                    )
                    if m["attempt_started_at"] is not None
                    else None,
                }
            )
        return members

    def get(self, round_id):
        now = self.clock()
        with self.store.connect() as db:
            row = self._round(db, round_id)
            members = self._members(db, row, now)
            cutoff = json.loads(row["cutoff_snapshot"]) if row["cutoff_snapshot"] else None
            elapsed = (
                max(0, (row["finished_at"] or now) - row["started_at"])
                if row["started_at"] is not None
                else 0
            )
            return {
                "id": row["id"],
                "state": "expired"
                if row["state"] == "running" and now >= row["deadline"]
                else row["state"],
                "stored_state": row["state"],
                "created_at": row["created_at"],
                "started_at": row["started_at"],
                "submit_deadline": row["submit_deadline"],
                "deadline": row["deadline"],
                "elapsed_seconds": elapsed,
                "remaining_seconds": max(0, TOTAL_SECONDS - elapsed),
                "confirmed_count": sum(m["verified"] for m in members),
                "within_budget_count": sum(m["within_budget"] for m in members),
                "all_six_within_budget": len(members) == 6
                and all(m["within_budget"] for m in members),
                "members": members,
                "cutoff_snapshot": cutoff,
            }

    def tick(self):
        """One bounded admission per tick; existing pool owns execution/device/account locks."""
        now, chosen = self.clock(), None
        repair = []
        with self.store.connect(write=True) as db:
            rounds = db.execute(
                "SELECT * FROM phone_rounds WHERE state='running' ORDER BY started_at,id"
            ).fetchall()
            for row in rounds:
                members = self._members(db, row, now)
                complete = len(members) == 6 and all(m["verified"] for m in members)
                if complete or now >= row["deadline"]:
                    db.execute(
                        "UPDATE phone_rounds SET state=?,finished_at=?,cutoff_snapshot=? WHERE id=?",
                        (
                            "completed"
                            if complete and all(m["within_budget"] for m in members)
                            else "expired",
                            min(now, row["deadline"]),
                            canonical(members),
                            row["id"],
                        ),
                    )
                    continue
                # Native start may have completed before a process died. Repair the original queue only.
                for member in db.execute(
                    "SELECT * FROM phone_round_members WHERE round_id=? AND state='starting' AND start_lease_until<=?",
                    (row["id"], now),
                ).fetchall():
                    task = db.execute(
                        "SELECT state FROM tasks WHERE id=?", (member["task_id"],)
                    ).fetchone()
                    if task and task["state"] == "active":
                        repair.append(member["task_id"])
                    else:
                        db.execute(
                            "UPDATE phone_round_members SET state='blocked',error='PhoneRoundStartInterrupted' WHERE task_id=?",
                            (member["task_id"],),
                        )
                stop = db.execute("SELECT value FROM meta WHERE key='global_stop'").fetchone()
                if (
                    chosen
                    or now >= row["submit_deadline"]
                    or not stop
                    or stop[0] != "0"
                    or not self.engine.live_enabled
                ):
                    continue
                runtime = db.execute(
                    "SELECT value FROM meta WHERE key='execution_runtime'"
                ).fetchone()
                if not runtime or runtime[0] != "pooled":
                    continue
                if db.execute(
                    "SELECT 1 FROM phone_round_members WHERE state='starting' AND start_lease_until>? LIMIT 1",
                    (now,),
                ).fetchone():
                    continue
                member = db.execute(
                    "SELECT * FROM phone_round_members WHERE round_id=? AND state='pending' ORDER BY position LIMIT 1",
                    (row["id"],),
                ).fetchone()
                if member:
                    db.execute(
                        "UPDATE phone_round_members SET state='starting',start_requested_at=?,start_lease_until=? WHERE task_id=? AND state='pending'",
                        (now, min(now + 120, row["deadline"]), member["task_id"]),
                    )
                    chosen = member["task_id"]
        for task_id in repair:
            from .queue_store import JobQueue

            try:
                # Idempotent metadata repair only. Keep the intent pending until sync succeeds,
                # including if the process dies between queue sync and the member update.
                JobQueue(self.engine).sync(task_id)
            except Exception:
                self.store.audit_best_effort(
                    "phone_round.queue_repair_failed",
                    {"reason": "PhoneRoundQueueRepairFailed"},
                    task_id=task_id,
                    now=self.clock(),
                )
            else:
                with self.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE phone_round_members SET state='queued',enqueued_at=?,error='PhoneRoundQueueRecovered' WHERE task_id=? AND state='starting'",
                        (self.clock(), task_id),
                    )
        if chosen:
            try:
                # Normal engine requires fresh cached identity, policy checks and original JobQueue.sync.
                # Stale identity fails closed; round admission never navigates six apps again.
                self.engine.start_and_run(chosen, source="phone_round")
            except Exception as exc:
                reason = (
                    str(exc)
                    if isinstance(exc, Conflict)
                    and re.fullmatch(r"Phone[A-Za-z0-9_]{1,90}", str(exc))
                    else "PhoneRoundStartFailed"
                )
                # No automatic start retry. No arbitrary provider text/credentials in diagnostics.
                with self.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE phone_round_members SET state='blocked',error=? WHERE task_id=? AND state='starting'",
                        (reason, chosen),
                    )
                    Store.audit(
                        db,
                        "phone_round.start_failed",
                        {"reason": reason, "error_type": type(exc).__name__},
                        chosen,
                        self.clock(),
                    )
            else:
                with self.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE phone_round_members SET state='queued',enqueued_at=? WHERE task_id=? AND state='starting'",
                        (self.clock(), chosen),
                    )
        return chosen
