"""Opt-in development observations/draft checks using AP's persistent job journal.

No item attempt, quota, dedup, live switch, task channel or delivery status is changed.
The business engine owns explicit start, frozen manifest and account policy.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .core import Conflict
from .models import canonical, digest, epoch
from .store import Store


class Binding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    device_id: str
    identity_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    node_os: str = Field(default="any", pattern=r"^(any|windows|macos)$")


class PhoneConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: str = Field(pattern=r"^development$")
    enabled: bool = False
    fleet_url: str
    service_token: SecretStr
    authorized_task_ids: list[str]
    channel: str = Field(pattern=r"^(local|cloud)$")
    bindings: dict[str, Binding]


SCHEMA = """
CREATE TABLE IF NOT EXISTS phone_fleet_preparations(
 job_id TEXT PRIMARY KEY REFERENCES execution_jobs(id), request TEXT NOT NULL,
 digest TEXT NOT NULL, execution_id TEXT, dispatch TEXT NOT NULL DEFAULT 'ready',
 revision INTEGER NOT NULL DEFAULT 0, result TEXT NOT NULL DEFAULT '{}',
 UNIQUE(digest)
);
CREATE TABLE IF NOT EXISTS phone_fleet_event_consumers(
 consumer_id TEXT PRIMARY KEY, cursor INTEGER NOT NULL DEFAULT 0
);
"""


class PhonePreparations:
    def __init__(self, engine, config: PhoneConfig, client):
        if not config.enabled or config.mode != "development":
            raise Conflict("PhoneFleetDevelopmentDisabled")
        self.engine, self.store, self.config, self.client = engine, engine.store, config, client
        with self.store.connect(write=True) as db:
            # Explicit opt-in initialization, never added to the existing sender startup.
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)

    def validate(self, db, task_id, item_id):
        stopped = db.execute("SELECT value FROM meta WHERE key='global_stop'").fetchone()
        if not stopped or stopped[0] != "0":
            raise Conflict("PhoneFleetGlobalStop")
        if task_id not in self.config.authorized_task_ids:
            raise Conflict("PhoneFleetTaskNotInDevelopmentScope")
        task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        item = db.execute(
            "SELECT * FROM items WHERE id=? AND task_id=?", (item_id, task_id)
        ).fetchone()
        if not task or not item or task["state"] != "active" or task["phase"] != "ready":
            raise Conflict("PhoneFleetRequiresExplicitlyStartedTask")
        if not db.execute(
            "SELECT 1 FROM audit WHERE task_id=? AND kind='task.started'", (task_id,)
        ).fetchone():
            raise Conflict("PhoneFleetStartRecordMissing")
        policy = self.engine._integrity(db, task, "outbound")
        if item["status"] != "not_attempted" or item["attempt_id"]:
            raise Conflict("PhoneFleetItemAlreadyAttempted")
        account = db.execute("SELECT * FROM accounts WHERE id=?", (item["account_id"],)).fetchone()
        if account["disabled"] or account["cooldown_until"] > self.engine.clock():
            raise Conflict("PhoneFleetAccountBlocked")
        if db.execute(
            "SELECT 1 FROM account_blocks WHERE account_id=? AND state='active' AND (until_at IS NULL OR until_at>?)",
            (item["account_id"], self.engine.clock()),
        ).fetchone():
            raise Conflict("PhoneFleetAccountBlocked")
        if self.engine.clock() >= epoch(policy["ends_at"]):
            raise Conflict("PhoneFleetDeadlineExceeded")
        if policy.get("execution_method", "inherit") not in {"inherit", "phone"}:
            raise Conflict("PhoneFleetMethodMismatch")
        if item["account_id"] not in self.config.bindings:
            raise Conflict("PhoneFleetAccountBindingMissing")
        from .task_channels import effective

        if effective(self.engine, policy, task_id, item["account_id"]) != self.config.channel:
            raise Conflict("PhoneFleetChannelMismatch")
        # This is read-only preparation, not an offline write authorization. Outbound
        # will still require TaskChannelRouter, final permit/quota/dedup and once guard.
        return task, item, policy

    def enqueue(self, task_id, item_id, operation="observe"):
        if operation not in {"observe", "draft_validate"}:
            raise Conflict("PhoneFleetExternalSubmissionDisabled")
        from phone_fleet.protocol import ExecutionRequest

        with self.store.connect(write=True) as db:
            task, item, policy = self.validate(db, task_id, item_id)
            binding = self.config.bindings[item["account_id"]]
            payload = json.loads(item["payload"])
            request = ExecutionRequest(
                business_task_id=task_id,
                task_item_id=item_id,
                manifest_sha256=task["manifest_hash"],
                account_id=item["account_id"],
                device_id=binding.device_id,
                operation=operation,
                execution_channel=policy.get("execution_channel", "inherit"),
                node_os=policy.get("execution_node_os", binding.node_os),
                not_before=max(epoch(policy["starts_at"]), self.engine.clock()),
                deadline=epoch(policy["ends_at"]),
                draft_sha256=digest({"text": payload.get("text", "")})
                if operation == "draft_validate"
                else None,
                expected_identity_sha256=binding.identity_sha256,
            )
            # One preparation per item/operation/manifest; clocks do not change identity.
            identity = digest([task_id, item_id, operation, task["manifest_hash"]])
            prior = db.execute(
                "SELECT job_id FROM phone_fleet_preparations WHERE digest=?", (identity,)
            ).fetchone()
            if prior:
                return prior[0]
            job_id = "phone_prepare:" + uuid4().hex
            route = {
                "platform": account_platform(db, item["account_id"]),
                "transport": "phone",
                "pool": "phone.prepare",
                "resource": "phone:" + digest(binding.device_id),
                "identity": item["account_id"],
                "execution_method": "phone",
                "node_os": request.node_os,
            }
            db.execute(
                "INSERT INTO execution_jobs(id,kind,reference_id,task_id,account_id,pool,route,version,available_at,expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    "phone_prepare",
                    item_id + ":" + operation,
                    task_id,
                    item["account_id"],
                    "phone.prepare",
                    canonical(route),
                    task["manifest_hash"],
                    self.engine.clock(),
                    request.deadline,
                    self.engine.clock(),
                    self.engine.clock(),
                ),
            )
            db.execute(
                "INSERT INTO phone_fleet_preparations(job_id,request,digest) VALUES(?,?,?)",
                (job_id, request.model_dump_json(), identity),
            )
            Store.audit(
                db,
                "phone.preparation_queued",
                {
                    "job_id": job_id,
                    "operation": operation,
                    "manifest_sha256": task["manifest_hash"],
                },
                task_id,
                self.engine.clock(),
            )
            return job_id

    def view(self, job_id):
        row = self.store.one(
            "SELECT p.*,j.task_id,j.state FROM phone_fleet_preparations p JOIN execution_jobs j ON j.id=p.job_id WHERE p.job_id=?",
            (job_id,),
        )
        if not row:
            raise Conflict("PhonePreparationNotFound")
        return {
            **row,
            "request": json.loads(row["request"]),
            "result": json.loads(row["result"]),
            "publication_verified": False,
            "external_submission_enabled": False,
        }

    async def tick(self, job_id):
        from phone_fleet.protocol import ExecutionRequest

        row = self.view(job_id)
        from .phone_protocol import is_legacy, legacy_result, require_capabilities

        if is_legacy(row["request"]):
            from urllib.parse import quote, urlencode

            path = (
                "/executions/" + quote(row["execution_id"], safe="")
                if row["execution_id"]
                else "/executions/lookup?"
                + urlencode(
                    {
                        k: row["request"][k]
                        for k in ("business_task_id", "task_item_id", "operation")
                    }
                )
            )
            result = legacy_result(
                await self.client.request("GET", path), row["request"], row["execution_id"]
            )
            return {**row, "result": result, "legacy_read_only": True}
        request = ExecutionRequest.model_validate(row["request"])
        if row["dispatch"] == "ready":
            require_capabilities(
                await self.client.request("GET", "/capabilities"), request.operation
            )
            with self.store.connect(write=True) as db:
                self.validate(db, request.business_task_id, request.task_item_id)
                changed = db.execute(
                    "UPDATE phone_fleet_preparations SET dispatch='sending' WHERE job_id=? AND dispatch='ready'",
                    (job_id,),
                ).rowcount
                if not changed:
                    return self.view(job_id)
                db.execute(
                    "UPDATE execution_jobs SET state='leased',owner='phone-preparation',lease_until=?,updated_at=? WHERE id=?",
                    (self.engine.clock() + 30, self.engine.clock(), job_id),
                )
            try:
                snapshot = await self.client.create(request, job_id)
            except Exception:
                with self.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE phone_fleet_preparations SET dispatch='uncertain' WHERE job_id=?",
                        (job_id,),
                    )
                    db.execute(
                        "UPDATE execution_jobs SET state='unknown',updated_at=? WHERE id=?",
                        (self.engine.clock(), job_id),
                    )
                return self.view(job_id)
        else:
            # Sending can survive process death. Lookup/query only, never POST again.
            try:
                snapshot = (
                    await self.client.get(row["execution_id"])
                    if row["execution_id"]
                    else await self.client.lookup(request)
                )
            except Exception:
                return row
        if snapshot.get("request") != request.model_dump(mode="json") or (
            row["execution_id"] and row["execution_id"] != snapshot.get("id")
        ):
            raise Conflict("PhoneFleetCorrelationMismatch")
        # A business pause/cancel/global stop must propagate to its mapped phone
        # execution. Query first, then fence the control with the current revision.
        task = self.store.one("SELECT state FROM tasks WHERE id=?", (request.business_task_id,))
        stop = self.store.one("SELECT value FROM meta WHERE key='global_stop'")
        desired = (
            "cancelled"
            if not task or task["state"] in {"cancelled", "completed"}
            else "paused"
            if task["state"] != "active" or not stop or stop["value"] != "0"
            else "run"
        )
        if (
            desired != "run"
            and snapshot["desired"] != desired
            and snapshot["status"] not in {"succeeded", "failed", "cancelled"}
        ):
            from urllib.parse import quote

            try:
                snapshot = await self.client.request(
                    "POST",
                    "/executions/" + quote(snapshot["id"], safe="") + "/control",
                    {
                        "action": "cancel" if desired == "cancelled" else "pause",
                        "expected_revision": snapshot["revision"],
                    },
                )
            except Exception:
                # A lost control acknowledgement does not authorize resume; next
                # tick queries authoritative desired state before another control.
                return row
        with self.store.connect(write=True) as db:
            current = db.execute(
                "SELECT * FROM phone_fleet_preparations WHERE job_id=?", (job_id,)
            ).fetchone()
            from phone_fleet.protocol import ExecutionSnapshot

            ExecutionSnapshot.model_validate(snapshot)
            if snapshot["request"] != request.model_dump(mode="json") or (
                current["execution_id"] and current["execution_id"] != snapshot["id"]
            ):
                raise Conflict("PhoneFleetCorrelationMismatch")
            if snapshot["revision"] < current["revision"]:
                return self.view(job_id)
            old = json.loads(current["result"])
            # A later pause/cancel/event cannot erase a latched unknown outcome.
            unknown = old.get("status") == "unknown" or snapshot["status"] == "unknown"
            state = (
                "unknown"
                if unknown
                else "completed"
                if snapshot["status"] in {"succeeded", "failed", "cancelled"}
                else "leased"
            )
            if unknown:
                snapshot = {**snapshot, "status": "unknown"}
            db.execute(
                "UPDATE phone_fleet_preparations SET dispatch='linked',execution_id=?,revision=?,result=? WHERE job_id=?",
                (snapshot["id"], snapshot["revision"], canonical(snapshot), job_id),
            )
            db.execute(
                "UPDATE execution_jobs SET state=?,result=?,updated_at=? WHERE id=?",
                (
                    state,
                    canonical(
                        {"preparation_status": snapshot["status"], "publication_verified": False}
                    ),
                    self.engine.clock(),
                    job_id,
                ),
            )
        return self.view(job_id)

    async def control(self, job_id, action):
        row = self.view(job_id)
        if not row["execution_id"] or action not in {"pause", "resume", "cancel"}:
            raise Conflict("PhoneFleetControlRequiresMapping")
        if action == "resume":
            request = row["request"]
            with self.store.connect() as db:
                self.validate(db, request["business_task_id"], request["task_item_id"])
        from urllib.parse import quote

        await self.client.request(
            "POST",
            "/executions/" + quote(row["execution_id"], safe="") + "/control",
            {"action": action, "expected_revision": row["revision"]},
        )
        return await self.tick(job_id)

    async def compensate_events(self, consumer_id="ap-phone"):
        row = self.store.one(
            "SELECT cursor FROM phone_fleet_event_consumers WHERE consumer_id=?", (consumer_id,)
        )
        cursor = row["cursor"] if row else 0
        page = await self.client.request("GET", "/events/poll?after=" + str(cursor))
        for event in page["events"]:
            for mapping in self.store.rows(
                "SELECT job_id FROM phone_fleet_preparations WHERE execution_id=?",
                (event["execution_id"],),
            ):
                await self.tick(mapping["job_id"])  # query authority, events are only hints
            cursor = max(cursor, event["event_id"])
            with self.store.connect(write=True) as db:
                db.execute(
                    "INSERT INTO phone_fleet_event_consumers VALUES(?,?) ON CONFLICT(consumer_id) DO UPDATE SET cursor=MAX(cursor,excluded.cursor)",
                    (consumer_id, cursor),
                )
        return cursor


def account_platform(db, account_id):
    return db.execute("SELECT platform FROM accounts WHERE id=?", (account_id,)).fetchone()[0]


def configured(engine, path):
    from phone_fleet.client import Client

    config = PhoneConfig.model_validate_json(Path(path).read_text(encoding="utf-8"))
    client = Client(config.fleet_url, config.service_token.get_secret_value(), development=True)
    return PhonePreparations(engine, config, client)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--database", required=True)
    args = parser.parse_args()
    import asyncio

    from .adapters import AdapterRegistry
    from .core import Engine

    engine = Engine(Store(args.database), AdapterRegistry(Path.cwd()))
    service = configured(engine, args.config)

    async def work():
        try:
            while True:
                for row in service.store.rows(
                    "SELECT job_id FROM phone_fleet_preparations p JOIN execution_jobs j ON j.id=p.job_id WHERE j.state IN ('pending','leased','unknown')"
                ):
                    try:
                        await service.tick(row["job_id"])
                    except Conflict:
                        # A stopped/deadline-blocked task does not stop other
                        # scoped preparations or reset its dispatch history.
                        continue
                await service.compensate_events()
                await asyncio.sleep(5)
        finally:
            await service.client.close()

    asyncio.run(work())


if __name__ == "__main__":
    main()
