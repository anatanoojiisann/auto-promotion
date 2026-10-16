"""Independent persistent processes for platform/connection pools."""

import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .cloud_control import CloudControl, CloudJobClosed, CloudJournal, CloudUnavailable
from .models import canonical
from .queue_store import JobQueue, job_scope
from .routing import POOLS
from .store import Store


class PoolScheduler:
    def __init__(self, engine, *, popen=subprocess.Popen):
        self.engine, self.queue, self.popen = engine, JobQueue(engine), popen
        self.node = os.getenv("AP_NODE_ID", socket.gethostname())
        self.children, self.handles = {}, {}
        self.last_sync = 0
        self.cloud = CloudControl.configured()
        self.closed = False

    def enable(self):
        with self.engine.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO meta VALUES('execution_runtime','pooled') ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
            Store.audit(
                db,
                "execution.pools_enabled",
                {"node_id": self.node, "live_switch_changed": False, "global_stop_changed": False},
            )
            if self.cloud:
                db.execute(
                    "INSERT INTO meta VALUES('cloud_control_required','1') ON CONFLICT(key) DO UPDATE SET value='1'"
                )

    def tick(self):
        if self.closed:
            return
        self.queue.sync()
        self.engine.recover()
        required = self.engine.store.one(
            "SELECT value FROM meta WHERE key='cloud_control_required'"
        )
        if required and required["value"] == "1" and not self.cloud:
            raise CloudUnavailable("CloudControlConfigurationMissing")
        if self.cloud:
            try:
                self.cloud.request("/v1/heartbeat", {})
                self.cloud.flush_settings(self.engine)
            except CloudUnavailable as exc:
                self._cloud_health(str(exc))
                return self.queue.pools()
            CloudJournal(self.engine, self.cloud).flush()
            if self.engine.clock() - self.last_sync >= 15:
                try:
                    self.cloud.sync(self.engine)
                except CloudUnavailable as exc:
                    self._cloud_health(str(exc))
                    return self.queue.pools()
                self.last_sync = self.engine.clock()
                self._cloud_health("connected")
        for key, child in list(self.children.items()):
            if child.poll() is not None:
                with self.engine.store.connect(write=True) as db:
                    Store.audit(
                        db,
                        "pool.process_exit",
                        {"pool": key[0], "pid": child.pid, "exit_code": child.returncode},
                    )
                    db.execute(
                        "UPDATE pool_workers SET state='stopped',reason='observed_process_exit' WHERE pid=? AND state='running'",
                        (child.pid,),
                    )
                for handle in self.handles.pop(key, []):
                    handle.close()
                del self.children[key]
        settings = self.engine.execution_settings()
        now = self.engine.clock()
        pools = self.engine.store.rows(
            "SELECT pool,MIN(available_at) oldest FROM execution_jobs WHERE state='pending' AND available_at<=? AND expires_at>? GROUP BY pool ORDER BY oldest,pool",
            (now + 2, now),
        )
        allowed = set(os.getenv("AP_EXECUTOR_POOLS", "").split(",")) - {""}
        folder = Path(self.engine.store.path).parent / "pool-logs"
        folder.mkdir(exist_ok=True)
        for row in pools:
            pool = row["pool"]
            if pool not in POOLS or (allowed and pool not in allowed):
                continue
            cap = settings.get("pool_workers", {}).get(pool, 1)
            active = self.engine.store.one(
                "SELECT COUNT(*) n FROM pool_workers WHERE pool=? AND state='running' AND heartbeat_at>?",
                (pool, now - 45),
            )["n"]
            owned = sum(key[0] == pool for key in self.children)
            spawn_budget = max(0, cap - max(active, owned))
            for slot in range(cap):
                key = (pool, slot)
                if (
                    not spawn_budget
                    or key in self.children
                    or len(self.children) >= min(32, settings["max_workers"] * 2)
                ):
                    continue
                # No account data or credentials are put on the command line or in logs.
                handles = [
                    open(folder / f"{pool}-{slot}.{suffix}.log", "ab") for suffix in ("out", "err")
                ]
                argv = [
                    sys.executable,
                    "-m",
                    "auto_promotion.cli",
                    "--db",
                    self.engine.store.path,
                    "pool-worker",
                    "--pool",
                    pool,
                    "--node-id",
                    self.node,
                ]
                child = self.popen(
                    argv,
                    cwd=Path(__file__).resolve().parents[2],
                    stdout=handles[0],
                    stderr=handles[1],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                self.children[key], self.handles[key] = child, handles
                spawn_budget -= 1
        return self.queue.pools()

    def _cloud_health(self, state):
        with self.engine.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO meta VALUES('cloud_control_health',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (canonical({"state": state, "at": self.engine.clock()}),),
            )

    def close(self, timeout=210):
        # Do not terminate a writer at shutdown. Consumers exit after their bounded idle
        # timeout, after finishing the current guarded operation.
        self.closed = True
        end = time.monotonic() + timeout
        for child in self.children.values():
            try:
                child.wait(timeout=max(0, end - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
        for handles in self.handles.values():
            for handle in handles:
                handle.close()


def run_pool(engine, pool, node_id, *, idle_exit=30, max_jobs=None):
    if pool not in POOLS:
        raise ValueError("UnknownExecutionPool")
    queue = JobQueue(engine)
    cloud = CloudControl.configured()
    journal = CloudJournal(engine, cloud) if cloud else None
    required = engine.store.one("SELECT value FROM meta WHERE key='cloud_control_required'")
    if required and required["value"] == "1" and not cloud:
        raise CloudUnavailable("CloudControlConfigurationMissing")
    owner, now, handled = "pool-" + uuid.uuid4().hex, time.time(), 0
    with engine.store.connect(write=True) as db:
        db.execute(
            "INSERT INTO pool_workers VALUES(?,?,?,?,?,?,?,?)",
            (owner, pool, node_id, os.getpid(), now, now, "running", None),
        )
    idle_since = time.monotonic()
    try:
        while max_jobs is None or handled < max_jobs:
            with engine.store.connect(write=True) as db:
                db.execute(
                    "UPDATE pool_workers SET heartbeat_at=? WHERE id=?", (time.time(), owner)
                )
            job = queue.claim(pool, owner)
            if job is None:
                if time.monotonic() - idle_since >= idle_exit:
                    break
                time.sleep(1)
                continue
            error = None
            lease = None
            try:
                if cloud:
                    lease = cloud.claim(pool, job["id"])
                    if not lease:
                        queue.settle(job)
                        time.sleep(1)
                        continue
                    journal.remember(lease)
                    if any(
                        lease[key] != job[key]
                        for key in (
                            "id",
                            "kind",
                            "reference_id",
                            "task_id",
                            "account_id",
                            "pool",
                            "version",
                        )
                    ):
                        raise CloudUnavailable("CloudManifestMismatch")
                with (
                    job_scope(job),
                    queue.heartbeat(job, (lambda: cloud.renew(lease)) if cloud else None),
                    engine.adapters.execution_job(
                        {"id": job["id"], "owner": owner, "cloud": lease}
                    ),
                ):
                    engine.run_reference_job(job)
            except CloudJobClosed as exc:
                with engine.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE execution_jobs SET state=?,owner=NULL,lease_until=0,result=? WHERE id=? AND owner=?",
                        (
                            exc.state,
                            canonical({"reason": "cloud_job_already_settled"}),
                            job["id"],
                            owner,
                        ),
                    )
            except Exception as exc:
                error = exc
            finally:
                queue.settle(job, error)
                if lease:
                    journal.finish(lease)
            handled += 1
            idle_since = time.monotonic()
    finally:
        with engine.store.connect(write=True) as db:
            db.execute(
                "UPDATE pool_workers SET state='stopped',heartbeat_at=? WHERE id=?",
                (time.time(), owner),
            )
    return {"pool": pool, "processed_jobs": handled}
