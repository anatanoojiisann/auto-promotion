import pytest
import test_persistent_pools as fixtures

from auto_promotion.backends.common import BackendError
from auto_promotion.backends.phone import PhoneBackend
from auto_promotion.core import Conflict
from auto_promotion.models import Policy
from auto_promotion.phone_fleet_demo import demo, make_center
from auto_promotion.queue_store import JobQueue
from auto_promotion.routing import route_for


@pytest.mark.asyncio
async def test_offline_observe_draft_demo_never_attempts_or_confirms_delivery(tmp_path):
    summary = await demo(tmp_path)
    assert [r["status"] for r in summary["results"]] == ["succeeded", "succeeded"]
    assert summary["outbound_attempts"] == 0 and summary["manifest_unchanged"]
    assert summary["original_item_status"] == "not_attempted"
    assert all(not r["publication_verified"] for r in summary["results"])


@pytest.mark.asyncio
async def test_create_response_loss_queries_same_mapping_never_resends(tmp_path):
    fixture = fixtures.PersistentPoolTests()
    fixture.setUp()
    task, account = fixture.ready(channel="local")
    task = fixture.engine.task(task["id"])
    fleet, admin, service, agent, ap_client, node_client = await make_center(
        fixture.engine, tmp_path, task, account
    )
    try:
        job = service.enqueue(task["id"], task["items"][0]["id"])
        original = ap_client.create
        calls = []

        async def lost(request, key):
            calls.append(key)
            await original(request, key)
            raise TimeoutError("simulated response loss")

        ap_client.create = lost
        assert (await service.tick(job))["state"] == "unknown"
        recovered = await service.tick(job)
        assert recovered["execution_id"] and len(calls) == 1
        assert service.enqueue(task["id"], task["items"][0]["id"]) == job
        assert fixture.store.one("SELECT COUNT(*) n FROM attempts")["n"] == 0
        # The existing send/join worker cannot consume preparation jobs.
        assert JobQueue(fixture.engine).claim("phone.prepare", "old-worker") is None
        assert len(fleet.list(admin, "executions")) == 1
    finally:
        await ap_client.close()
        await node_client.close()
        fixture.doCleanups()


@pytest.mark.asyncio
async def test_paused_task_and_channel_mismatch_block_preparation(tmp_path):
    fixture = fixtures.PersistentPoolTests()
    fixture.setUp()
    task, account = fixture.ready(channel="local")
    task = fixture.engine.task(task["id"])
    fleet, admin, service, agent, ac, nc = await make_center(
        fixture.engine, tmp_path, task, account
    )
    try:
        item_id = task["items"][0]["id"]
        service.config = service.config.model_copy(update={"channel": "cloud"})
        with pytest.raises(Conflict, match="ChannelMismatch"):
            service.enqueue(task["id"], item_id)
        service.config = service.config.model_copy(update={"channel": "local"})
        with fixture.store.connect(write=True) as db:
            db.execute("UPDATE tasks SET state='paused' WHERE id=?", (task["id"],))
        with pytest.raises(Conflict, match="ExplicitlyStarted"):
            service.enqueue(task["id"], item_id)
    finally:
        await ac.close()
        await nc.close()
        fixture.doCleanups()


def test_phone_routing_separates_channel_method_os_and_disables_outbound():
    account = {"platform": "telegram", "mode": "live", "id": "account", "backend_label": "phone"}
    route = route_for(
        account,
        profile={
            "connection_type": "phone_fleet",
            "phone_fleet": {"device_id": "android-1", "node_os": "macos"},
        },
    )
    assert (
        route.pool == "telegram.phone"
        and route.execution_method == "phone"
        and route.node_os == "macos"
    )
    assert not PhoneBackend().call(account, "preflight", {})["allowed"]
    for operation in ["execute", "join", "discover"]:
        with pytest.raises(BackendError, match="ExternalSubmissionDisabled"):
            PhoneBackend().call(account, operation, {})
    assert set(Policy.model_fields["execution_channel"].annotation.__args__) == {
        "inherit",
        "cloud",
        "local",
    }


@pytest.mark.asyncio
async def test_business_pause_propagates_and_cannot_be_resumed_through_preparation(tmp_path):
    fixture = fixtures.PersistentPoolTests()
    fixture.setUp()
    task, account = fixture.ready(channel="local")
    task = fixture.engine.task(task["id"])
    fleet, admin, service, agent, ac, nc = await make_center(
        fixture.engine, tmp_path, task, account
    )
    try:
        job = service.enqueue(task["id"], task["items"][0]["id"])
        await service.tick(job)
        with fixture.store.connect(write=True) as db:
            db.execute("UPDATE tasks SET state='paused' WHERE id=?", (task["id"],))
        row = await service.tick(job)
        assert row["result"]["status"] == "paused"
        with pytest.raises(Conflict, match="ExplicitlyStarted"):
            await service.control(job, "resume")
        assert fixture.store.one("SELECT COUNT(*) n FROM attempts")["n"] == 0
    finally:
        await ac.close()
        await nc.close()
        fleet.store.engine.dispose()
        fixture.doCleanups()
