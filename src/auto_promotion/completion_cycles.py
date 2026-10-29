"""Cross-workspace, per-account completion clocks and exact interaction handoffs.

This coordinator never calls a platform. A consumer scans only a claimed round,
records its exact candidates, and hands eligible work to the existing sender or
the account's established external workflow. Unknown submissions are never replayed.
"""

import json
import math
import time
import uuid
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field

from .core import Conflict
from .models import Model, canonical, digest
from .store import Store

PRIORITIES = ("P0", "P1", "P2", "P3", "P4")
FINISHED = {"confirmed", "failed_known", "skipped", "cancelled"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS completion_cycle_policy(
 id INTEGER PRIMARY KEY CHECK(id=1),enabled INTEGER NOT NULL,wait_seconds INTEGER NOT NULL,
 source TEXT NOT NULL,updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS completion_cycle_accounts(
 id TEXT PRIMARY KEY,platform TEXT NOT NULL,label TEXT NOT NULL,details TEXT NOT NULL,
 enrolled_at REAL NOT NULL,idle_since REAL,next_scan_at REAL,active_round_id TEXT
);
CREATE TABLE IF NOT EXISTS completion_cycle_workflows(
 account_id TEXT NOT NULL,reference TEXT NOT NULL,state TEXT NOT NULL,completed_at REAL,
 observed_at REAL NOT NULL,evidence TEXT NOT NULL,PRIMARY KEY(account_id,reference)
);
CREATE TABLE IF NOT EXISTS completion_cycle_rounds(
 id TEXT PRIMARY KEY,account_id TEXT NOT NULL,state TEXT NOT NULL,started_at REAL NOT NULL,
 scan_finished_at REAL,completed_at REAL,owner TEXT NOT NULL,lease_until REAL NOT NULL,
 evidence TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS completion_cycle_candidates(
 id TEXT PRIMARY KEY,round_id TEXT NOT NULL,account_id TEXT NOT NULL,platform TEXT NOT NULL,
 event_id TEXT NOT NULL,priority TEXT NOT NULL,payload TEXT NOT NULL,payload_hash TEXT NOT NULL,
 state TEXT NOT NULL,authorized_at REAL,authorization TEXT,task_id TEXT,result TEXT NOT NULL DEFAULT '{}',
 updated_at REAL NOT NULL,UNIQUE(platform,event_id)
);
CREATE TABLE IF NOT EXISTS completion_cycle_notices(
 round_id TEXT PRIMARY KEY,account_id TEXT NOT NULL,created_at REAL NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending',snapshot TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS completion_cycle_candidate_round ON completion_cycle_candidates(round_id,state,priority);
"""


class CycleCandidate(Model):
    event_id: str = Field(min_length=1, max_length=300)
    priority: Literal["P0", "P1", "P2", "P3", "P4"]
    target: str = Field(min_length=1, max_length=2000)
    source_quote: str = Field(min_length=1, max_length=10000)
    reason: str = Field(min_length=1, max_length=3000)
    draft: str = Field(min_length=1, max_length=5000)
    context_verified: bool
    unresolved: bool = False
    direct_relation_evidence: str = Field(default="", max_length=3000)
    evidence: list[str] = Field(min_length=1, max_length=20)
    expires_at: float


class CompletionCycles:
    def __init__(self, store, *, clock=time.time):
        self.store, self.clock = store, clock
        with store.connect(write=True) as db:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)

    def configure(self, *, enabled, source, wait_seconds=7200):
        if not source.strip() or type(wait_seconds) is not int or wait_seconds < 7200:
            raise Conflict("completion_policy_source_or_wait_invalid")
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO completion_cycle_policy VALUES(1,?,?,?,?) ON CONFLICT(id) "
                "DO UPDATE SET enabled=excluded.enabled,wait_seconds=excluded.wait_seconds,"
                "source=excluded.source,updated_at=excluded.updated_at",
                (int(enabled), wait_seconds, source, self.clock()),
            )
            Store.audit(
                db,
                "completion.policy_saved",
                {"enabled": enabled, "wait_seconds": wait_seconds, "source": source},
                now=self.clock(),
            )

    def enroll(self, account_id, platform, label, *, details=None):
        if not account_id or not platform or not label:
            raise Conflict("completion_account_identity_required")
        details = details or {}
        if set(details) - {
            "native_account_id",
            "connector",
            "profile_url",
            "workflow_paths",
            "scope",
        }:
            raise Conflict("completion_account_details_invalid")
        with self.store.connect(write=True) as db:
            old = db.execute(
                "SELECT platform FROM completion_cycle_accounts WHERE id=?", (account_id,)
            ).fetchone()
            if old and old[0] != platform:
                raise Conflict("completion_account_platform_changed")
            db.execute(
                "INSERT INTO completion_cycle_accounts VALUES(?,?,?,?,?,NULL,NULL,NULL) "
                "ON CONFLICT(id) DO UPDATE SET label=excluded.label,details=excluded.details",
                (account_id, platform, label, canonical(details), self.clock()),
            )

    def observe(self, account_id, reference, state, *, evidence, completed_at=None):
        if state not in {"busy", "unknown", "complete", "stopped"} or not evidence:
            raise Conflict("completion_observation_invalid")
        if completed_at is not None and (
            isinstance(completed_at, bool)
            or not math.isfinite(completed_at)
            or not 0 < completed_at <= self.clock()
        ):
            raise Conflict("completion_timestamp_invalid")
        if state in {"complete", "stopped"} and completed_at is None:
            raise Conflict("completion_timestamp_required")
        with self.store.connect(write=True) as db:
            if not db.execute(
                "SELECT 1 FROM completion_cycle_accounts WHERE id=?", (account_id,)
            ).fetchone():
                raise Conflict("completion_account_missing")
            db.execute(
                "INSERT INTO completion_cycle_workflows VALUES(?,?,?,?,?,?) ON CONFLICT(account_id,reference) "
                "DO UPDATE SET state=excluded.state,completed_at=excluded.completed_at,observed_at=excluded.observed_at,evidence=excluded.evidence",
                (account_id, reference, state, completed_at, self.clock(), evidence),
            )
            self._refresh(db, account_id)

    def _refresh(self, db, account_id):
        account = db.execute(
            "SELECT * FROM completion_cycle_accounts WHERE id=?", (account_id,)
        ).fetchone()
        workflows = db.execute(
            "SELECT * FROM completion_cycle_workflows WHERE account_id=?", (account_id,)
        ).fetchall()
        pending = db.execute(
            "SELECT 1 FROM completion_cycle_candidates WHERE account_id=? AND "
            "(state IN ('submitting','unknown','scheduled') OR (state='ready' AND (priority IN ('P0','P1') OR authorized_at IS NOT NULL))) LIMIT 1",
            (account_id,),
        ).fetchone()
        busy = not workflows or any(w["state"] in {"busy", "unknown"} for w in workflows) or pending
        if busy:
            db.execute(
                "UPDATE completion_cycle_accounts SET idle_since=NULL,next_scan_at=NULL WHERE id=?",
                (account_id,),
            )
            return
        if account["active_round_id"]:
            return
        # A late poll starts the conservative clock at the first observed completion.
        # Reading status/restarting never moves an existing clock forward.
        anchor = max([account["enrolled_at"], *[w["completed_at"] or 0 for w in workflows]])
        anchor = max(anchor, account["idle_since"] or 0)
        policy = db.execute(
            "SELECT wait_seconds FROM completion_cycle_policy WHERE id=1"
        ).fetchone()
        db.execute(
            "UPDATE completion_cycle_accounts SET idle_since=?,next_scan_at=? WHERE id=?",
            (anchor, anchor + (policy[0] if policy else 7200), account_id),
        )

    def status(self):
        now = self.clock()
        with self.store.connect(write=True) as db:
            for a in db.execute("SELECT id FROM completion_cycle_accounts").fetchall():
                self._settle(db, a["id"])
                self._refresh(db, a["id"])
            policy = db.execute("SELECT * FROM completion_cycle_policy WHERE id=1").fetchone()
            accounts = []
            for row in db.execute(
                "SELECT * FROM completion_cycle_accounts ORDER BY platform,label"
            ):
                a = dict(row)
                a["details"] = json.loads(a["details"])
                a["workflows"] = [
                    dict(w)
                    for w in db.execute(
                        "SELECT * FROM completion_cycle_workflows WHERE account_id=?", (a["id"],)
                    )
                ]
                a["due"] = bool(
                    policy
                    and policy["enabled"]
                    and a["next_scan_at"]
                    and a["next_scan_at"] <= now
                    and not a["active_round_id"]
                )
                a["blockers"] = [
                    w["reference"] + ":" + w["state"]
                    for w in a["workflows"]
                    if w["state"] in {"busy", "unknown"}
                ]
                if not a["workflows"]:
                    a["blockers"].append("completion_evidence_missing")
                accounts.append(a)
            return {
                "policy": dict(policy) if policy else None,
                "accounts": accounts,
                "pending_notices": [
                    dict(n) | {"snapshot": json.loads(n["snapshot"])}
                    for n in db.execute(
                        "SELECT * FROM completion_cycle_notices WHERE state='pending'"
                    )
                ],
                "pending_candidates": [
                    dict(c)
                    | {"payload": json.loads(c["payload"]), "result": json.loads(c["result"])}
                    for c in db.execute(
                        "SELECT * FROM completion_cycle_candidates WHERE state NOT IN ('confirmed','failed_known','skipped','cancelled') ORDER BY priority,updated_at"
                    )
                ],
            }

    def claim_scan(self, account_id, owner, *, freshness_seconds=600):
        now = self.clock()
        with self.store.connect(write=True) as db:
            self._settle(db, account_id)
            self._refresh(db, account_id)
            policy = db.execute("SELECT * FROM completion_cycle_policy WHERE id=1").fetchone()
            account = db.execute(
                "SELECT * FROM completion_cycle_accounts WHERE id=?", (account_id,)
            ).fetchone()
            if not policy or not policy["enabled"] or not account:
                raise Conflict("completion_policy_disabled_or_account_missing")
            native_id = json.loads(account["details"]).get("native_account_id")
            native = (
                db.execute(
                    "SELECT disabled,cooldown_until FROM accounts WHERE id=?", (native_id,)
                ).fetchone()
                if native_id
                else None
            )
            if native and (native["disabled"] or native["cooldown_until"] > now):
                raise Conflict("completion_account_disabled_or_cooldown")
            workflows = db.execute(
                "SELECT * FROM completion_cycle_workflows WHERE account_id=?", (account_id,)
            ).fetchall()
            if not workflows or any(
                w["state"] in {"busy", "unknown"} or w["observed_at"] < now - freshness_seconds
                for w in workflows
            ):
                raise Conflict("completion_fresh_evidence_required")
            if account["active_round_id"]:
                run = db.execute(
                    "SELECT * FROM completion_cycle_rounds WHERE id=?",
                    (account["active_round_id"],),
                ).fetchone()
                if run["state"] != "scanning" or run["lease_until"] > now:
                    raise Conflict("completion_round_in_progress")
                db.execute(
                    "UPDATE completion_cycle_rounds SET owner=?,lease_until=? WHERE id=?",
                    (owner, now + 1800, run["id"]),
                )
                return run["id"]
            if account["next_scan_at"] is None or account["next_scan_at"] > now:
                raise Conflict("completion_scan_not_due")
            round_id = "CC-" + uuid.uuid4().hex[:20]
            db.execute(
                "INSERT INTO completion_cycle_rounds(id,account_id,state,started_at,owner,lease_until) VALUES(?,?,'scanning',?,?,?)",
                (round_id, account_id, now, owner, now + 1800),
            )
            db.execute(
                "UPDATE completion_cycle_accounts SET active_round_id=?,next_scan_at=NULL WHERE id=?",
                (round_id, account_id),
            )
            Store.audit(
                db,
                "completion.scan_claimed",
                {"account_id": account_id, "round_id": round_id},
                now=now,
            )
            return round_id

    def finish_scan(self, round_id, owner, candidates, *, evidence, coverage="partial"):
        if (
            not evidence
            or coverage not in {"complete", "partial", "unavailable"}
            or len(candidates) > 100
        ):
            raise Conflict("completion_scan_evidence_invalid")
        values = [CycleCandidate.model_validate(c) for c in candidates]
        now = self.clock()
        with self.store.connect(write=True) as db:
            run = db.execute(
                "SELECT * FROM completion_cycle_rounds WHERE id=?", (round_id,)
            ).fetchone()
            if (
                not run
                or run["state"] != "scanning"
                or run["owner"] != owner
                or run["lease_until"] <= now
            ):
                raise Conflict("completion_scan_lease_lost")
            account = db.execute(
                "SELECT * FROM completion_cycle_accounts WHERE id=?", (run["account_id"],)
            ).fetchone()
            for c in values:
                if not math.isfinite(c.expires_at) or c.expires_at <= now:
                    raise Conflict("completion_candidate_expired")
                parsed = urlparse(c.target)
                hosts = {
                    "x": {"x.com", "www.x.com", "twitter.com", "www.twitter.com"},
                    "instagram": {"instagram.com", "www.instagram.com"},
                    "facebook": {"facebook.com", "www.facebook.com", "m.facebook.com"},
                    "telegram": {"t.me"},
                    "reddit": {"reddit.com", "www.reddit.com"},
                    "discord": {"discord.com"},
                    "tiktok": {"tiktok.com", "www.tiktok.com"},
                }
                if (
                    parsed.scheme != "https"
                    or parsed.netloc != parsed.hostname
                    or parsed.hostname not in hosts.get(account["platform"], set())
                ):
                    raise Conflict("completion_target_platform_mismatch")
                if account["platform"] == "x":
                    from .interaction_intents import event_id

                    if event_id("x", {"action": "reply", "target": c.target}) != c.event_id:
                        raise Conflict("completion_source_identity_mismatch")
                if c.priority in {"P0", "P1"} and (
                    not c.context_verified
                    or not c.unresolved
                    or (c.priority == "P0" and not c.direct_relation_evidence)
                ):
                    raise Conflict("completion_high_priority_evidence_required")
                payload = canonical(c.model_dump())
                db.execute(
                    "INSERT OR IGNORE INTO completion_cycle_candidates(id,round_id,account_id,platform,event_id,priority,payload,payload_hash,state,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        "ccc-" + digest([account["platform"], c.event_id])[:24],
                        round_id,
                        run["account_id"],
                        account["platform"],
                        c.event_id,
                        c.priority,
                        payload,
                        digest(c.model_dump()),
                        "ready" if c.priority in {"P0", "P1"} else "awaiting_authorization",
                        now,
                    ),
                )
            db.execute(
                "UPDATE completion_cycle_rounds SET state='interacting',scan_finished_at=?,evidence=? WHERE id=?",
                (now, canonical({"evidence": evidence, "coverage": coverage}), round_id),
            )
            self._settle(db, run["account_id"])

    def authorize(self, versions, *, source):
        if not versions or not source.strip():
            raise Conflict("completion_explicit_authorization_required")
        with self.store.connect(write=True) as db:
            for candidate_id, expected_hash in versions.items():
                c = db.execute(
                    "SELECT * FROM completion_cycle_candidates WHERE id=?", (candidate_id,)
                ).fetchone()
                if (
                    not c
                    or c["priority"] not in {"P2", "P3", "P4"}
                    or c["state"] != "awaiting_authorization"
                    or c["payload_hash"] != expected_hash
                ):
                    raise Conflict("completion_authorization_snapshot_changed")
                self._high_complete(db, c["round_id"], required=True)
                if json.loads(c["payload"])["expires_at"] <= self.clock():
                    raise Conflict("completion_candidate_expired")
                db.execute(
                    "UPDATE completion_cycle_candidates SET state='ready',authorized_at=?,authorization=?,updated_at=? WHERE id=?",
                    (self.clock(), source, self.clock(), candidate_id),
                )
                db.execute(
                    "UPDATE completion_cycle_rounds SET state='interacting',completed_at=NULL WHERE id=?",
                    (c["round_id"],),
                )
                db.execute(
                    "UPDATE completion_cycle_accounts SET idle_since=NULL,next_scan_at=NULL WHERE id=?",
                    (c["account_id"],),
                )
                Store.audit(
                    db,
                    "completion.low_priority_authorized",
                    {"candidate_id": candidate_id, "payload_hash": expected_hash, "source": source},
                    now=self.clock(),
                )

    def begin(self, candidate_id, expected_hash, *, task_id=None):
        now = self.clock()
        with self.store.connect(write=True) as db:
            c = db.execute(
                "SELECT * FROM completion_cycle_candidates WHERE id=?", (candidate_id,)
            ).fetchone()
            policy = db.execute("SELECT enabled FROM completion_cycle_policy WHERE id=1").fetchone()
            if (
                not policy
                or not policy[0]
                or not c
                or c["state"] != "ready"
                or c["payload_hash"] != expected_hash
            ):
                raise Conflict("completion_candidate_not_ready")
            stopped = db.execute("SELECT value FROM meta WHERE key='global_stop'").fetchone()
            if stopped and stopped[0] != "0":
                raise Conflict("global_stop")
            workflows = db.execute(
                "SELECT * FROM completion_cycle_workflows WHERE account_id=?", (c["account_id"],)
            ).fetchall()
            if not workflows or any(
                w["state"] in {"busy", "unknown"} or w["observed_at"] < now - 600 for w in workflows
            ):
                raise Conflict("completion_fresh_evidence_required")
            if c["priority"] not in {"P0", "P1"}:
                if c["authorized_at"] is None:
                    raise Conflict("completion_explicit_authorization_required")
                self._high_complete(db, c["round_id"], required=True)
            if json.loads(c["payload"])["expires_at"] <= now:
                raise Conflict("completion_candidate_expired")
            if json.loads(c["payload"])["context_verified"] is not True:
                raise Conflict("completion_context_unverified")
            account = db.execute(
                "SELECT details FROM completion_cycle_accounts WHERE id=?", (c["account_id"],)
            ).fetchone()
            native_id = json.loads(account[0]).get("native_account_id")
            if native_id:
                items = (
                    db.execute("SELECT * FROM items WHERE task_id=?", (task_id,)).fetchall()
                    if task_id
                    else []
                )
                if (
                    len(items) != 1
                    or items[0]["account_id"] != native_id
                    or items[0]["attempt_id"]
                    or items[0]["status"] != "not_attempted"
                ):
                    raise Conflict("completion_exact_native_task_required")
                item, source = json.loads(items[0]["payload"]), json.loads(c["payload"])
                matches = item["target"] == source["target"]
                if c["platform"] == "telegram":
                    parts = urlparse(source["target"]).path.strip("/").split("/")
                    matches = (
                        len(parts) == 2
                        and item["target"] in {"@" + parts[0], "https://t.me/" + parts[0]}
                        and str(item.get("reply_to")) == parts[1]
                    )
                elif c["platform"] == "discord":
                    parts = urlparse(source["target"]).path.strip("/").split("/")
                    matches = (
                        len(parts) == 4
                        and parts[0] == "channels"
                        and item["target"] == parts[2]
                        and item.get("discord_reply_to") == parts[3]
                    )
                if (
                    not matches
                    or item["text"] != source["draft"]
                    or item["action"] != "reply"
                    or item.get("execution_mode", "automatic") != "automatic"
                    or item.get("media")
                ):
                    raise Conflict("completion_native_manifest_mismatch")
            if db.execute(
                "SELECT 1 FROM completion_cycle_candidates WHERE account_id=? AND (state IN ('submitting','unknown','scheduled') OR (state='ready' AND priority<?)) AND id<>? LIMIT 1",
                (c["account_id"], c["priority"], c["id"]),
            ).fetchone():
                raise Conflict("completion_earlier_or_uncertain_interaction")
            db.execute(
                "UPDATE completion_cycle_candidates SET state='submitting',task_id=?,updated_at=? WHERE id=?",
                (task_id, now, c["id"]),
            )
            db.execute(
                "UPDATE completion_cycle_accounts SET idle_since=NULL,next_scan_at=NULL WHERE id=?",
                (c["account_id"],),
            )
            Store.audit(
                db,
                "completion.submission_reserved",
                {"candidate_id": c["id"], "payload_hash": expected_hash, "task_id": task_id},
                now=now,
            )

    def result(
        self,
        candidate_id,
        state,
        *,
        evidence,
        external_reference=None,
        verified=False,
        published_at=None,
        confirmed_not_submitted=False,
    ):
        if state not in FINISHED | {"unknown", "scheduled"} or not evidence:
            raise Conflict("completion_result_invalid")
        if state == "confirmed" and (
            verified is not True
            or not external_reference
            or not external_reference.startswith("https://")
            or published_at is None
            or isinstance(published_at, bool)
            or not math.isfinite(published_at)
            or not 0 < published_at <= self.clock()
        ):
            raise Conflict("completion_verified_receipt_required")
        with self.store.connect(write=True) as db:
            c = db.execute(
                "SELECT * FROM completion_cycle_candidates WHERE id=?", (candidate_id,)
            ).fetchone()
            if not c or c["state"] in FINISHED:
                raise Conflict("completion_result_already_final")
            if state in {"confirmed", "unknown", "scheduled", "failed_known"} and c[
                "state"
            ] not in {"submitting", "unknown", "scheduled"}:
                raise Conflict("completion_submission_not_reserved")
            if c["state"] in {"unknown", "scheduled", "submitting"} and state in {
                "skipped",
                "cancelled",
            }:
                raise Conflict("completion_uncertain_submission_cannot_close")
            if (
                c["state"] in {"unknown", "scheduled"}
                and state == "failed_known"
                and confirmed_not_submitted is not True
            ):
                raise Conflict("completion_uncertain_submission_requires_proof")
            db.execute(
                "UPDATE completion_cycle_candidates SET state=?,result=?,updated_at=? WHERE id=?",
                (
                    state,
                    canonical(
                        {
                            "evidence": evidence,
                            "external_reference": external_reference,
                            "verified": verified,
                            "published_at": published_at,
                        }
                    ),
                    self.clock(),
                    candidate_id,
                ),
            )
            self._settle(db, c["account_id"])

    def _high_complete(self, db, round_id, *, required=False):
        pending = db.execute(
            "SELECT 1 FROM completion_cycle_candidates WHERE round_id=? AND priority IN ('P0','P1') AND state NOT IN ('confirmed','failed_known','skipped','cancelled') LIMIT 1",
            (round_id,),
        ).fetchone()
        if pending and required:
            raise Conflict("completion_high_priority_not_finished")
        return not pending

    def _settle(self, db, account_id):
        now = self.clock()
        for c in db.execute(
            "SELECT id,payload FROM completion_cycle_candidates WHERE account_id=? AND state IN ('ready','awaiting_authorization')",
            (account_id,),
        ).fetchall():
            if json.loads(c["payload"])["expires_at"] <= now:
                db.execute(
                    "UPDATE completion_cycle_candidates SET state='skipped',result=?,updated_at=? WHERE id=?",
                    (canonical({"reason": "expired_before_submission"}), now, c["id"]),
                )
        for run in db.execute(
            "SELECT * FROM completion_cycle_rounds WHERE account_id=? AND state<>'scanning'",
            (account_id,),
        ).fetchall():
            if not self._high_complete(db, run["id"]):
                continue
            lows = db.execute(
                "SELECT id,priority,payload,payload_hash,state FROM completion_cycle_candidates WHERE round_id=? AND priority IN ('P2','P3','P4') AND state='awaiting_authorization' ORDER BY priority,id",
                (run["id"],),
            ).fetchall()
            if lows:
                db.execute(
                    "INSERT OR IGNORE INTO completion_cycle_notices(round_id,account_id,created_at,snapshot) VALUES(?,?,?,?)",
                    (
                        run["id"],
                        account_id,
                        now,
                        canonical([dict(c) | {"payload": json.loads(c["payload"])} for c in lows]),
                    ),
                )
                db.execute(
                    "UPDATE completion_cycle_notices SET snapshot=? WHERE round_id=? AND state='pending'",
                    (
                        canonical([dict(c) | {"payload": json.loads(c["payload"])} for c in lows]),
                        run["id"],
                    ),
                )
            else:
                db.execute(
                    "UPDATE completion_cycle_notices SET state='expired' WHERE round_id=? AND state='pending'",
                    (run["id"],),
                )
            pending = db.execute(
                "SELECT 1 FROM completion_cycle_candidates WHERE round_id=? AND state IN ('ready','submitting','unknown','scheduled') LIMIT 1",
                (run["id"],),
            ).fetchone()
            if pending:
                continue
            finished = run["completed_at"] or now
            db.execute(
                "UPDATE completion_cycle_rounds SET state=?,completed_at=? WHERE id=?",
                ("awaiting_authorization" if lows else "completed", finished, run["id"]),
            )
            db.execute(
                "UPDATE completion_cycle_accounts SET active_round_id=NULL,idle_since=MAX(COALESCE(idle_since,0),?) WHERE id=? AND (active_round_id=? OR active_round_id IS NULL)",
                (finished, account_id, run["id"]),
            )
        self._refresh(db, account_id)

    def acknowledge_notice(self, round_id):
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE completion_cycle_notices SET state='delivered' WHERE round_id=?",
                (round_id,),
            )
