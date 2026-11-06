"""Opt-in scoped round coordinator. Original tasks remain the only send authority."""

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
PREFLIGHT_SECONDS = 30
SCHEMA = """
CREATE TABLE IF NOT EXISTS phone_rounds(
 id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE, manifest_hash TEXT NOT NULL,
 state TEXT NOT NULL, created_at REAL NOT NULL, started_at REAL, submit_deadline REAL,
 deadline REAL, finished_at REAL, cutoff_snapshot TEXT
);
CREATE TABLE IF NOT EXISTS phone_round_preflights(
 id INTEGER PRIMARY KEY AUTOINCREMENT, round_id TEXT NOT NULL REFERENCES phone_rounds(id),
 started_at REAL NOT NULL, finished_at REAL, result TEXT
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
    def __init__(self, engine, *, preflight_check=None):
        self.engine, self.store, self.clock = engine, engine.store, engine.clock
        self.preflight_check = preflight_check
        with self.store.connect(write=True) as db:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
            if "check_id" not in {
                col[1] for col in db.execute("PRAGMA table_info(phone_round_members)")
            }:
                db.execute("ALTER TABLE phone_round_members ADD COLUMN check_id INTEGER")
                # A pre-upgrade worker could have exited during preflight, before admission.
                db.execute(
                    "UPDATE phone_round_members SET state='blocked',error='PhoneRoundPreflightInterrupted' "
                    "WHERE round_id IN (SELECT id FROM phone_rounds WHERE state='checking') "
                    "AND state IN ('pending','blocked') AND start_requested_at IS NULL"
                )
                db.execute("UPDATE phone_rounds SET state='blocked' WHERE state='checking'")

    @staticmethod
    def _round(db, round_id):
        row = db.execute("SELECT * FROM phone_rounds WHERE id=?", (round_id,)).fetchone()
        if not row:
            raise NotFound("PhoneRoundNotFound")
        return row

    def create(self, task_ids, request_key):
        if (
            not 1 <= len(task_ids) <= len(PLATFORMS)
            or len(set(task_ids)) != len(task_ids)
            or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", request_key)
        ):
            raise Conflict("PhoneRoundDistinctTasksAndRequestKeyRequired")
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
                    if account["platform"] not in PLATFORMS or account["platform"] in platforms:
                        raise Conflict("PhoneRoundDistinctSupportedPlatformsRequired")
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
                db.execute(
                    "INSERT INTO phone_rounds(id,request_key,manifest_hash,state,created_at) VALUES(?,?,?,'draft',?)",
                    (identifier, request_key, manifest, now),
                )
                for position, member in enumerate(sorted(members, key=lambda m: m[2])):
                    task_id, item_id, platform, payload_hash, manifest_hash = member
                    db.execute(
                        "INSERT INTO phone_round_members(round_id,task_id,item_id,platform,position,payload_hash,manifest_hash,state) VALUES(?,?,?,?,?,?,?,'unstarted')",
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

    @staticmethod
    def _scope_intact(row, members):
        platforms = {m["platform"] for m in members}
        return (
            bool(platforms)
            and platforms <= PLATFORMS
            and len(platforms) == len(members)
            and digest(sorted(m["task_id"] for m in members)) == row["manifest_hash"]
        )

    @classmethod
    def _sync_state(cls, db, round_id, now):
        row = cls._round(db, round_id)
        if row["state"] in {"stopped", "completed", "expired"}:
            return
        states = {
            m[0]
            for m in db.execute(
                "SELECT state FROM phone_round_members WHERE round_id=?", (round_id,)
            )
        }
        if states == {"stopped"}:
            state = "stopped"
        elif row["started_at"] is None:
            state = "draft"
        elif states & {"pending", "starting", "queued"}:
            state = "running"
        elif "checking" in states:
            state = "checking"
        else:
            state = "blocked"
        db.execute(
            "UPDATE phone_rounds SET state=?,finished_at=? WHERE id=?",
            (state, now if state == "stopped" else None, round_id),
        )

    def _begin_check(self, round_id, platform, now):
        with self.store.connect(write=True) as db:
            row = self._round(db, round_id)
            members = db.execute(
                "SELECT * FROM phone_round_members WHERE round_id=?", (round_id,)
            ).fetchall()
            selected = {m["platform"] for m in members}
            if platform is not None and platform not in selected:
                raise NotFound("PhoneRoundPlatformNotSelected")
            if row["state"] in {"completed", "expired", "stopped"}:
                return None
            if not self._scope_intact(row, members):
                raise Conflict("PhoneRoundManifestChanged")
            if row["state"] == "draft":
                # Old drafts used 'pending' for members that had never passed preflight.
                db.execute(
                    "UPDATE phone_round_members SET state='unstarted' WHERE round_id=? "
                    "AND state='pending' AND start_requested_at IS NULL",
                    (round_id,),
                )
            db.execute(
                "UPDATE phone_round_members SET state='blocked',error='PhoneRoundPreflightInterrupted' "
                "WHERE round_id=? AND (? IS NULL OR platform=?) AND state='checking' AND check_id IN "
                "(SELECT id FROM phone_round_preflights WHERE round_id=? AND started_at<=?)",
                (round_id, platform, platform, round_id, now - PREFLIGHT_SECONDS),
            )
            scope = {
                m[0]
                for m in db.execute(
                    "SELECT platform FROM phone_round_members WHERE round_id=? "
                    "AND (? IS NULL OR platform=?) AND state IN ('unstarted','blocked') "
                    "AND start_requested_at IS NULL "
                    "AND NOT EXISTS (SELECT 1 FROM attempts a WHERE a.item_id=phone_round_members.item_id)",
                    (round_id, platform, platform),
                )
            }
            if not scope:
                return None
            if row["submit_deadline"] is not None and now >= row["submit_deadline"]:
                raise Conflict("PhoneRoundSubmissionDeadlineElapsed")
            runtime = db.execute("SELECT value FROM meta WHERE key='execution_runtime'").fetchone()
            if not runtime or runtime[0] != "pooled":
                raise Conflict("PhoneRoundPooledRuntimeRequired")
            if not self.engine.live_enabled:
                raise Conflict("PhoneRoundLiveWritesDisabled")
            db.execute(
                "UPDATE phone_rounds SET started_at=COALESCE(started_at,?),"
                "submit_deadline=COALESCE(submit_deadline,?),deadline=COALESCE(deadline,?) WHERE id=?",
                (now, now + SUBMIT_SECONDS, now + TOTAL_SECONDS, round_id),
            )
            check_id = db.execute(
                "INSERT INTO phone_round_preflights(round_id,started_at) VALUES(?,?)",
                (round_id, now),
            ).lastrowid
            db.executemany(
                "UPDATE phone_round_members SET state='checking',check_id=?,error=NULL "
                "WHERE round_id=? AND platform=?",
                [(check_id, round_id, p) for p in scope],
            )
            self._sync_state(db, round_id, now)
            Store.audit(
                db,
                "phone_round.start_requested",
                {"round_id": round_id, "platforms": sorted(scope)},
                now=now,
            )
            return check_id, scope, selected

    def start(self, round_id, platform=None):
        now = self.clock()
        check = self._begin_check(round_id, platform, now)
        if check is None:
            return self.get(round_id)
        check_id, scope, selected = check
        from .phone_round_readiness import preflight

        checker = self.preflight_check or preflight
        try:
            if self.preflight_check is None:

                def completed(rows):
                    self._apply_check(
                        round_id,
                        check_id,
                        {m["platform"] for m in rows},
                        {"round_id": round_id, "members": rows, "submission_authorized": False},
                        now,
                        final=False,
                    )

                snapshot = checker(self, round_id, platforms=scope, on_members=completed)
            else:
                snapshot = (
                    checker(self, round_id)
                    if scope == selected
                    else checker(self, round_id, platforms=scope)
                )
            members = snapshot["members"]
            if (
                snapshot.get("round_id") != round_id
                or snapshot.get("submission_authorized") is not False
                or not isinstance(members, list)
                or len(members) != len(scope)
                or {m["platform"] for m in members} != scope
            ):
                raise ValueError("Invalid readiness summary")
        except Exception:
            snapshot = {
                "round_id": round_id,
                "members": [],
                "submission_authorized": False,
                "blockers": ["PhoneRoundPreflightUnavailable"],
            }
        self._apply_check(round_id, check_id, scope, snapshot, now, final=True)
        return self.get(round_id)

    def _apply_check(self, round_id, check_id, scope, snapshot, now, *, final):
        finished = self.clock()
        blockers = list(snapshot.get("blockers", []))
        if finished < now:
            blockers.append("PhoneRoundClockInvalid")
        if finished - now >= PREFLIGHT_SECONDS:
            blockers.append("PhoneRoundPreflightBudgetElapsed")
        with self.store.connect(write=True) as db:
            row = self._round(db, round_id)
            stop = db.execute("SELECT value FROM meta WHERE key='global_stop'").fetchone()
            runtime = db.execute("SELECT value FROM meta WHERE key='execution_runtime'").fetchone()
            if not stop or stop[0] != "0":
                blockers.append("GlobalStop")
            if not runtime or runtime[0] != "pooled" or not self.engine.live_enabled:
                blockers.append("PhoneRoundRuntimeChanged")
            if finished >= row["submit_deadline"]:
                blockers.append("PhoneRoundSubmissionDeadlineElapsed")
            if finished >= row["deadline"]:
                blockers.append("PhoneRoundDeadlineElapsed")
            if row["state"] in {"stopped", "completed", "expired"}:
                blockers.append("PhoneRoundNotRunning")
            by_platform = {m["platform"]: m for m in snapshot["members"]}
            for app in sorted(scope):
                member = by_platform.get(app, {})
                allowed = (
                    not blockers and member.get("ready") is True and not member.get("blockers")
                )
                reasons = blockers or member.get("blockers") or ["PhoneRoundPrerequisitesMissing"]
                reason = reasons[0]
                if not isinstance(reason, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,100}", reason):
                    reason = "PhoneRoundPrerequisitesMissing"
                changed = db.execute(
                    "UPDATE phone_round_members SET state=?,error=? WHERE round_id=? AND platform=? "
                    "AND check_id=? AND state='checking' AND start_requested_at IS NULL "
                    "AND (?=0 OR (EXISTS (SELECT 1 FROM items i WHERE i.id=phone_round_members.item_id "
                    "AND i.task_id=phone_round_members.task_id AND i.status='not_attempted' AND i.attempt_id IS NULL) "
                    "AND NOT EXISTS (SELECT 1 FROM attempts a WHERE a.item_id=phone_round_members.item_id)))",
                    (
                        "pending" if allowed else "blocked",
                        None if allowed else reason,
                        round_id,
                        app,
                        check_id,
                        int(allowed),
                    ),
                ).rowcount
                if allowed and not changed:
                    db.execute(
                        "UPDATE phone_round_members SET state='blocked',error='PhoneRoundOriginalChanged' "
                        "WHERE round_id=? AND platform=? AND check_id=? AND state='checking'",
                        (round_id, app, check_id),
                    )
            admitted = db.execute(
                "SELECT count(*) FROM phone_round_members WHERE round_id=? AND check_id=? "
                "AND state IN ('pending','starting','queued')",
                (round_id, check_id),
            ).fetchone()[0]
            snapshot.update(
                platforms=sorted(scope),
                blockers=list(dict.fromkeys(blockers)),
                ready_count=admitted,
            )
            db.execute(
                "UPDATE phone_round_preflights SET finished_at=?,result=? WHERE id=?",
                (finished if final else None, canonical(snapshot), check_id),
            )
            self._sync_state(db, round_id, finished)
            Store.audit(
                db,
                "phone_round.preflight_completed"
                if final
                else "phone_round.platform_preflight_completed",
                {
                    "round_id": round_id,
                    "ready": bool(admitted),
                    "platforms": sorted(scope),
                    "duration_seconds": max(0, finished - now),
                },
                now=finished,
            )

    def stop_platform(self, round_id, platform):
        now = self.clock()
        with self.store.connect(write=True) as db:
            row = self._round(db, round_id)
            member = db.execute(
                "SELECT state FROM phone_round_members WHERE round_id=? AND platform=?",
                (round_id, platform),
            ).fetchone()
            if not member:
                raise NotFound("PhoneRoundPlatformNotSelected")
            if (
                row["state"] not in {"completed", "expired", "stopped"}
                and member["state"] != "stopped"
            ):
                db.execute(
                    "UPDATE phone_round_members SET state='stopped',error='PhoneRoundPlatformStopped' "
                    "WHERE round_id=? AND platform=?",
                    (round_id, platform),
                )
                self._sync_state(db, round_id, now)
                Store.audit(
                    db,
                    "phone_round.platform_stopped",
                    {"round_id": round_id, "platform": platform},
                    now=now,
                )
        return self.get(round_id)

    def stop(self, round_id):
        now = self.clock()
        with self.store.connect(write=True) as db:
            row = self._round(db, round_id)
            if row["state"] in {"draft", "checking", "running", "blocked"}:
                db.execute(
                    "UPDATE phone_rounds SET state='stopped',finished_at=? WHERE id=?",
                    (now, round_id),
                )
                Store.audit(db, "phone_round.stopped", {"round_id": round_id}, now=now)
        # Existing uncertain submissions and their original receipt lookups are preserved.
        return self.get(round_id)

    def _members(self, db, row, now, *, include_timing=False):
        members = []
        for m in db.execute(
            "SELECT m.*,i.status item_status,i.attempt_id,i.result,i.external_reference,a.started_at attempt_started_at,"
            "a.finished_at attempt_finished_at FROM phone_round_members m LEFT JOIN items i ON i.id=m.item_id AND i.task_id=m.task_id "
            "LEFT JOIN attempts a ON a.id=i.attempt_id AND a.item_id=i.id AND a.task_id=m.task_id AND a.account_id=i.account_id WHERE m.round_id=? ORDER BY m.position",
            (row["id"],),
        ):
            result = json.loads(m["result"] or "{}")
            job = db.execute(
                "SELECT result,state FROM execution_jobs WHERE id=?", ("send:" + m["item_id"],)
            ).fetchone()
            control_wait = json.loads(job["result"]).get("control_wait") if job else None
            waiting = bool(
                control_wait
                and job["state"] == "pending"
                and m["state"] == "queued"
                and row["state"] == "running"
                and m["item_status"] == "not_attempted"
                and now < row["submit_deadline"]
            )
            public_wait = (
                {k: v for k, v in control_wait.items() if k != "revision"} if control_wait else None
            )

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
                "waiting_for_configuration"
                if waiting and control_wait["waiting_for"] == "configuration_change"
                else "waiting_for_retry"
                if waiting
                else "confirmed"
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
            wait_end = min(now, row["finished_at"] or now, row["submit_deadline"] or now)
            from .execution_timing import report

            timing = report(db, m["task_id"]) if include_timing else None
            members.append(
                {
                    "platform": m["platform"],
                    **({"stage_timings": timing} if include_timing else {}),
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
                    "admission_state": m["state"],
                    "item_status": m["item_status"],
                    "verified": verified,
                    "within_budget": in_budget,
                    "external_reference": m["external_reference"] if verified else None,
                    "reason": m["error"]
                    or (control_wait or {}).get("reason")
                    or result.get("reason"),
                    "control_wait": public_wait,
                    "start_requested_at": m["start_requested_at"],
                    "enqueued_at": m["enqueued_at"],
                    "attempt_started_at": m["attempt_started_at"],
                    "receipt_verified_at": m["attempt_finished_at"] if verified else None,
                    "queue_seconds": max(
                        0, (m["attempt_started_at"] or wait_end) - m["enqueued_at"]
                    )
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
            members = self._members(db, row, now, include_timing=True)
            cutoff = json.loads(row["cutoff_snapshot"]) if row["cutoff_snapshot"] else None
            check = db.execute(
                "SELECT * FROM phone_round_preflights WHERE round_id=? ORDER BY id DESC LIMIT 1",
                (round_id,),
            ).fetchone()
            check_count = db.execute(
                "SELECT COUNT(*) FROM phone_round_preflights WHERE round_id=?", (round_id,)
            ).fetchone()[0]
            readiness = json.loads(check["result"]) if check and check["result"] else None
            elapsed = (
                max(0, (row["finished_at"] or now) - row["started_at"])
                if row["started_at"] is not None
                else 0
            )
            return {
                "id": row["id"],
                "state": "expired"
                if row["state"] in {"checking", "running", "blocked"} and now >= row["deadline"]
                else row["state"],
                "stored_state": row["state"],
                "created_at": row["created_at"],
                "started_at": row["started_at"],
                "submit_deadline": row["submit_deadline"],
                "deadline": row["deadline"],
                "elapsed_seconds": elapsed,
                "timing_scope": "prepared_tasks_including_preflight",
                "draft_wait_seconds": max(0, (row["started_at"] or now) - row["created_at"]),
                "preparation_included": False,
                "end_to_end_six_app_pass": False,
                "end_to_end_selected_apps_pass": False,
                "preflight_seconds": max(
                    0, (check["finished_at"] or row["finished_at"] or now) - check["started_at"]
                )
                if check
                else None,
                "preflight": readiness,
                "preflight_checks": check_count,
                "remaining_seconds": max(0, TOTAL_SECONDS - elapsed),
                "confirmed_count": sum(m["verified"] for m in members),
                "within_budget_count": sum(m["within_budget"] for m in members),
                "expected_platform_count": len(members),
                "selected_platforms": sorted(m["platform"] for m in members),
                "all_members_within_budget": self._scope_intact(row, members)
                and all(m["within_budget"] for m in members),
                "all_six_within_budget": self._scope_intact(row, members)
                and {m["platform"] for m in members} == PLATFORMS
                and all(m["within_budget"] for m in members),
                "members": members,
                "cutoff_snapshot": cutoff,
            }

    def tick(self):
        """One bounded admission per tick; existing pool owns execution/device/account locks."""
        # Idle rounds must not contend with unrelated platform writers.
        # A round started after this read is picked up on the next tick.
        if not self.store.one(
            "SELECT 1 FROM phone_rounds WHERE state IN ('checking','running','blocked') LIMIT 1"
        ):
            return
        now, chosen = self.clock(), None
        repair = []
        with self.store.connect(write=True) as db:
            rounds = db.execute(
                "SELECT * FROM phone_rounds WHERE state IN ('checking','running','blocked') ORDER BY started_at,id"
            ).fetchall()
            for row in rounds:
                members = self._members(db, row, now)
                complete = self._scope_intact(row, members) and all(m["verified"] for m in members)
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
                if row["state"] in {"checking", "blocked"}:
                    continue  # Waiting never stops the clock or starts native work.
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
                # Each original start intent is claimed atomically. The ordinary queue owns the device lock.
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
                # Stale identity fails closed; round admission uses cached identity.
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
