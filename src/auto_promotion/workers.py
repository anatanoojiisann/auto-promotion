"""Durable sender heartbeats and observed process exits; never authorizes publication."""

import os
import subprocess
import threading
import time
import uuid

from .execution_diagnostics import exception_details, safe_code
from .models import canonical
from .store import Store, decode

HEARTBEAT_INTERVAL = 10
STALE_AFTER = 60


class Workers:
    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock

    def start(self, task_id=None, *, live_enabled=False, pid=None):
        run_id, now = "worker-" + uuid.uuid4().hex, self.clock()
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO worker_runs(id,task_id,pid,live_enabled,started_at,heartbeat_at,state,phase) "
                "VALUES(?,?,?,?,?,?,'running','starting')",
                (run_id, task_id, pid or os.getpid(), int(live_enabled), now, now),
            )
            Store.audit(
                db, "worker.started", {"worker_id": run_id, "pid": pid or os.getpid()}, task_id, now
            )
        return run_id

    def heartbeat(self, run_id, phase=None):
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE worker_runs SET heartbeat_at=?,phase=COALESCE(?,phase) WHERE id=? AND state='running'",
                (self.clock(), safe_code(phase, "unknown") if phase else None, run_id),
            )

    def finish(self, run_id, reason="normal_exit", exit_code=0, diagnostics=None):
        now = self.clock()
        with self.store.connect(write=True) as db:
            row = db.execute(
                "SELECT task_id,state FROM worker_runs WHERE id=?", (run_id,)
            ).fetchone()
            if not row or row["state"] != "running":
                return
            state = "finished" if exit_code == 0 else "failed"
            db.execute(
                "UPDATE worker_runs SET state=?,finished_at=?,reason=?,exit_code=?,diagnostics=? WHERE id=?",
                (
                    state,
                    now,
                    safe_code(reason, "process_exit"),
                    exit_code,
                    canonical(diagnostics or {}),
                    run_id,
                ),
            )
            Store.audit(
                db,
                "worker.finished",
                {"worker_id": run_id, "reason": safe_code(reason), "exit_code": exit_code},
                row["task_id"],
                now,
            )

    def snapshot(self, task_id=None, *, live=False):
        # A healthy global sender covers the task; a task-specific sender covers only its task.
        clause, params = (
            ("task_id IS NULL", ())
            if task_id is None
            else ("(task_id=? OR task_id IS NULL)", (task_id,))
        )
        rows = self.store.rows(
            "SELECT * FROM worker_runs WHERE "
            + clause
            + (" AND live_enabled=1" if live else "")
            + " ORDER BY started_at DESC LIMIT 100",
            params,
        )
        fresh = [
            r
            for r in rows
            if r["state"] == "running" and self.clock() - r["heartbeat_at"] <= STALE_AFTER
        ]
        if not rows:
            return {
                "status": "unmonitored",
                "last_heartbeat": None,
                "reason": None,
                "exit_code": None,
            }
        row = decode((fresh or rows)[0], "diagnostics")
        status = "running" if fresh else "stale" if row["state"] == "running" else "stopped"
        return {
            **row,
            "status": status,
            "last_heartbeat": row["heartbeat_at"],
            "reason": "heartbeat_expired" if status == "stale" else row["reason"],
        }


class WorkerRun:
    def __init__(self, store, task_id=None, *, live_enabled=False, run_id=None):
        self.workers = Workers(store)
        self.task_id, self.live_enabled, self.run_id = task_id, live_enabled, run_id
        self.stop = threading.Event()
        self.thread = None
        self.reason = "normal_exit"

    def __enter__(self):
        if self.run_id:
            with self.workers.store.connect(write=True) as db:
                row = db.execute("SELECT * FROM worker_runs WHERE id=?", (self.run_id,)).fetchone()
                if (
                    not row
                    or row["task_id"] != self.task_id
                    or row["state"] != "running"
                    or bool(row["live_enabled"]) != self.live_enabled
                ):
                    raise ValueError("WorkerRunMismatch")
                db.execute("UPDATE worker_runs SET pid=? WHERE id=?", (os.getpid(), self.run_id))
        else:
            self.run_id = self.workers.start(self.task_id, live_enabled=self.live_enabled)
        self.workers.heartbeat(self.run_id)
        self.thread = threading.Thread(target=self._pulse, daemon=True, name="sender-heartbeat")
        self.thread.start()
        return self

    def _pulse(self):
        while not self.stop.wait(HEARTBEAT_INTERVAL):
            try:
                self.workers.heartbeat(self.run_id)
            except Exception:
                # A failed heartbeat is visible as stale; it must not change sending permissions.
                pass

    def phase(self, value):
        self.workers.heartbeat(self.run_id, value)

    def __exit__(self, kind, exc, trace):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=20)
        self.workers.finish(
            self.run_id,
            self.reason if exc is None else safe_code(type(exc).__name__),
            0 if exc is None else 1,
            exception_details(exc) if exc else None,
        )


def supervise(store, task_id, argv, *, live_enabled=False):
    """Observe a child exit, including termination that bypasses the child's finally block."""
    workers = Workers(store)
    run_id = workers.start(task_id, live_enabled=live_enabled)
    env = {**os.environ, "AP_WORKER_RUN_ID": run_id}
    child = None
    try:
        child = subprocess.Popen(argv, env=env)
        with store.connect(write=True) as db:
            db.execute("UPDATE worker_runs SET pid=? WHERE id=?", (child.pid, run_id))
        code = child.wait()
        # Keep a graceful child's recorded reason, but fill in the actual OS exit code.
        workers.finish(run_id, "process_exit", code)
        with store.connect(write=True) as db:
            db.execute("UPDATE worker_runs SET exit_code=? WHERE id=?", (code, run_id))
        return code
    except BaseException as exc:
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait(timeout=30)
        workers.finish(
            run_id,
            safe_code(type(exc).__name__),
            child.returncode if child else 1,
            exception_details(exc),
        )
        raise
