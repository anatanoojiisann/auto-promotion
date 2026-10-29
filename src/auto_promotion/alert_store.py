"""Separate durable notification database; never writes the campaign database."""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .models import canonical, digest

SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alert_snapshots(
 kind TEXT NOT NULL,object_id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(kind,object_id));
CREATE TABLE IF NOT EXISTS alert_incidents(
 object_id TEXT PRIMARY KEY,issue TEXT NOT NULL,opened_at REAL NOT NULL,
 last_notice REAL NOT NULL,evidence_at REAL NOT NULL,active INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS alert_outbox(
 id TEXT PRIMARY KEY,event_key TEXT NOT NULL UNIQUE,kind TEXT NOT NULL,
 text TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',created_at REAL NOT NULL,
 available_at REAL NOT NULL,expires_at REAL NOT NULL,lease_until REAL NOT NULL DEFAULT 0,
 owner TEXT,attempts INTEGER NOT NULL DEFAULT 0,reason TEXT,http_status INTEGER,
 finished_at REAL);
CREATE TABLE IF NOT EXISTS alert_attempts(
 notice_id TEXT NOT NULL,attempt INTEGER NOT NULL,started_at REAL NOT NULL,
 finished_at REAL,state TEXT NOT NULL,reason TEXT,http_status INTEGER,
 PRIMARY KEY(notice_id,attempt));
CREATE INDEX IF NOT EXISTS alert_due ON alert_outbox(state,available_at);
"""


class AlertStore:
    def __init__(self, path, *, clock=time.time):
        self.path, self.clock = Path(path).resolve(), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self, *, write=False):
        db = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def meta(db, key, default=None):
        row = db.execute("SELECT value FROM alert_meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def set_meta(db, key, value):
        db.execute("INSERT OR REPLACE INTO alert_meta VALUES(?,?)", (key, canonical(value)))

    @staticmethod
    def snapshot(db, kind, object_id):
        row = db.execute(
            "SELECT payload FROM alert_snapshots WHERE kind=? AND object_id=?", (kind, object_id)
        ).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def save(db, kind, object_id, data):
        db.execute(
            "INSERT OR REPLACE INTO alert_snapshots VALUES(?,?,?)",
            (kind, object_id, canonical(data)),
        )

    def enqueue(self, db, event_key, kind, text, *, due=None, ttl=86400):
        now = self.clock()
        notice_id = "AL-" + digest(event_key)[:20]
        db.execute(
            "INSERT OR IGNORE INTO alert_outbox(id,event_key,kind,text,created_at,available_at,expires_at) VALUES(?,?,?,?,?,?,?)",
            (
                notice_id,
                event_key,
                kind,
                text if kind == "platform_summary" else text + "\n通知编号：" + notice_id,
                now,
                now if due is None else due,
                now + ttl,
            ),
        )
        return notice_id

    def claim(self, notice_id=None):
        now = self.clock()
        with self.connect(write=True) as db:
            db.execute(
                "UPDATE alert_attempts SET state='unknown',finished_at=?,reason='sender_lease_expired' WHERE state='sending' AND notice_id IN (SELECT id FROM alert_outbox WHERE state='sending' AND lease_until<=?)",
                (now, now),
            )
            db.execute(
                "UPDATE alert_outbox SET state='unknown',reason='sender_lease_expired',finished_at=? WHERE state='sending' AND lease_until<=?",
                (now, now),
            )
            db.execute(
                "UPDATE alert_outbox SET state='expired',reason='notice_expired' WHERE state='pending' AND expires_at<=?",
                (now,),
            )
            if self.meta(db, "authorization_blocked", False):
                return None
            restriction = ""
            parameters = (now,)
            if self.meta(db, "delivery_policy") == "three_hour_counts_v1":
                from .alert_summary import summary_cutoff

                # Defense in depth: old queued notices and revisions cannot escape.
                restriction = " AND ((kind='platform_summary' AND event_key=?)" + (
                    " OR kind='test')" if notice_id else ")"
                )
                parameters += (f"platform_summary:{summary_cutoff(now)}",)
                db.execute(
                    "UPDATE alert_outbox SET state='expired',reason='summary_policy_replaced' WHERE state='pending' AND kind NOT IN ('platform_summary','test')"
                )
            row = db.execute(
                "SELECT * FROM alert_outbox WHERE state='pending' AND available_at<=?"
                + restriction
                + (" AND id=?" if notice_id else "")
                + " ORDER BY created_at,id LIMIT 1",
                parameters + (notice_id,) if notice_id else parameters,
            ).fetchone()
            if not row:
                return None
            row = dict(row)
            row.update(owner=uuid.uuid4().hex, attempts=row["attempts"] + 1)
            db.execute(
                "UPDATE alert_outbox SET state='sending',owner=?,attempts=?,lease_until=? WHERE id=?",
                (row["owner"], row["attempts"], now + 90, row["id"]),
            )
            db.execute(
                "INSERT INTO alert_attempts(notice_id,attempt,started_at,state) VALUES(?,?,?,'sending')",
                (row["id"], row["attempts"], now),
            )
            return row

    def settle(self, row, result):
        now, state = self.clock(), result["state"]
        reason, status = result.get("reason"), result.get("http")
        delay = max(result.get("retry_after", 0), (30, 120, 600)[min(row["attempts"] - 1, 2)])
        if state == "retry":
            state = "pending" if row["attempts"] <= 3 else "failed"
        with self.connect(write=True) as db:
            changed = db.execute(
                "UPDATE alert_outbox SET state=?,reason=?,http_status=?,available_at=?,finished_at=?,owner=NULL,lease_until=0 WHERE id=? AND owner=? AND state='sending'",
                (
                    state,
                    reason,
                    status,
                    now + delay,
                    now if state != "pending" else None,
                    row["id"],
                    row["owner"],
                ),
            ).rowcount
            if not changed:
                return
            db.execute(
                "UPDATE alert_attempts SET finished_at=?,state=?,reason=?,http_status=? WHERE notice_id=? AND attempt=?",
                (now, result["state"], reason, status, row["id"], row["attempts"]),
            )
            if reason == "relay_authorization_failed":
                self.set_meta(db, "authorization_blocked", True)

    def status(self):
        with self.connect() as db:
            return status_from_connection(db, self.clock())

    def planned_queue_sources(self):
        with self.connect() as db:
            return self.meta(db, "planned_queue_sources", [])


def read_alert_status(path, *, clock=time.time):
    path = Path(path).resolve()
    if not path.is_file():
        return {"service_state": "not_installed", "queue": {}, "delivery_status": "unverified"}
    try:
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA query_only=ON")
            return status_from_connection(db, clock())
        finally:
            db.close()
    except sqlite3.Error:
        return {"service_state": "unavailable", "error": "notification_status_unavailable"}


def status_from_connection(db, now):
    runtime = AlertStore.meta(db, "runtime", {})
    age = now - runtime.get("heartbeat_at", 0)
    return {
        "service_state": "running"
        if runtime and 0 <= age <= runtime.get("poll_seconds", 30) * 2 + 60
        else "stale"
        if runtime
        else "not_running",
        "runtime": runtime,
        "last_observed_at": AlertStore.meta(db, "last_observed_at"),
        "last_collection_error": AlertStore.meta(db, "last_collection_error"),
        "queue_sources": AlertStore.meta(db, "queue_sources_state", []),
        "delivery_policy": AlertStore.meta(db, "delivery_policy"),
        "next_summary_at": AlertStore.meta(db, "next_summary_at"),
        "last_skipped_summary": AlertStore.meta(db, "last_skipped_summary"),
        "last_recovered_summary": AlertStore.meta(db, "last_recovered_summary"),
        "summary_recovery_policy": AlertStore.meta(db, "summary_recovery_policy"),
        "authorization_blocked": AlertStore.meta(db, "authorization_blocked", False),
        "queue": {
            r[0]: r[1] for r in db.execute("SELECT state,count(*) FROM alert_outbox GROUP BY state")
        },
        "active_incidents": db.execute(
            "SELECT count(*) FROM alert_incidents WHERE active=1"
        ).fetchone()[0],
        "last_accepted_at": db.execute(
            "SELECT max(finished_at) FROM alert_outbox WHERE state='accepted'"
        ).fetchone()[0],
        "recent": [
            dict(r)
            for r in db.execute(
                "SELECT id,kind,state,created_at,reason,http_status FROM alert_outbox ORDER BY created_at DESC,id DESC LIMIT 15"
            )
        ],
        "delivery_status": "unverified",
    }
