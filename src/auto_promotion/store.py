from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .models import canonical

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tiktok_oauth(
 id TEXT PRIMARY KEY, expires_at REAL NOT NULL, client_id TEXT NOT NULL,
 redirect_uri TEXT NOT NULL, details TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tiktok_events(
 id TEXT PRIMARY KEY, received_at REAL NOT NULL, event TEXT NOT NULL, details TEXT NOT NULL
);
INSERT OR IGNORE INTO meta VALUES('schema_version','1');
INSERT OR IGNORE INTO meta VALUES('global_stop','1');
CREATE TABLE IF NOT EXISTS accounts(
 id TEXT PRIMARY KEY, platform TEXT NOT NULL, label TEXT NOT NULL, backend_label TEXT NOT NULL,
 mode TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'unchecked', health TEXT NOT NULL DEFAULT '{}',
 checked_at REAL, cooldown_until REAL NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
 daily_limit INTEGER NOT NULL, hourly_limit INTEGER NOT NULL, min_interval INTEGER NOT NULL,
 disabled INTEGER NOT NULL DEFAULT 0, identity_hash TEXT,
 UNIQUE(platform,backend_label,mode)
);
CREATE TABLE IF NOT EXISTS tasks(
 id TEXT PRIMARY KEY, name TEXT NOT NULL, mode TEXT NOT NULL, account_ids TEXT NOT NULL,
 account_snapshot TEXT NOT NULL, policy TEXT NOT NULL, policy_hash TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'draft', phase TEXT NOT NULL DEFAULT 'discovery',
 next_at REAL NOT NULL DEFAULT 0,
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
CREATE TABLE IF NOT EXISTS worker_runs(
 id TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(id), pid INTEGER NOT NULL,
 live_enabled INTEGER NOT NULL DEFAULT 0, started_at REAL NOT NULL, heartbeat_at REAL NOT NULL,
 finished_at REAL, state TEXT NOT NULL, phase TEXT NOT NULL, reason TEXT, exit_code INTEGER,
 diagnostics TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS workers_task ON worker_runs(task_id,started_at);
CREATE INDEX IF NOT EXISTS attempts_limits ON attempts(account_id,started_at);
CREATE TABLE IF NOT EXISTS connection_jobs(
 id TEXT PRIMARY KEY, platform TEXT NOT NULL, username TEXT NOT NULL,
 backend_label TEXT NOT NULL, stage TEXT NOT NULL, created_at REAL NOT NULL,
 details TEXT NOT NULL DEFAULT '{}'
);
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
CREATE TABLE IF NOT EXISTS publication_evidence(
 item_id TEXT PRIMARY KEY REFERENCES items(id), published_at REAL,
 verified_at REAL NOT NULL, timestamp_source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback_events(
 platform TEXT NOT NULL, account_id TEXT NOT NULL REFERENCES accounts(id),
 event_id TEXT NOT NULL, item_id TEXT NOT NULL REFERENCES items(id),
 occurred_at REAL NOT NULL, observed_at REAL NOT NULL,
 PRIMARY KEY(platform,account_id,event_id)
);
CREATE TABLE IF NOT EXISTS feedback_checks(
 item_id TEXT PRIMARY KEY REFERENCES items(id), checked_through REAL NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('complete','partial','failed')), last_message_id INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS statistical_reports(
 kind TEXT NOT NULL, cutoff REAL NOT NULL, starts_at REAL NOT NULL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, revision INTEGER NOT NULL,
 content_hash TEXT NOT NULL, content TEXT NOT NULL,
 PRIMARY KEY(kind,cutoff)
);
CREATE TABLE IF NOT EXISTS reporting_state(
 id INTEGER PRIMARY KEY CHECK(id=1), last_tick REAL NOT NULL, source_revision INTEGER NOT NULL
);
INSERT OR IGNORE INTO meta VALUES('statistics_revision','0');
CREATE INDEX IF NOT EXISTS feedback_item_time ON feedback_events(item_id,occurred_at);
CREATE TABLE IF NOT EXISTS account_profiles(
 account_id TEXT PRIMARY KEY REFERENCES accounts(id),version INTEGER NOT NULL,
 payload TEXT NOT NULL,updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS account_pools(
 id TEXT PRIMARY KEY,name TEXT NOT NULL,description TEXT NOT NULL,limits TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1,created_at REAL NOT NULL,updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pool_members(
 pool_id TEXT NOT NULL REFERENCES account_pools(id),account_id TEXT NOT NULL REFERENCES accounts(id),
 PRIMARY KEY(pool_id,account_id)
);
CREATE TABLE IF NOT EXISTS opportunities(
 id TEXT PRIMARY KEY,platform TEXT NOT NULL,content_key TEXT NOT NULL,payload TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1,created_at REAL NOT NULL,updated_at REAL NOT NULL,
 UNIQUE(platform,content_key)
);
CREATE TABLE IF NOT EXISTS campaigns(
 id TEXT PRIMARY KEY,name TEXT NOT NULL,pool_id TEXT NOT NULL REFERENCES account_pools(id),
 config TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'draft',revision INTEGER NOT NULL DEFAULT 1,
 plan_hash TEXT,plan_snapshot TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS campaign_assignments(
 id TEXT PRIMARY KEY,campaign_id TEXT NOT NULL REFERENCES campaigns(id),opportunity_id TEXT NOT NULL REFERENCES opportunities(id),
 account_id TEXT REFERENCES accounts(id),score REAL NOT NULL DEFAULT 0,reasons TEXT NOT NULL,
 payload TEXT,profile_snapshot TEXT,status TEXT NOT NULL,task_id TEXT REFERENCES tasks(id),item_id TEXT REFERENCES items(id),
 UNIQUE(campaign_id,opportunity_id)
);
CREATE INDEX IF NOT EXISTS assignments_campaign ON campaign_assignments(campaign_id,status);
CREATE TABLE IF NOT EXISTS content_contacts(
 mode TEXT NOT NULL,platform TEXT NOT NULL,content_key TEXT NOT NULL,
 assignment_id TEXT NOT NULL UNIQUE REFERENCES campaign_assignments(id),
 PRIMARY KEY(mode,platform,content_key)
);
CREATE TABLE IF NOT EXISTS quota_reservations(
 item_id TEXT PRIMARY KEY REFERENCES items(id),account_id TEXT NOT NULL REFERENCES accounts(id),
 task_id TEXT NOT NULL REFERENCES tasks(id),due_at REAL NOT NULL,payload TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('reserved','consumed'))
);
CREATE INDEX IF NOT EXISTS reservations_due ON quota_reservations(state,due_at);
CREATE TABLE IF NOT EXISTS account_lanes(
 account_id TEXT PRIMARY KEY REFERENCES accounts(id),owner TEXT NOT NULL,
 expires_at REAL NOT NULL,heartbeat_at REAL NOT NULL
);
INSERT OR IGNORE INTO meta VALUES('execution_settings','{}');
CREATE TABLE IF NOT EXISTS growth_goals(
 id TEXT PRIMARY KEY,payload TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS growth_channels(
 id TEXT PRIMARY KEY,goal_id TEXT NOT NULL REFERENCES growth_goals(id),
 payload TEXT NOT NULL,link_key TEXT NOT NULL UNIQUE,created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS conversion_events(
 goal_id TEXT NOT NULL REFERENCES growth_goals(id),source TEXT NOT NULL,event_id TEXT NOT NULL,
 subject_hash TEXT NOT NULL,kind TEXT NOT NULL,occurred_at REAL NOT NULL,
 channel_id TEXT REFERENCES growth_channels(id),payload TEXT NOT NULL,payload_hash TEXT NOT NULL,
 imported_at REAL NOT NULL,PRIMARY KEY(goal_id,source,event_id)
);
CREATE INDEX IF NOT EXISTS conversions_cohort ON conversion_events(goal_id,subject_hash,kind);
CREATE TABLE IF NOT EXISTS growth_store_metrics(
 channel_id TEXT NOT NULL REFERENCES growth_channels(id),day TEXT NOT NULL,
 country TEXT NOT NULL DEFAULT '',payload TEXT NOT NULL,updated_at REAL NOT NULL,
 PRIMARY KEY(channel_id,day,country)
);
CREATE TABLE IF NOT EXISTS reply_inbox(
 id TEXT PRIMARY KEY,platform TEXT NOT NULL,account_id TEXT NOT NULL,event_id TEXT NOT NULL,
 item_id TEXT NOT NULL REFERENCES items(id),state TEXT NOT NULL DEFAULT 'open',
 note TEXT NOT NULL DEFAULT '',followup_task_id TEXT REFERENCES tasks(id),updated_at REAL NOT NULL,
 UNIQUE(platform,account_id,event_id)
);
CREATE TABLE IF NOT EXISTS content_feeds(
 id TEXT PRIMARY KEY,payload TEXT NOT NULL,next_at REAL NOT NULL,last_at REAL,
 status TEXT NOT NULL DEFAULT 'waiting',result TEXT NOT NULL DEFAULT '{}',
 lease_until REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS task_templates(
 id TEXT PRIMARY KEY,name TEXT NOT NULL,description TEXT NOT NULL,mode TEXT NOT NULL,
 platforms TEXT NOT NULL,settings TEXT NOT NULL,source_task_id TEXT REFERENCES tasks(id),
 created_at REAL NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            from .queue_store import QUEUE_SCHEMA

            db.executescript(QUEUE_SCHEMA)
            # Existing workspaces predate identity binding and task-wide pacing.
            if "identity_hash" not in {r[1] for r in db.execute("PRAGMA table_info(accounts)")}:
                db.execute("ALTER TABLE accounts ADD COLUMN identity_hash TEXT")
            if "next_at" not in {r[1] for r in db.execute("PRAGMA table_info(tasks)")}:
                db.execute("ALTER TABLE tasks ADD COLUMN next_at REAL NOT NULL DEFAULT 0")
            if "campaign_id" not in {r[1] for r in db.execute("PRAGMA table_info(tasks)")}:
                db.execute("ALTER TABLE tasks ADD COLUMN campaign_id TEXT REFERENCES campaigns(id)")
            db.execute("CREATE INDEX IF NOT EXISTS tasks_campaign ON tasks(campaign_id)")
            if "execution_step" not in {r[1] for r in db.execute("PRAGMA table_info(calls)")}:
                db.execute("ALTER TABLE calls ADD COLUMN execution_step TEXT")
            if "last_message_id" not in {
                r[1] for r in db.execute("PRAGMA table_info(feedback_checks)")
            }:
                db.execute(
                    "ALTER TABLE feedback_checks ADD COLUMN last_message_id INTEGER NOT NULL DEFAULT 0"
                )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS accounts_identity_unique "
                "ON accounts(platform,identity_hash,mode) WHERE identity_hash IS NOT NULL"
            )
            for table in ("items", "publication_evidence", "feedback_events", "feedback_checks"):
                for event in ("INSERT", "UPDATE", "DELETE"):
                    db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS statistics_{table}_{event.lower()} "
                        f"AFTER {event} ON {table} BEGIN "
                        "UPDATE meta SET value=CAST(value AS INTEGER)+1 "
                        "WHERE key='statistics_revision'; END"
                    )
            for table, columns in (("accounts", "platform,mode"), ("tasks", "mode")):
                db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS statistics_{table}_scope "
                    f"AFTER UPDATE OF {columns} ON {table} BEGIN "
                    "UPDATE meta SET value=CAST(value AS INTEGER)+1 "
                    "WHERE key='statistics_revision'; END"
                )
        Path(self.path).chmod(0o600)

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        from .queue_store import bind_connection

        bind_connection(db)
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

    def log_call(
        self,
        platform,
        operation,
        status,
        started,
        task_id=None,
        error_code=None,
        execution_step=None,
    ):
        with self.connect(write=True) as db:
            db.execute(
                "INSERT INTO calls(at,platform,operation,status,duration_ms,task_id,error_code,execution_step) VALUES(?,?,?,?,?,?,?,?)",
                (
                    time.time(),
                    platform,
                    operation,
                    status,
                    int((time.monotonic() - started) * 1000),
                    task_id,
                    error_code,
                    execution_step,
                ),
            )


def decode(row, *keys):
    result = dict(row)
    for key in keys:
        if result.get(key) is not None:
            result[key] = json.loads(result[key])
    return result
