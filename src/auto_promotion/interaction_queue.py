"""Stage jobs in the existing execution_jobs table, with fenced renewable leases."""

import threading
import uuid
from contextlib import contextmanager

from .models import canonical, digest


class LostLease(RuntimeError):
    pass


class InteractionQueue:
    def __init__(self, store, clock):
        self.store, self.clock = store, clock

    def put(self, db, stage, reference, *, at=None, version="1"):
        now = self.clock()
        key = "hf-" + digest([stage, reference])[:32]
        db.execute(
            "INSERT OR IGNORE INTO execution_jobs(id,kind,reference_id,pool,route,version,"
            "available_at,expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                key,
                "hf_" + stage,
                reference,
                "hf." + stage,
                "workflow",
                version,
                now if at is None else at,
                1e15,
                now,
                now,
            ),
        )
        return key

    def claim(self, stage, lease_seconds=120):
        now, owner = self.clock(), uuid.uuid4().hex
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE execution_jobs SET state='pending',owner=NULL WHERE kind=? "
                "AND state='leased' AND lease_until<=?",
                ("hf_" + stage, now),
            )
            row = db.execute(
                "SELECT * FROM execution_jobs WHERE kind=? AND state='pending' AND available_at<=? "
                "ORDER BY available_at,created_at,id LIMIT 1",
                ("hf_" + stage, now),
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE execution_jobs SET state='leased',owner=?,lease_until=?,updated_at=? WHERE id=?",
                (owner, now + lease_seconds, now, row["id"]),
            )
            return dict(
                db.execute("SELECT * FROM execution_jobs WHERE id=?", (row["id"],)).fetchone()
            )

    def guard(self, db, job):
        row = db.execute(
            "SELECT 1 FROM execution_jobs WHERE id=? AND owner=? AND state='leased' AND lease_until>?",
            (job["id"], job["owner"], self.clock()),
        ).fetchone()
        if not row:
            raise LostLease("WorkflowLeaseLost")

    def settle(self, db, job, *, retry_at=None, error=None, retry_count=0):
        self.guard(db, job)
        db.execute(
            "UPDATE execution_jobs SET state=?,available_at=?,owner=NULL,lease_until=0,"
            "updated_at=?,result=? WHERE id=? AND owner=?",
            (
                "done" if retry_at is None else "pending",
                retry_at or self.clock(),
                self.clock(),
                canonical({"error": error, "retry_count": retry_count}) if error else "{}",
                job["id"],
                job["owner"],
            ),
        )

    @contextmanager
    def heartbeat(self, job, seconds=120, interval=30):
        stop = threading.Event()

        def pulse():
            while not stop.wait(interval):
                try:
                    with self.store.connect(write=True) as db:
                        self.guard(db, job)
                        db.execute(
                            "UPDATE execution_jobs SET lease_until=?,updated_at=? WHERE id=? AND owner=?",
                            (self.clock() + seconds, self.clock(), job["id"], job["owner"]),
                        )
                except Exception:
                    return  # Every commit is fenced, including after a failed renewal.

        thread = threading.Thread(target=pulse, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=2)
