"""Task-selected channels with durable, per-account, fail-closed handoffs.

Legacy tasks keep their original global default. A managed account is fenced
locally before any server request; lost responses never reopen its old channel.
"""

import json
import os
import socket
import uuid

from .cloud_control import CloudControl, CloudJournal, CloudUnavailable
from .models import epoch
from .store import Store


def requested(policy):
    return policy.get("execution_channel", "inherit")


def managed(db, task_id, account_id):
    task = db.execute("SELECT policy FROM tasks WHERE id=?", (task_id,)).fetchone()
    return bool(
        (task and requested(json.loads(task[0])) != "inherit")
        or db.execute(
            "SELECT 1 FROM account_task_channels WHERE account_id=?", (account_id,)
        ).fetchone()
    )


def pending_managed(engine):
    return bool(
        engine.store.one(
            "SELECT 1 FROM execution_jobs j JOIN tasks t ON t.id=j.task_id "
            "WHERE j.state='pending' AND t.state='active' AND j.expires_at>? AND "
            "(COALESCE(json_extract(t.policy,'$.execution_channel'),'inherit')<>'inherit' "
            "OR EXISTS(SELECT 1 FROM account_task_channels a WHERE a.account_id=j.account_id)) LIMIT 1",
            (engine.clock(),),
        )
    )


def effective(engine, policy):
    from .execution_channels import read_state

    channel = requested(policy)
    if channel != "inherit":
        return channel
    state = read_state(engine.store)
    if state.get("active") == "local" and state.get("until", 0) <= engine.clock():
        return "cloud"
    return state.get("active", "cloud")


def handoff_blocks(db, authority, task_id, now):
    if not authority or authority["state"] != "handoff" or authority["task_id"] == task_id:
        return False
    owner = db.execute(
        "SELECT state,policy FROM tasks WHERE id=?", (authority["task_id"],)
    ).fetchone()
    return bool(
        owner and owner["state"] == "active" and epoch(json.loads(owner["policy"])["ends_at"]) > now
    )


def valid_context(db, context, *, now):
    if not context or not context.get("task_channel"):
        return False
    row = db.execute(
        "SELECT a.*,j.owner job_owner,j.state job_state,j.lease_until,t.policy,c.owner claim_owner,"
        "c.generation claim_generation,c.channel claim_channel FROM execution_jobs j "
        "JOIN account_task_channels a ON a.account_id=j.account_id "
        "JOIN task_channel_claims c ON c.job_id=j.id JOIN tasks t ON t.id=j.task_id WHERE j.id=?",
        (context.get("id"),),
    ).fetchone()
    return bool(
        row
        and row["state"] == "ready"
        and row["job_state"] == "leased"
        and row["lease_until"] > now
        and row["job_owner"] == row["claim_owner"] == context.get("owner")
        and row["generation"] == row["claim_generation"] == context.get("generation")
        and row["channel"] == row["claim_channel"] == context.get("channel")
        and requested(json.loads(row["policy"])) in {"inherit", context.get("channel")}
        and (row["channel"] == "cloud" or row["until_at"] > now)
    )


def guard_context(engine, context):
    with engine.store.connect() as db:
        if not valid_context(db, context, now=engine.clock()):
            raise CloudUnavailable("TaskChannelClaimExpired")


def snapshot(engine, task):
    rows = engine.store.rows(
        "SELECT account_id,channel,state,until_at,error,retry_at,updated_at "
        "FROM account_task_channels WHERE account_id IN ("
        + ",".join("?" for _ in task["account_ids"])
        + ")",
        tuple(task["account_ids"]),
    )
    return {
        "requested": requested(task["policy"]),
        "default": effective(engine, {"execution_channel": "inherit"}),
        "accounts": rows,
    }


class TaskChannelRouter:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def handoff_tasks(self, job, prior):
        from .execution_channels import read_state

        task_ids = {job["task_id"]}
        task_ids.update(
            r["task_id"]
            for r in self.store.rows(
                "SELECT DISTINCT j.task_id FROM execution_jobs j JOIN task_channel_claims c ON c.job_id=j.id "
                "WHERE j.account_id=? AND c.channel='local'",
                (job["account_id"],),
            )
        )
        legacy = read_state(self.store)
        if job["account_id"] in legacy.get("account_ids", []):
            task_ids.update(
                r["task_id"]
                for r in self.store.rows(
                    "SELECT task_id FROM attempts WHERE account_id=? AND started_at>=? UNION "
                    "SELECT task_id FROM join_attempts WHERE account_id=? AND started_at>=?",
                    (
                        job["account_id"],
                        legacy.get("since", 0),
                        job["account_id"],
                        legacy.get("since", 0),
                    ),
                )
            )
        if prior and prior["channel"] == "local":
            task_ids.add(prior["task_id"])
        return sorted(task_ids)

    def prepare(self, job):
        from .execution_channels import binding, independent_active, read_state

        now = self.engine.clock()
        task = self.store.one("SELECT policy FROM tasks WHERE id=?", (job["task_id"],))
        policy = json.loads(task["policy"])
        channel = effective(self.engine, policy)
        until = epoch(policy["ends_at"])
        cloud = CloudControl.configured()
        node_id = cloud.node_id if cloud else os.getenv("AP_NODE_ID", socket.gethostname())
        store_id = binding(self.engine)
        prior = self.store.one(
            "SELECT * FROM account_task_channels WHERE account_id=?", (job["account_id"],)
        )
        if prior and prior["retry_at"] > now:
            raise CloudUnavailable(prior["error"] or "TaskChannelHandoffPending")
        with self.store.connect() as db:
            if handoff_blocks(db, prior, job["task_id"], now):
                raise CloudUnavailable("TaskChannelHandoffPending")
        if prior and prior["state"] == "handoff" and prior["task_id"] == job["task_id"]:
            channel, until = prior["channel"], prior["until_at"]
        if cloud and self.store.one(
            "SELECT 1 FROM cloud_receipt_outbox WHERE json_extract(lease,'$.account_id')=? LIMIT 1",
            (job["account_id"],),
        ):
            CloudJournal(self.engine, cloud).flush(account_id=job["account_id"])
        reusable = bool(
            prior
            and prior["state"] == "ready"
            and prior["channel"] == channel
            and (channel == "cloud" or prior["until_at"] >= until)
        )
        if reusable:
            generation = prior["generation"]
            if channel == "cloud":
                if not cloud:
                    raise CloudUnavailable("CloudControlConfigurationMissing")
                cloud.sync(self.engine, task_ids=[job["task_id"]])
        else:
            with self.store.connect(write=True) as db:
                current = db.execute(
                    "SELECT * FROM execution_jobs WHERE id=?", (job["id"],)
                ).fetchone()
                if not current or current["owner"] != job["owner"] or current["lease_until"] <= now:
                    raise CloudUnavailable("PersistentJobClaimExpired")
                if db.execute(
                    "SELECT 1 FROM execution_jobs WHERE account_id=? AND state='leased' "
                    "AND lease_until>? AND id<>?",
                    (job["account_id"], now, job["id"]),
                ).fetchone():
                    raise CloudUnavailable("TaskChannelHandoffBusy")
                if db.execute(
                    "SELECT 1 FROM cloud_receipt_outbox r JOIN execution_jobs j ON j.id=r.job_id "
                    "WHERE j.account_id=? LIMIT 1",
                    (job["account_id"],),
                ).fetchone():
                    raise CloudUnavailable("LocalChannelReceiptPending")
                generation = (
                    prior["generation"]
                    if prior and prior["state"] == "handoff" and prior["task_id"] == job["task_id"]
                    else uuid.uuid4().hex
                )
                db.execute(
                    "INSERT INTO account_task_channels VALUES(?,?,?,?,?,'handoff','pending',?,NULL,0) "
                    "ON CONFLICT(account_id) DO UPDATE SET channel=excluded.channel,generation=excluded.generation,"
                    "until_at=excluded.until_at,task_id=excluded.task_id,state='handoff',fence_source='pending',"
                    "updated_at=excluded.updated_at,error=NULL,retry_at=0",
                    (job["account_id"], channel, generation, until, job["task_id"], now),
                )
                Store.audit(
                    db,
                    "execution.account_handoff_started",
                    {"account_id": job["account_id"], "channel": channel, "generation": generation},
                    job["task_id"],
                    now,
                )
            try:
                required = self.store.one(
                    "SELECT value FROM meta WHERE key='cloud_control_required'"
                )
                legacy = read_state(self.store)
                legacy_fence = (
                    not prior
                    and channel == "local"
                    and independent_active(self.engine)
                    and job["account_id"] in legacy.get("account_ids", [])
                    and legacy.get("until", 0) >= until
                )
                standalone = not cloud and not (required and required["value"] == "1")
                if channel == "local" and (legacy_fence or standalone):
                    fence_source = "legacy" if legacy_fence else "standalone"
                else:
                    if not cloud:
                        raise CloudUnavailable("CloudControlConfigurationMissing")
                    # The server may already have accepted this generation before
                    # a response was lost. Read it before any new transition.
                    observed = cloud.request(
                        "/v1/account-channel/status", {"account_id": job["account_id"]}
                    ).get("channel")
                    if not (observed and observed.get("generation") == generation):
                        cloud.flush_settings(self.engine)
                        CloudJournal(self.engine, cloud).flush(account_id=job["account_id"])
                        cloud.sync(self.engine, task_ids=self.handoff_tasks(job, prior))
                        observed = cloud.request(
                            "/v1/account-channel",
                            {
                                "local_store_id": store_id,
                                "account_id": job["account_id"],
                                "channel": channel,
                                "generation": generation,
                                "until": until,
                                "node_id": node_id,
                                "task_id": job["task_id"],
                                "expected_generation": observed.get("generation")
                                if observed
                                else None,
                            },
                            operator=True,
                        ).get("channel")
                    if (
                        not observed
                        or observed.get("generation") != generation
                        or observed.get("channel") != channel
                    ):
                        raise CloudUnavailable("TaskChannelHandoffUnverified")
                    fence_source = "cloud_acknowledged"
                with self.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE account_task_channels SET state='ready',fence_source=?,updated_at=?,"
                        "error=NULL,retry_at=0 WHERE account_id=? AND generation=?",
                        (fence_source, self.engine.clock(), job["account_id"], generation),
                    )
                    Store.audit(
                        db,
                        "execution.account_channel_changed",
                        {
                            "account_id": job["account_id"],
                            "channel": channel,
                            "generation": generation,
                        },
                        job["task_id"],
                        self.engine.clock(),
                    )
            except Exception as exc:
                code = str(exc) if isinstance(exc, CloudUnavailable) else type(exc).__name__
                with self.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE account_task_channels SET error=?,retry_at=? WHERE account_id=? AND generation=?",
                        (code, self.engine.clock() + 30, job["account_id"], generation),
                    )
                raise
        context = {
            "id": job["id"],
            "owner": job["owner"],
            "task_channel": True,
            "channel": channel,
            "generation": generation,
            "node_id": node_id,
            "store_id": store_id,
        }
        with self.store.connect(write=True) as db:
            current = db.execute(
                "SELECT owner,state,lease_until FROM execution_jobs WHERE id=?", (job["id"],)
            ).fetchone()
            if (
                not current
                or current["owner"] != job["owner"]
                or current["state"] != "leased"
                or current["lease_until"] <= self.engine.clock()
            ):
                raise CloudUnavailable("PersistentJobClaimExpired")
            db.execute(
                "INSERT INTO task_channel_claims VALUES(?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET "
                "owner=excluded.owner,generation=excluded.generation,channel=excluded.channel",
                (job["id"], job["owner"], generation, channel),
            )
        guard_context(self.engine, context)
        return context, cloud
