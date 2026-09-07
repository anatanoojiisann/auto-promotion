from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .models import canonical

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO meta VALUES('schema_version','1');
INSERT OR IGNORE INTO meta VALUES('global_stop','1');
CREATE TABLE IF NOT EXISTS accounts(
 id TEXT PRIMARY KEY, platform TEXT NOT NULL, label TEXT NOT NULL, backend_label TEXT NOT NULL,
 mode TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'unchecked', health TEXT NOT NULL DEFAULT '{}',
 checked_at REAL, cooldown_until REAL NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
 daily_limit INTEGER NOT NULL, hourly_limit INTEGER NOT NULL, min_interval INTEGER NOT NULL,
 disabled INTEGER NOT NULL DEFAULT 0, UNIQUE(platform,backend_label,mode)
);
CREATE TABLE IF NOT EXISTS tasks(
 id TEXT PRIMARY KEY, name TEXT NOT NULL, mode TEXT NOT NULL, account_ids TEXT NOT NULL,
 account_snapshot TEXT NOT NULL, policy TEXT NOT NULL, policy_hash TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'draft', phase TEXT NOT NULL DEFAULT 'discovery',
 manifest TEXT, manifest_hash TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals(
 task_id TEXT NOT NULL REFERENCES tasks(id), stage TEXT NOT NULL, hash TEXT NOT NULL,
 phrase TEXT NOT NULL, actor TEXT NOT NULL, approved_at REAL NOT NULL,
 PRIMARY KEY(task_id,stage), CHECK(stage IN ('discovery_join','outbound'))
);
CREATE TABLE IF NOT EXISTS items(
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), account_id TEXT NOT NULL REFERENCES accounts(id),
 position INTEGER NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL, due_at REAL NOT NULL,
 status TEXT NOT NULL DEFAULT 'not_attempted', attempt_id TEXT, claimed_at REAL,
 external_reference TEXT, result TEXT NOT NULL DEFAULT '{}', UNIQUE(task_id,position)
);
CREATE TABLE IF NOT EXISTS attempts(
 id TEXT PRIMARY KEY, item_id TEXT NOT NULL UNIQUE REFERENCES items(id), account_id TEXT NOT NULL,
 task_id TEXT NOT NULL, target_key TEXT NOT NULL, user_key TEXT NOT NULL, mode TEXT NOT NULL,
 started_at REAL NOT NULL, finished_at REAL, outcome TEXT NOT NULL DEFAULT 'claimed'
);
CREATE TABLE IF NOT EXISTS candidates(
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), account_id TEXT NOT NULL,
 target TEXT NOT NULL, payload TEXT NOT NULL, decision TEXT NOT NULL, reasons TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'not_attempted', result TEXT NOT NULL DEFAULT '{}',
 UNIQUE(task_id,account_id,target)
);
CREATE TABLE IF NOT EXISTS audit(
 id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, task_id TEXT, kind TEXT NOT NULL, details TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calls(
 id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, platform TEXT NOT NULL,
 operation TEXT NOT NULL, status TEXT NOT NULL, duration_ms INTEGER NOT NULL, task_id TEXT,
 error_code TEXT
);
CREATE INDEX IF NOT EXISTS items_due ON items(status,due_at);
CREATE INDEX IF NOT EXISTS attempts_limits ON attempts(account_id,started_at);
CREATE TABLE IF NOT EXISTS join_attempts(
 id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL UNIQUE REFERENCES candidates(id),
 account_id TEXT NOT NULL, task_id TEXT NOT NULL, target_key TEXT NOT NULL,
 user_key TEXT NOT NULL, mode TEXT NOT NULL, started_at REAL NOT NULL,
 finished_at REAL, outcome TEXT NOT NULL DEFAULT 'claimed'
);
CREATE VIEW IF NOT EXISTS activity AS
 SELECT account_id,task_id,target_key,user_key,mode,started_at,outcome FROM attempts
 UNION ALL
 SELECT account_id,task_id,target_key,user_key,mode,started_at,outcome FROM join_attempts;
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
        Path(self.path).chmod(0o600)

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def rows(self, sql, params=()):
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, params)]

    def one(self, sql, params=()):
        rows = self.rows(sql, params)
        return rows[0] if rows else None

    @staticmethod
    def audit(db, kind, details, task_id=None, now=None):
        db.execute(
            "INSERT INTO audit(at,task_id,kind,details) VALUES(?,?,?,?)",
            (time.time() if now is None else now, task_id, kind, canonical(details)),
        )

    def log_call(self, platform, operation, status, started, task_id=None, error_code=None):
        with self.connect(write=True) as db:
            db.execute(
                "INSERT INTO calls(at,platform,operation,status,duration_ms,task_id,error_code) VALUES(?,?,?,?,?,?,?)",
                (
                    time.time(),
                    platform,
                    operation,
                    status,
                    int((time.monotonic() - started) * 1000),
                    task_id,
                    error_code,
                ),
            )


def decode(row, *keys):
    result = dict(row)
    for key in keys:
        if result.get(key) is not None:
            result[key] = json.loads(result[key])
    return result
