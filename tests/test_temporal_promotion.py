"""Official Temporal server and SDK; all platform responses are synthetic."""

import asyncio
import os
import time

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from auto_promotion.temporal_task_workflow import PromotionTask

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def until(handle, stage):
    async with asyncio.timeout(20):
        while True:
            try:
                value = await handle.query(PromotionTask.progress)
                if value["stage"] == stage:
                    return value
            except Exception:
                pass
            await asyncio.sleep(0.05)


async def test_original_unknown_yields_and_recovers_without_another_admission(tmp_path):
    executable = os.environ.get("FLEET_TEST_TEMPORAL_EXE")
    if not executable:
        pytest.skip("Explicit local Temporal executable required")
    admissions = []
    receipt_ready = False

    @activity.defn(name="promotion_task_admit")
    async def admit(value: dict) -> dict:
        admissions.append(value["task_id"])
        return {"admitted": True}

    @activity.defn(name="promotion_task_status")
    async def status(value: dict) -> dict:
        confirmed = value["task_id"] == "instagram" or receipt_ready
        return {"manifest_hash": value["manifest_hash"], "state": "completed" if confirmed else "active", "items": [{
            "status": "confirmed" if confirmed else "unknown", "submission_started": True,
            "external_reference": "synthetic://native/11" if confirmed else None,
            "native_id": "11" if confirmed else None, "verified": confirmed, "resource_state": "released"}]}

    options = {"task_queue": "new-task-regression", "workflows": [PromotionTask], "activities": [admit, status]}
    async with await WorkflowEnvironment.start_local(dev_server_existing_path=executable,
            dev_server_database_filename=str(tmp_path / "temporal.sqlite"), dev_server_log_level="error") as temporal:
        tg = {"task_id": "telegram", "manifest_hash": "a" * 64, "workflow_id": "original-telegram", "deadline": time.time() + 90}
        handle = await temporal.client.start_workflow(PromotionTask.run, tg, id=tg["workflow_id"], task_queue=options["task_queue"])
        async with Worker(temporal.client, **options):
            current = await until(handle, "waiting_receipt")
            assert current["device_held"] is False
        # The replacement worker replays the same workflow; original admission
        # and unknown business item are not recreated on reconnect.
        async with Worker(temporal.client, **options):
            ig = {**tg, "task_id": "instagram", "workflow_id": "independent-instagram"}
            other = await temporal.client.start_workflow(PromotionTask.run, ig, id=ig["workflow_id"], task_queue=options["task_queue"])
            assert (await other.result())["stage"] == "confirmed"
            assert (await handle.query(PromotionTask.progress))["stage"] == "waiting_receipt"
            receipt_ready = True
            assert (await handle.result())["stage"] == "confirmed"
        assert admissions == ["telegram", "instagram"]
