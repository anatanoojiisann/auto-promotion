"""Renewable, process-safe account lanes shared by sending and read operations."""

import threading
import time
import uuid
from contextlib import contextmanager


class AccountBusy(RuntimeError):
    safe_code = "AccountBusy"


class AccountLanes:
    def __init__(self, store, *, lease_seconds=60):
        self.store = store
        self.lease_seconds = lease_seconds
        self.local = threading.local()

    @contextmanager
    def acquire(self, account_id, limits=None):
        held = getattr(self.local, "held", {})
        if account_id in held:
            yield held[account_id]
            return
        owner, now = uuid.uuid4().hex, time.time()
        with self.store.connect(write=True) as db:
            row = db.execute(
                "SELECT * FROM account_lanes WHERE account_id=?", (account_id,)
            ).fetchone()
            if row and row["expires_at"] > now:
                raise AccountBusy()
            if limits:
                platform = db.execute(
                    "SELECT platform FROM accounts WHERE id=?", (account_id,)
                ).fetchone()[0]
                active = db.execute(
                    "SELECT a.platform FROM account_lanes l JOIN accounts a ON a.id=l.account_id WHERE l.expires_at>?",
                    (now,),
                ).fetchall()
                if len(active) >= limits["max_workers"] or sum(
                    r[0] == platform for r in active
                ) >= limits["platform_workers"].get(platform, limits["max_workers"]):
                    raise AccountBusy()
            db.execute(
                "INSERT INTO account_lanes VALUES(?,?,?,?) ON CONFLICT(account_id) "
                "DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at,heartbeat_at=excluded.heartbeat_at",
                (account_id, owner, now + self.lease_seconds, now),
            )
        self.local.held = {**held, account_id: owner}
        stop = threading.Event()

        def pulse():
            while not stop.wait(min(10, self.lease_seconds / 3)):
                try:
                    at = time.time()
                    with self.store.connect(write=True) as db:
                        db.execute(
                            "UPDATE account_lanes SET expires_at=?,heartbeat_at=? WHERE account_id=? AND owner=?",
                            (at + self.lease_seconds, at, account_id, owner),
                        )
                except Exception:
                    # Losing the lane cannot authorize replay of a claimed write.
                    pass

        thread = threading.Thread(target=pulse, daemon=True, name="account-lane")
        thread.start()
        try:
            yield owner
        finally:
            stop.set()
            thread.join(timeout=20)
            self.local.held = held
            with self.store.connect(write=True) as db:
                db.execute(
                    "DELETE FROM account_lanes WHERE account_id=? AND owner=?", (account_id, owner)
                )
