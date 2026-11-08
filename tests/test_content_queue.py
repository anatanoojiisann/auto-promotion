"""Offline fixtures only: these tests never publish or claim hardware acceptance."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.api import create_app
from auto_promotion.content_queue import (
    ContentBatch,
    ContentDraft,
    ContentQueue,
    publication_status,
)
from auto_promotion.core import Conflict, Engine
from auto_promotion.delivery_policy import item_deadline, late
from auto_promotion.models import PLATFORMS, AccountInput, ItemInput, Policy
from auto_promotion.queue_store import JobQueue
from auto_promotion.store import Store

NOW = 1800000000


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "offline.db")
    engine = Engine(store, AdapterRegistry(tmp_path), clock=lambda: NOW)
    return engine, ContentQueue(engine)


def draft(platform="x", key="example"):
    return ContentDraft(
        platform=platform,
        idempotency_key=key,
        action="send_message"
        if platform in {"telegram", "discord"}
        else "reply"
        if platform in {"x", "reddit"}
        else "comment",
        target="https://x.com/source/status/12345" if platform == "x" else "test-target",
        text="Offline fixture, never publish.",
        context={"source": "synthetic"},
    )


def batch(engine, entry, key="batch"):
    account = engine.add_account(
        AccountInput(
            platform=entry["platform"],
            label="fixture",
            backend_label="fixture",
            mode="simulation",
            min_interval=0,
        )
    )
    return ContentBatch(
        idempotency_key=key,
        task={
            "name": "offline-content",
            "account_ids": [account["id"]],
            "policy": {
                "starts_at": iso(NOW - 10),
                "ends_at": iso(NOW + 10800),
                "execution_method": "phone",
                "execution_channel": "local",
                "dispatch_policy": "phone_queue_v1",
                "min_interval": 0,
            },
        },
        assignments=[
            {"content_id": entry["id"], "account_id": account["id"], "scheduled_at": iso(NOW)}
        ],
    )


@pytest.mark.parametrize("platform", PLATFORMS)
def test_all_platforms_can_produce_content_without_accounts_devices_or_models(setup, platform):
    engine, queue = setup
    entry = queue.put(draft(platform))
    assert entry["binding"] is None
    assert not engine.store.rows("SELECT * FROM tasks")
    assert not engine.store.rows("SELECT * FROM execution_jobs")
    reopened = ContentQueue(
        Engine(Store(engine.store.path), engine.adapters, clock=lambda: NOW + 7200)
    )
    assert reopened.get(entry["id"])["content_hash"] == entry["content_hash"]


def test_concurrent_content_writes_and_conflicting_idempotency(setup):
    engine, queue = setup
    with ThreadPoolExecutor(max_workers=4) as workers:
        entries = list(workers.map(lambda _: queue.put(draft()), range(8)))
    assert len({e["id"] for e in entries}) == 1
    with pytest.raises(Conflict):
        queue.put(draft().model_copy(update={"text": "changed"}))
    assert len(engine.store.rows("SELECT * FROM content_queue_entries")) == 1


def test_handoff_is_idempotent_and_never_starts_or_queues_sending(setup):
    engine, queue = setup
    value = batch(engine, queue.put(draft()))
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda _: queue.materialize(value), range(4)))
    assert len({r["task_id"] for r in results}) == 1
    assert all(r["state"] == "draft" for r in results)
    assert len(engine.store.rows("SELECT * FROM items")) == 1
    assert not engine.store.rows("SELECT * FROM attempts")
    assert not engine.store.rows("SELECT * FROM execution_jobs")


def test_lost_reply_after_task_creation_resumes_original_task(setup, monkeypatch):
    engine, queue = setup
    value = batch(engine, queue.put(draft()))
    original = engine.set_items
    monkeypatch.setattr(
        engine, "set_items", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("crash"))
    )
    with pytest.raises(RuntimeError):
        queue.materialize(value)
    task_id = engine.store.rows("SELECT * FROM tasks")[0]["id"]
    monkeypatch.setattr(engine, "set_items", original)
    assert queue.materialize(value)["task_id"] == task_id
    assert len(engine.store.rows("SELECT * FROM tasks")) == 1


def test_cannot_assign_same_content_twice_or_change_bound_batch(setup):
    engine, queue = setup
    value = batch(engine, queue.put(draft()))
    queue.materialize(value)
    with pytest.raises(Conflict):
        queue.materialize(value.model_copy(update={"idempotency_key": "other"}))
    altered = value.model_copy(deep=True)
    altered.task.name = "changed"
    with pytest.raises(Conflict):
        queue.materialize(altered)


def test_operator_changed_draft_is_not_silently_adopted(setup):
    engine, queue = setup
    value = batch(engine, queue.put(draft()))
    task_id = queue.materialize(value)["task_id"]
    with engine.store.connect(write=True) as db:
        db.execute(
            "UPDATE items SET payload=json_set(payload,'$.text','different') WHERE task_id=?",
            (task_id,),
        )
    with pytest.raises(Conflict):
        queue.materialize(value)


def test_new_delivery_policy_preserves_only_explicit_deadline(setup):
    engine, queue = setup
    value = batch(engine, queue.put(draft()))
    task_id = queue.materialize(value)["task_id"]
    with engine.store.connect(write=True) as db:
        # Fixture admission only; no actual platform or start authorization.
        db.execute("UPDATE tasks SET state='active',phase='ready' WHERE id=?", (task_id,))
    engine.clock = lambda: NOW + 7200
    JobQueue(engine).sync(task_id)
    job = engine.store.rows("SELECT * FROM execution_jobs WHERE kind='send'")[0]
    assert job["expires_at"] == NOW + 10800
    assert engine.task(task_id)["items"][0]["status"] == "not_attempted"
    engine.clock = lambda: NOW + 10801
    JobQueue(engine).sync(task_id)
    assert engine.task(task_id)["items"][0]["status"] == "missed"


def test_legacy_and_native_schedule_deadlines_are_unchanged():
    old = {"ends_at": iso(NOW + 10800)}
    new = {**old, "dispatch_policy": "phone_queue_v1"}
    assert item_deadline(old, NOW, "reply") == NOW + 900
    assert item_deadline(new, NOW, "reply") == NOW + 10800
    assert item_deadline(new, NOW, "schedule_message") == NOW
    assert late(old, NOW, NOW + 901) and not late(new, NOW, NOW + 901)
    with pytest.raises(ValueError):
        Policy(starts_at=iso(NOW), ends_at=iso(NOW + 100), dispatch_policy="phone_queue_v1")


@pytest.mark.parametrize(
    "status,result,reference,expected",
    [
        ("confirmed", {}, None, "pending_verification"),
        (
            "confirmed",
            {"verified": True, "native_id": "123"},
            "https://example.test/123",
            "published",
        ),
        ("unknown", {"submission_started": True}, None, "pending_verification"),
        ("cancelled", {"submission_started": True}, None, "pending_verification"),
        ("failed_known", {"confirmed_not_submitted": True}, None, "needs_attention"),
        ("not_attempted", {}, None, "queued"),
    ],
)
def test_six_status_projection_preserves_evidence(status, result, reference, expected):
    item = {"id": "fixture", "status": status, "result": result, "external_reference": reference}
    assert publication_status(item)["status"] == expected
    assert item["status"] == status


def test_content_api_requires_auth_and_works_without_sender(tmp_path):
    app = create_app(
        db_path=tmp_path / "api.db", registry=AdapterRegistry(tmp_path), token="fixture-key"
    )
    with TestClient(app) as client:
        payload = draft().model_dump()
        assert client.post("/api/content-queue", json=payload).status_code == 401
        headers = {"Authorization": "Bearer fixture-key"}
        saved = client.post("/api/content-queue", json=payload, headers=headers)
        assert saved.status_code == 201, saved.text
        assert (
            client.get("/api/content-queue", headers=headers).json()[0]["id"] == saved.json()["id"]
        )
        assert client.get("/api/content-queue?limit=0", headers=headers).status_code == 422
        assert client.get("/api/tasks", headers=headers).json() == []


@pytest.mark.parametrize("platform", PLATFORMS)
def test_platform_handoff_uses_existing_task_validation(setup, platform):
    engine, queue = setup
    urls = {
        "x": "https://x.com/source/status/12345",
        "telegram": "https://t.me/test_group",
        "instagram": "https://www.instagram.com/p/ABC123/",
        "facebook": "https://www.facebook.com/source/posts/23456",
        "tiktok": "https://www.tiktok.com/@source/video/34567",
        "reddit": "https://www.reddit.com/r/test/comments/abc123/example/",
        "discord": "123456789012345678",
    }
    entry = queue.put(draft(platform).model_copy(update={"target": urls[platform]}))
    value = batch(engine, entry)
    task_id = queue.materialize(value)["task_id"]
    task = engine.task(task_id)
    assert task["state"] == "draft"
    assert task["items"][0]["payload"]["target"] == urls[platform]
    assert not engine.store.rows("SELECT * FROM attempts")
    with pytest.raises(Conflict, match="ContentTaskImmutable"):
        engine.set_items(
            task_id,
            [
                ItemInput(
                    **entry["payload"],
                    account_id=value.assignments[0].account_id,
                    scheduled_at=value.assignments[0].scheduled_at,
                )
            ],
        )


def test_mcp_content_tools_are_local_and_persistent(setup):
    import asyncio

    from auto_promotion.mcp_server import create_server

    engine, queue = setup
    server = create_server(engine)

    async def exercise():
        tools = {t.name: t for t in await server.list_tools()}
        assert tools["list_interaction_content"].annotations.readOnlyHint
        for name in ("save_interaction_content", "materialize_content_batch"):
            assert not tools[name].annotations.openWorldHint
            assert not tools[name].annotations.destructiveHint
        result = await server.call_tool(
            "save_interaction_content", {"content": draft().model_dump()}
        )
        assert result
        assert len(queue.list()) == 1
        assert not engine.store.rows("SELECT * FROM tasks")

    asyncio.run(exercise())


def test_invalid_late_batch_does_not_replace_bound_items(setup):
    engine, queue = setup
    entry = queue.put(draft())
    value = batch(engine, entry)
    first = queue.materialize(value)
    engine.clock = lambda: NOW + 900
    assert queue.materialize(value)["items"] == first["items"]
    invalid = value.model_copy(deep=True)
    invalid.assignments[0].account_id = "untrusted-account"
    with pytest.raises(Exception):
        queue.materialize(invalid)
    assert len(engine.store.rows("SELECT * FROM tasks")) == 1


def test_phone_tiktok_does_not_apply_browser_or_api_owned_target_rules(setup):
    engine, queue = setup
    entry = queue.put(
        draft("tiktok").model_copy(
            update={"target": "https://www.tiktok.com/@source/video/34567", "text": "A" * 180}
        )
    )
    assert queue.materialize(batch(engine, entry))["state"] == "draft"


def test_explicit_start_defers_health_but_does_not_bypass_global_stop(setup, monkeypatch):
    engine, queue = setup
    value = batch(engine, queue.put(draft()))
    task_id = queue.materialize(value)["task_id"]

    def forbidden_probe(*args, **kwargs):
        raise AssertionError("Start must not use the phone or probe desktop health")

    monkeypatch.setattr(engine, "check_account", forbidden_probe)
    started = engine.start_task(task_id, source="synthetic_explicit_start")
    assert started["state"] == "active"
    assert started["publication_items"][0]["status"] == "queued"
    assert engine.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"] == "1"
    JobQueue(engine).sync(task_id)
    assert JobQueue(engine).claim("x.phone", "fixture-worker") is None
    assert not engine.store.rows("SELECT * FROM attempts")


def test_durable_start_still_rejects_disabled_account(setup):
    engine, queue = setup
    value = batch(engine, queue.put(draft()))
    task_id = queue.materialize(value)["task_id"]
    with engine.store.connect(write=True) as db:
        db.execute("UPDATE accounts SET disabled=1 WHERE id=?", (value.assignments[0].account_id,))
    with pytest.raises(Conflict, match="AccountDisabled"):
        engine.start_task(task_id)
    assert engine.task(task_id)["state"] == "draft"
