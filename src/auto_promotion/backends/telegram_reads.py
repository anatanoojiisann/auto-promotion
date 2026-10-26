"""Private Telegram read ledger and data cache. No credentials or write permissions."""

from __future__ import annotations

import contextlib
import json
import math
import sqlite3
import time

from ..models import canonical, digest
from .common import BackendError, profile_path

DEFAULT_POLICY = {
    "requests_per_window": 120,
    "window_seconds": 3600,
    "scan_rpc_limit": 40,
    "messages_per_page": 100,
    "groups_per_scan": 10,
    "cache_ttl_seconds": 300,
}
POLICY_BOUNDS = {
    "requests_per_window": (1, 10000),
    "window_seconds": (60, 86400),
    "scan_rpc_limit": (1, 1000),
    "messages_per_page": (1, 100),
    "groups_per_scan": (1, 100),
    "cache_ttl_seconds": (0, 3600),
}


class ReadBudgetExceeded(BackendError):
    def __init__(self, reason, retry_after=0):
        super().__init__(reason)
        self.reason = reason
        self.retry_after = max(0, int(math.ceil(retry_after)))


def read_policy(value):
    if value is None:
        return dict(DEFAULT_POLICY)
    if not isinstance(value, dict) or set(value) - set(DEFAULT_POLICY):
        raise BackendError("InvalidTelegramReadPolicy")
    policy = {**DEFAULT_POLICY, **value}
    for key, (lower, upper) in POLICY_BOUNDS.items():
        if type(policy[key]) is not int or not lower <= policy[key] <= upper:
            raise BackendError("InvalidTelegramReadPolicy")
    return policy


class TelegramReads:
    def __init__(
        self,
        label,
        identity,
        revision,
        policy=None,
        *,
        context=None,
        scan_limit=None,
        clock=time.time,
    ):
        self.policy = read_policy(policy)
        self.requested_policy = dict(self.policy)
        self.context, self.clock = context, clock
        if scan_limit is not None and (type(scan_limit) is not int or scan_limit < 1):
            raise BackendError("InvalidTelegramReadScanLimit")
        self.scan_limit = scan_limit
        self.identity = digest(["telegram-read-identity-v1", str(identity)])
        self.scope = digest(["telegram-read-cache-v1", self.identity, str(revision)])
        # All labels for the SAME real identity on this machine share one ledger.
        self.path = profile_path("telegram", label).parent / "read-state.sqlite3"
        self.scan_used, self.denied, self.cache_hits, self.cache_misses = 0, 0, 0, 0
        self.cache_observed_at = None
        self.budget_used, self.budget_end = None, None
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS budgets(
                    identity TEXT PRIMARY KEY,start REAL NOT NULL,end REAL NOT NULL,
                    used INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS read_cache(
                    scope TEXT NOT NULL,kind TEXT NOT NULL,key TEXT NOT NULL,
                    observed REAL NOT NULL,data TEXT NOT NULL,PRIMARY KEY(scope,kind,key));
                CREATE TABLE IF NOT EXISTS read_cursors(
                    scope TEXT NOT NULL,key TEXT NOT NULL,data TEXT NOT NULL,
                    PRIMARY KEY(scope,key));
                CREATE TABLE IF NOT EXISTS read_snapshots(
                    scope TEXT NOT NULL,key TEXT NOT NULL,data TEXT NOT NULL,
                    PRIMARY KEY(scope,key));
                CREATE TABLE IF NOT EXISTS read_authority(
                    identity TEXT PRIMARY KEY,context TEXT NOT NULL,revision INTEGER NOT NULL,
                    policy TEXT NOT NULL);
            """)

    @contextlib.contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            db = sqlite3.connect(self.path, timeout=5)
            self.path.chmod(0o600)
            try:
                db.execute("PRAGMA busy_timeout=5000")
                db.execute("PRAGMA synchronous=FULL")
                yield db
                db.commit()
            finally:
                db.close()
        except (sqlite3.Error, OSError):
            raise BackendError("TelegramReadStateUnavailable") from None

    def refresh_policy(self):
        if self.context:
            try:
                from ..telegram_reading import read_policy_context

                current = read_policy_context(self.context)
                latest = read_policy(current["policy"])
            except Exception:
                raise BackendError("TelegramReadPolicyUnavailable") from None
            key = digest([self.context.get("database"), self.context.get("account_id")])
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT INTO read_authority VALUES(?,?,?,?) ON CONFLICT(identity) DO UPDATE SET "
                    "context=excluded.context,revision=excluded.revision,policy=excluded.policy "
                    "WHERE read_authority.context!=excluded.context OR excluded.revision>=read_authority.revision",
                    (self.identity, key, current["revision"], canonical(latest)),
                )
                row = db.execute(
                    "SELECT policy FROM read_authority WHERE identity=?", (self.identity,)
                ).fetchone()
            self.policy = read_policy(json.loads(row[0]))
        else:
            with self.connect() as db:
                row = db.execute(
                    "SELECT policy FROM read_authority WHERE identity=?", (self.identity,)
                ).fetchone()
            if row:
                authoritative = read_policy(json.loads(row[0]))
                self.policy = {
                    key: (max if key == "window_seconds" else min)(value, authoritative[key])
                    for key, value in self.requested_policy.items()
                }

    def reserve(self, count):
        """Reserve a whole _call batch before any logical read attempt is started."""
        self.refresh_policy()
        if type(count) is not int or count < 0:
            raise ValueError("Invalid read reservation")
        if not count:
            return
        limit = min(self.policy["scan_rpc_limit"], self.scan_limit or self.policy["scan_rpc_limit"])
        if self.scan_used + count > limit:
            self.denied += count
            raise ReadBudgetExceeded("TelegramReadScanBudgetExhausted")
        now = self.clock()
        if not isinstance(now, (int, float)) or not math.isfinite(now):
            raise BackendError("TelegramReadClockUnavailable")
        rejected = False
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            authority = db.execute(
                "SELECT policy FROM read_authority WHERE identity=?", (self.identity,)
            ).fetchone()
            if authority:
                authoritative = read_policy(json.loads(authority[0]))
                self.policy = (
                    authoritative
                    if self.context
                    else {
                        key: (max if key == "window_seconds" else min)(value, authoritative[key])
                        for key, value in self.requested_policy.items()
                    }
                )
                limit = min(
                    self.policy["scan_rpc_limit"], self.scan_limit or self.policy["scan_rpc_limit"]
                )
                if self.scan_used + count > limit:
                    self.denied += count
                    raise ReadBudgetExceeded("TelegramReadScanBudgetExhausted")
            row = db.execute(
                "SELECT start,end,used FROM budgets WHERE identity=?", (self.identity,)
            ).fetchone()
            start, end, used = row if row else (now, now + self.policy["window_seconds"], 0)
            if now < start:
                raise BackendError("TelegramReadClockUnavailable")
            if now >= end:
                start, end, used = now, now + self.policy["window_seconds"], 0
            # A shorter policy cannot end the persisted active window early.
            end = max(end, start + self.policy["window_seconds"])
            rejected = used + count > self.policy["requests_per_window"]
            if not rejected:
                used += count
            db.execute(
                "INSERT INTO budgets VALUES(?,?,?,?) ON CONFLICT(identity) DO UPDATE SET "
                "start=excluded.start,end=excluded.end,used=excluded.used",
                (self.identity, start, end, used),
            )
            self.budget_used, self.budget_end = used, end
        if rejected:
            self.denied += count
            raise ReadBudgetExceeded("TelegramReadAccountBudgetExhausted", end - now)
        self.scan_used += count

    def get(self, kind, key):
        self.refresh_policy()
        if kind not in {"entity", "directory", "discovery_metadata"}:
            raise ValueError("Unsupported Telegram read cache kind")
        if not self.policy["cache_ttl_seconds"]:
            self.cache_misses += 1
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT data,observed FROM read_cache WHERE scope=? AND kind=? AND key=?",
                (self.scope, kind, digest(key)),
            ).fetchone()
        now = self.clock()
        if not row or not 0 <= now - row[1] < self.policy["cache_ttl_seconds"]:
            self.cache_misses += 1
            return None
        try:
            value = json.loads(row[0])
        except (ValueError, TypeError):
            raise BackendError("TelegramReadCacheInvalid") from None
        self.cache_hits += 1
        self.cache_observed_at = min(self.cache_observed_at or row[1], row[1])
        return value, row[1]

    def put(self, kind, key, value, *, observed_at=None):
        if kind not in {"entity", "directory", "discovery_metadata"}:
            raise ValueError("Unsupported Telegram read cache kind")
        if not self.policy["cache_ttl_seconds"]:
            return
        observed = self.clock() if observed_at is None else observed_at
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO read_cache VALUES(?,?,?,?,?)",
                (self.scope, kind, digest(key), observed, canonical(value)),
            )

    def issue_cursor(self, value):
        if value.get("scope") != self.scope:
            raise ValueError("Invalid Telegram cursor scope")
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO read_cursors VALUES(?,?,?)",
                (self.scope, digest(value), canonical(value)),
            )
        return value

    def valid_cursor(self, value):
        if not isinstance(value, dict) or value.get("scope") != self.scope:
            return False
        return self.registered_cursor(value)

    def registered_cursor(self, value):
        if not isinstance(value, dict) or value.get("identity") != self.identity:
            return False
        with self.connect() as db:
            row = db.execute(
                "SELECT data FROM read_cursors WHERE scope=? AND key=?",
                (value.get("scope"), digest(value)),
            ).fetchone()
        return bool(row and row[0] == canonical(value))

    def put_snapshot(self, key, value):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO read_snapshots VALUES(?,?,?)",
                (self.scope, key, canonical(value)),
            )

    def get_snapshot(self, key):
        with self.connect() as db:
            row = db.execute(
                "SELECT data FROM read_snapshots WHERE scope=? AND key=?", (self.scope, key)
            ).fetchone()
        if not row:
            raise BackendError("TelegramReadSnapshotUnavailable")
        try:
            return json.loads(row[0])
        except (ValueError, TypeError):
            raise BackendError("TelegramReadSnapshotUnavailable") from None

    def diagnostics(self):
        return {
            "read_rpc_budget_reserved": self.scan_used,
            "read_rpc_budget_rejected": self.denied,
            "read_rpc_budget_used": self.budget_used,
            "read_rpc_budget_limit": self.policy["requests_per_window"],
            "read_rpc_budget_resets_at": self.budget_end,
            "read_rpc_budget_scope": "local_profile_identity",
            "read_rpc_budget_authority": "private_sqlite",
            "read_cache_hits": self.cache_hits,
            "read_cache_misses": self.cache_misses,
            "read_cache_observed_at": self.cache_observed_at,
        }
