"""Explicit six-platform software check over the real Fleet API and a simulated node."""

import argparse
import asyncio
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from phone_fleet.protocol import VERSION, AccountBinding, Command

from .adapters import AdapterRegistry
from .core import Engine
from .models import AccountInput, ItemInput, TaskInput
from .phone_fleet_demo import make_center
from .phone_protocol import capability_status
from .runtime_identity import capture_runtime
from .store import Store

PLATFORMS = ("telegram", "reddit", "instagram", "facebook", "x", "tiktok")
TARGETS = {
    "telegram": "@synthetic_group",
    "reddit": "https://www.reddit.com/r/test/comments/abc123/fixture/",
    "instagram": "https://www.instagram.com/p/SYNTHETIC/",
    "facebook": "https://www.facebook.com/synthetic/posts/123",
    "x": "https://x.com/synthetic/status/123",
    "tiktok": "https://www.tiktok.com/@synthetic/video/123",
}


async def check(root, *, timeout=90):
    if not 0 < timeout <= 600:
        raise ValueError("SoftwareCheckTimeoutInvalid")
    async with asyncio.timeout(timeout):
        return await _check(root)


async def _check(root):
    started = time.monotonic()
    engine = Engine(
        Store(root / "ap.db"), AdapterRegistry(root), clock=time.time, live_enabled=False
    )
    tasks = []

    def iso(value):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()

    for platform in PLATFORMS:
        account = engine.add_account(
            AccountInput(
                platform=platform,
                label="SIMULATED " + platform,
                backend_label="software-check-" + platform,
                mode="simulation",
                min_interval=0,
            )
        )
        engine.check_account(account["id"])
        now = engine.clock()
        task = engine.create_task(
            TaskInput(
                name="SIMULATED software preparation " + platform,
                account_ids=[account["id"]],
                policy={
                    "execution_channel": "local",
                    "execution_method": "phone",
                    "starts_at": iso(now - 30),
                    "ends_at": iso(now + 600),
                    "min_interval": 0,
                    "max_joins": 0,
                },
            )
        )
        action = {"telegram": "send_message", "reddit": "reply", "x": "reply"}.get(
            platform, "comment"
        )
        engine.set_items(
            task["id"],
            [
                ItemInput(
                    account_id=account["id"],
                    action=action,
                    target=TARGETS[platform],
                    text="Synthetic draft for offline software validation",
                    scheduled_at=iso(now),
                    **(
                        {"target_owned": True, "tiktok_video_id": "123"}
                        if platform == "tiktok"
                        else {}
                    ),
                )
            ],
        )
        tasks.append((platform, account, task))
    # This stop belongs only to this newly created temporary fixture database.
    with engine.store.connect(write=True) as db:
        db.execute("UPDATE meta SET value='0' WHERE key='global_stop'")
    for _, _, task in tasks:
        engine.start_task(task["id"], source="explicit_simulated_software_check")
    tasks = [(p, a, engine.task(t["id"])) for p, a, t in tasks]
    fleet, admin, service, agent, business, node = await make_center(
        engine, root, tasks[0][2], tasks[0][1]
    )
    rows = []
    try:
        bindings = dict(service.config.bindings)
        for _, account, _ in tasks[1:]:
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
            bindings[account["id"]] = service.config.bindings[tasks[0][1]["id"]]
        service.config = service.config.model_copy(
            update={"bindings": bindings, "authorized_task_ids": [t["id"] for _, _, t in tasks]}
        )
        compatibility = capability_status(await business.request("GET", "/capabilities"))
        if not compatibility["software_ready"]:
            raise RuntimeError("SoftwareCheckProtocolMismatch")
        for platform, _, task in tasks:
            item = task["items"][0]
            operations = []
            before = time.monotonic()
            for operation in ("observe", "draft_validate"):
                job = service.enqueue(task["id"], item["id"], operation)
                await service.tick(job)
                claimed = await node.request("POST", "/node/claim", agent.ref())
                if not claimed.get("command"):
                    raise RuntimeError("SoftwareCheckCommandNotClaimed")
                await agent.execute(Command.model_validate(claimed["command"]))
                result = await service.tick(job)
                if not (
                    result["result"]["status"] == "succeeded"
                    and result["result"]["result"]["simulated"] is True
                ):
                    raise RuntimeError("SoftwareCheckSimulatedReadbackRequired")
                operations.append(
                    {
                        "operation": operation,
                        "job_id": job,
                        "execution_id": result["execution_id"],
                        "status": "succeeded",
                        "simulated": True,
                        "publication_verified": False,
                    }
                )
            rows.append(
                {
                    "platform": platform,
                    "task_id": task["id"],
                    "item_id": item["id"],
                    "operations": operations,
                    "elapsed_seconds": round(time.monotonic() - before, 3),
                    "manifest_unchanged": engine.task(task["id"])["manifest_hash"]
                    == task["manifest_hash"],
                    "item_status": engine.task(task["id"])["items"][0]["status"],
                }
            )
        await service.compensate_events()
        return {
            "schema": "phone-software-check/1",
            "mode": "simulation",
            "protocol": VERSION,
            "software_chain_verified": True,
            "hardware_validation": "not_started",
            "real_publications_confirmed": 0,
            "outbound_attempts": engine.store.one("SELECT COUNT(*) n FROM attempts")["n"],
            "external_submission_enabled": False,
            "runtime": capture_runtime("phone-software-check"),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "platforms": rows,
        }
    finally:
        await business.close()
        await node.close()
        fleet.store.engine.dispose()


def main():
    parser = argparse.ArgumentParser(
        description="Validate six-app software integration without a phone or platform calls"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="phone-software-check-") as directory:
        result = asyncio.run(check(Path(directory)))
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()
