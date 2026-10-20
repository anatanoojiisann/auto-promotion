"""Independent persistent processes for platform/connection pools."""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import closing
from pathlib import Path

from .cloud_control import CloudControl, CloudJobClosed, CloudJournal, CloudUnavailable
from .execution_channels import (
    activate_local,
    check_standby,
    enabled,
    independent_active,
    independent_scope,
    local_account_scope,
    local_active,
    read_state,
    use_cloud,
)
from .models import canonical
from .queue_store import JobQueue, executor_task_ids, job_scope
from .routing import POOLS
from .store import Store
from .task_channels import TaskChannelRouter, managed, pending_managed


class PoolScheduler:
    def __init__(self, engine, *, popen=subprocess.Popen):
        self.engine, self.queue, self.popen = engine, JobQueue(engine), popen
        self.node = os.getenv("AP_NODE_ID", socket.gethostname())
        self.children, self.handles = {}, {}
        self.last_sync = 0
        self.last_heartbeat = None
        self.cloud = None if independent_active(engine) else CloudControl.configured()
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
        independent = independent_active(self.engine)
        if not independent and not self.cloud:
            self.cloud = CloudControl.configured()
        mixed = pending_managed(self.engine)
        if (
            required
            and required["value"] == "1"
            and not self.cloud
            and not independent
            and not mixed
        ):
            raise CloudUnavailable("CloudControlConfigurationMissing")
        if not mixed and not independent and self.cloud and not self._control_ready():
            return self.queue.pools()
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
        task_ids = executor_task_ids()
        sql = (
            "SELECT pool,MIN(available_at) oldest FROM execution_jobs "
            "WHERE state='pending' AND available_at<=? AND expires_at>?"
        )
        params = [now + 2, now]
        with self.engine.store.connect() as db:
            accounts = local_account_scope(db, now)
        if accounts is not None:
            sql += " AND (account_id IN (" + (",".join("?" for _ in accounts) or "NULL") + ") "
            sql += "OR task_id IN (SELECT id FROM tasks WHERE COALESCE(json_extract(policy,'$.execution_channel'),'inherit')<>'inherit') "
            sql += "OR account_id IN (SELECT account_id FROM account_task_channels))"
            params.extend(accounts)
        if task_ids is not None:
            if not task_ids:
                return self.queue.pools()
            sql += " AND task_id IN (" + ",".join("?" for _ in task_ids) + ")"
            params.extend(task_ids)
        sql += " GROUP BY pool ORDER BY oldest,pool"
        pools = self.engine.store.rows(sql, params)
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

    def _try_local(self, retry):
        now = self.engine.clock()
        if now < getattr(self, "next_fallback_check", 0):
            return False
        health = self.engine.store.one("SELECT value FROM meta WHERE key='cloud_control_health'")
        health = json.loads(health["value"]) if health else {}
        with self.engine.store.connect() as db:
            independent = independent_scope(db)
        if (
            not independent
            and health.get("state") == "LocalChannelCheckpointRequired"
            and health.get("retry_at", 0) > now
        ):
            return False
        try:
            return activate_local(self.engine, self.cloud, retry)
        except CloudUnavailable as exc:
            self.next_fallback_check = (
                retry["until"] if str(exc) == "LocalChannelCheckpointRequired" else now + 30
            )
            self._cloud_health(str(exc), execution_ready=False, retry_at=self.next_fallback_check)
            return False

    def _control_ready(self):
        if local_active(self.engine, self.cloud):
            return True
        previous = read_state(self.engine.store)
        pending = self.engine.store.one("SELECT value FROM meta WHERE key='cloud_retry_after'")
        retry = json.loads(pending["value"]) if pending else None
        now = self.engine.clock()
        if retry and retry["until"] > now:
            return self._try_local(retry)
        # Drain local work before uploading its outcomes and quota ledger. Workers
        # cannot claim cloud jobs until use_cloud commits after a successful sync.
        returning = previous.get("active") == "local"
        if returning and self.engine.store.one(
            "SELECT id FROM execution_jobs WHERE state='leased' AND lease_until>? LIMIT 1", (now,)
        ):
            return False
        try:
            active = self.engine.store.one(
                "SELECT id FROM execution_jobs WHERE (state='leased' OR "
                "(state='pending' AND available_at<=? AND expires_at>?)) LIMIT 1",
                (now + 60, now),
            )
            interval = 60 if active else 300
            contact = getattr(self.cloud, "last_contact_monotonic", None)
            recent = isinstance(contact, (int, float)) and time.monotonic() - contact < interval
            if (
                self.last_heartbeat is None
                or retry
                or (now - self.last_heartbeat >= interval and not recent)
            ):
                self.cloud.request("/v1/heartbeat", {})
                self.last_heartbeat = now
            self.cloud.flush_settings(self.engine)
            CloudJournal(self.engine, self.cloud).flush()
            if returning or now - self.last_sync >= 15:
                self.cloud.sync(self.engine, force=returning)
                self.last_sync = now
            use_cloud(self.engine)
            self._cloud_health("connected")
            with self.engine.store.connect() as db:
                independent = independent_scope(db)
            if (
                enabled(self.engine.store)
                and not independent
                and now >= getattr(self, "next_checkpoint", 0)
            ):
                self.next_checkpoint = now + 60
                try:
                    check_standby(self.engine, self.cloud)
                except CloudUnavailable:
                    # Active cloud leases may defer a checkpoint until a quiet
                    # tick. This never changes the normal cloud authorization.
                    pass
            return True
        except CloudUnavailable as exc:
            retry = self._cloud_failure(exc)
            return self._try_local(retry)

    def _cloud_failure(self, error):
        now = self.engine.clock()
        previous = self.engine.store.one("SELECT value FROM meta WHERE key='cloud_retry_after'")
        previous = json.loads(previous["value"]) if previous else {}
        failures = previous.get("failures", 0) + 1 if previous.get("reason") == str(error) else 1
        delay = getattr(error, "retry_after_seconds", None)
        if not delay:
            delay = (
                (int(now // 86400) + 1) * 86400 + 30 - now
                if str(error) == "CloudWriteQuotaExceeded"
                else min(300, 15 * 2 ** min(failures - 1, 5))
            )
        value = {"until": now + delay, "reason": str(error), "failures": failures}
        with self.engine.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO meta VALUES('cloud_retry_after',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (canonical(value),),
            )
            if failures == 1:
                Store.audit(db, "cloud.execution_blocked", value, now=now)
        self._cloud_health(str(error), retry_at=now + delay, execution_ready=False)
        return value

    def _cloud_health(self, state, **details):
        with self.engine.store.connect(write=True) as db:
            if state == "connected":
                db.execute("DELETE FROM meta WHERE key='cloud_retry_after'")
            db.execute(
                "INSERT INTO meta VALUES('cloud_control_health',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (canonical({"state": state, "at": self.engine.clock(), **details}),),
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
    independent = independent_active(engine)
    cloud = None if independent else CloudControl.configured()
    journal = CloudJournal(engine, cloud) if cloud else None
    required = engine.store.one("SELECT value FROM meta WHERE key='cloud_control_required'")
    if (
        required
        and required["value"] == "1"
        and not cloud
        and not independent
        and not pending_managed(engine)
    ):
        raise CloudUnavailable("CloudControlConfigurationMissing")
    owner, now, handled = "pool-" + uuid.uuid4().hex, time.time(), 0
    with engine.store.connect(write=True) as db:
        db.execute(
            "INSERT INTO pool_workers VALUES(?,?,?,?,?,?,?,?)",
            (owner, pool, node_id, os.getpid(), now, now, "running", None),
        )
    idle_since = time.monotonic()
    next_heartbeat = 0
    try:
        while max_jobs is None or handled < max_jobs:
            # Preparation is read-only or transactional DB work. An exhausted
            # lock wait must not kill the pool or replay a platform invocation.
            try:
                if time.monotonic() >= next_heartbeat:
                    with engine.store.connect(write=True) as db:
                        db.execute(
                            "UPDATE pool_workers SET heartbeat_at=?,reason=NULL WHERE id=?",
                            (time.time(), owner),
                        )
                    next_heartbeat = time.monotonic() + 10
                local = independent_active(engine) or (
                    local_active(engine, cloud) if cloud else False
                )
                channel_state = read_state(engine.store)
                pending = engine.store.one("SELECT value FROM meta WHERE key='cloud_retry_after'")
                if (
                    not pending_managed(engine)
                    and not local
                    and (
                        channel_state.get("active") == "local"
                        or (pending and json.loads(pending["value"])["until"] > engine.clock())
                    )
                ):
                    break
                job = queue.claim(pool, owner)
            except Exception as exc:
                worker_error(engine, owner, exc)
                if time.monotonic() - idle_since >= idle_exit:
                    break
                time.sleep(1)
                continue
            if job is None:
                if time.monotonic() - idle_since >= idle_exit:
                    break
                time.sleep(1)
                continue
            error = None
            lease = None
            with engine.store.connect() as db:
                is_managed = managed(db, job["task_id"], job["account_id"])
            context = None
            try:
                if is_managed:
                    with queue.heartbeat(job):
                        context, cloud = TaskChannelRouter(engine).prepare(job)
                    local = context["channel"] == "local"
                    journal = CloudJournal(engine, cloud) if cloud else None
                if local:
                    if not is_managed and not independent_active(engine):
                        check_standby(engine, cloud, job)
                elif cloud:
                    if not is_managed and pending_managed(engine):
                        journal.flush(account_id=job["account_id"])
                        cloud.sync(engine, task_ids=[job["task_id"]])
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
                context = context or {"id": job["id"], "owner": owner}
                context["cloud"] = lease
                if local and not is_managed:
                    context.update(
                        {k: channel_state[k] for k in ("generation", "store_id", "node_id")}
                    )
                    context["channel"] = "local"
                with engine.store.connect(write=True) as db:
                    Store.audit(
                        db,
                        "execution.job_channel",
                        {
                            "job_id": job["id"],
                            "channel": "local" if local else "cloud" if cloud else "local_only",
                        },
                        task_id=job["task_id"],
                        now=engine.clock(),
                    )
                with (
                    job_scope(job),
                    queue.heartbeat(job, (lambda: cloud.renew(lease)) if lease else None),
                    engine.adapters.execution_job(context),
                ):
                    engine.run_reference_job(job)
            except CloudJobClosed as exc:
                try:
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
                except sqlite3.Error as persistence_error:
                    worker_error(engine, owner, persistence_error)
            except Exception as exc:
                error = exc
                if isinstance(exc, CloudUnavailable) and is_managed:
                    with engine.store.connect(write=True) as db:
                        db.execute(
                            "UPDATE account_task_channels SET error=?,retry_at=? WHERE account_id=?",
                            (str(exc), engine.clock() + 30, job["account_id"]),
                        )
                if isinstance(exc, CloudUnavailable) and not local and not is_managed:
                    try:
                        PoolScheduler(engine)._cloud_failure(exc)
                    except sqlite3.Error as persistence_error:
                        worker_error(engine, owner, persistence_error)
            finally:
                # Never run the job again to recover a failed receipt write.
                # Its attempt remains durable; lease recovery quarantines it.
                try:
                    queue.settle(job, error)
                except Exception as exc:
                    worker_error(engine, owner, exc)
                if lease:
                    try:
                        journal.finish(lease)
                    except Exception as exc:
                        worker_error(engine, owner, exc)
            handled += 1
            idle_since = time.monotonic()
    finally:
        try:
            with engine.store.connect(write=True) as db:
                db.execute(
                    "UPDATE pool_workers SET state='stopped',heartbeat_at=? WHERE id=?",
                    (time.time(), owner),
                )
        except sqlite3.Error:
            # A stale heartbeat remains visible after a database outage.
            pass
    return {"pool": pool, "processed_jobs": handled}


def worker_error(engine, owner, error):
    """Best-effort safe diagnostics; never log exception text or credentials."""
    try:
        # Diagnostics must not add another full lock retry budget during an
        # outage. This single best-effort statement never encloses execution.
        with closing(sqlite3.connect(engine.store.path, timeout=0, isolation_level=None)) as db:
            db.execute(
                "UPDATE pool_workers SET reason=? WHERE id=?",
                (type(error).__name__, owner),
            )
    except sqlite3.Error:
        pass
