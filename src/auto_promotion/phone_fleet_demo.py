"""Offline AP -> Fleet API -> simulated Node -> draft/readback demo. No devices."""

import asyncio
import base64
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from phone_fleet.agent import Agent
from phone_fleet.api import create_app
from phone_fleet.auth import DevelopmentAuth, Principal
from phone_fleet.client import Client
from phone_fleet.protocol import (
    AccountBinding,
    Command,
    DeviceRegistration,
    Group,
    NodeRegistration,
    PairRedeem,
    PairRequest,
)
from phone_fleet.runtime import Journal, SimulatedRuntime
from phone_fleet.service import Fleet
from phone_fleet.storage import Store as FleetStore

from .adapters import AdapterRegistry
from .core import Engine
from .models import AccountInput, ItemInput, TaskInput
from .phone_fleet import PhoneConfig, PhonePreparations
from .store import Store


async def make_center(engine, root, task, account):
    """Only synthetic fixtures, no configured DB/credential/environment lookup."""
    store = FleetStore("sqlite:///" + (root / "fleet.db").as_posix(), development=True)
    store.migrate()
    fleet = Fleet(store, development=True, internal_enabled=True, clock=engine.clock)
    admin, business = Principal("demo", "operator", "admin"), Principal("demo", "ap", "business")
    auth = DevelopmentAuth({"a" * 32: admin, "b" * 32: business}, development=True)
    fleet.register(admin, "groups", Group(id="demo-group", label="Simulated laboratory"))
    private = Ed25519PrivateKey.generate()
    fleet.register(
        admin,
        "nodes",
        NodeRegistration(
            id="demo-windows",
            label="simulated Windows node",
            os="windows",
            arch="x86_64",
            public_key=base64.b64encode(private.public_key().public_bytes_raw()).decode(),
            simulated=True,
        ),
    )
    fleet.register(
        admin,
        "devices",
        DeviceRegistration(
            id="demo-android",
            label="simulated Android",
            node_id="demo-windows",
            group_id="demo-group",
            serial_sha256="a" * 64,
        ),
    )
    fleet.register(
        admin,
        "accounts",
        AccountBinding(
            id=account["id"],
            device_id="demo-android",
            platform=account["platform"],
            identity_sha256="d" * 64,
        ),
    )
    pair = fleet.pair(admin, PairRequest(kind="node", target_id="demo-windows"))
    issued = fleet.redeem(
        PairRedeem(
            pair_id=pair["pair_id"],
            code=pair["code"],
            signature=base64.b64encode(private.sign(pair["signing_message"].encode())).decode(),
        )
    )
    app = create_app(fleet, auth)
    transport = httpx.ASGITransport(app)
    ap_client = Client("http://127.0.0.1:18340", "b" * 32, development=True, transport=transport)
    node_client = Client(
        "http://127.0.0.1:18340", issued["token"], development=True, transport=transport
    )
    config = PhoneConfig(
        mode="development",
        enabled=True,
        fleet_url="http://127.0.0.1:18340",
        service_token="b" * 32,
        authorized_task_ids=[task["id"]],
        channel="local",
        bindings={account["id"]: {"device_id": "demo-android", "identity_sha256": "d" * 64}},
    )
    preparations = PhonePreparations(engine, config, ap_client)
    runtime = SimulatedRuntime(
        [
            {
                "device_id": "demo-android",
                "serial_sha256": "a" * 64,
                "initialization": "ready",
                "capabilities": ["observe", "draft_validate"],
                "runtime_version": "android-runtime/0.1",
                "simulated": True,
            }
        ],
        clock=engine.clock,
    )
    agent = Agent(
        node_client,
        runtime,
        Journal(root / "agent.db"),
        os_name="windows",
        arch="x86_64",
        clock=engine.clock,
    )
    await agent.connect()
    await agent.heartbeat()
    return fleet, admin, preparations, agent, ap_client, node_client


async def demo(root):
    engine = Engine(Store(root / "ap.db"), AdapterRegistry(root), clock=time.time)
    now = engine.clock()

    def iso(value):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()

    account = engine.add_account(
        AccountInput(
            platform="x",
            label="simulated",
            backend_label="simulated",
            mode="simulation",
            min_interval=0,
        )
    )
    engine.check_account(account["id"])
    task = engine.create_task(
        TaskInput(
            name="offline phone observation",
            account_ids=[account["id"]],
            policy={
                "execution_channel": "local",
                "execution_method": "phone",
                "starts_at": iso(now - 30),
                "ends_at": iso(now + 3600),
                "min_interval": 0,
                "max_joins": 0,
            },
        )
    )
    engine.set_items(
        task["id"],
        [
            ItemInput(
                account_id=account["id"],
                action="reply",
                target="https://x.com/simulated/status/123",
                text="Synthetic draft only",
                scheduled_at=iso(now),
            )
        ],
    )
    # Explicit fixture start only. The isolated database's stop is changed solely
    # to exercise the existing Engine start; no real global setting is accessed.
    with engine.store.connect(write=True) as db:
        db.execute("UPDATE meta SET value='0' WHERE key='global_stop'")
    engine.start_task(task["id"], source="offline_simulated_demo")
    task = engine.task(task["id"])
    fleet, admin, service, agent, ap_client, node_client = await make_center(
        engine, root, task, account
    )
    summary = []
    try:
        for operation in ["observe", "draft_validate"]:
            job = service.enqueue(task["id"], task["items"][0]["id"], operation)
            await service.tick(job)
            claimed = await node_client.request("POST", "/node/claim", agent.ref())
            await agent.execute(Command.model_validate(claimed["command"]))
            row = await service.tick(job)
            summary.append(
                {
                    "operation": operation,
                    "job_id": job,
                    "execution_id": row["execution_id"],
                    "status": row["result"]["status"],
                    "publication_verified": row["publication_verified"],
                }
            )
        await service.compensate_events()
        return {
            "simulated": True,
            "results": summary,
            "native_actions": len(agent.runtime.calls),
            "outbound_attempts": engine.store.one("SELECT COUNT(*) n FROM attempts")["n"],
            "manifest_unchanged": engine.task(task["id"])["manifest_hash"] == task["manifest_hash"],
            "original_item_status": engine.task(task["id"])["items"][0]["status"],
            "external_submission_enabled": False,
        }
    finally:
        await ap_client.close()
        await node_client.close()
        fleet.store.engine.dispose()


def main():
    root = Path("var/phone-fleet-demo").resolve()
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as directory:
        print(json.dumps(asyncio.run(demo(Path(directory))), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
