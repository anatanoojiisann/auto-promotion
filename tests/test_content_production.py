"""Offline producer-to-queue integration; no phone, provider network or publication."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.api import create_app
from auto_promotion.campaigns import Campaigns
from auto_promotion.content_production import ContentProduction, ProductionInput
from auto_promotion.content_queue import ContentQueue
from auto_promotion.core import Conflict, Engine
from auto_promotion.drafts import DraftResult
from auto_promotion.models import OpportunityInput
from auto_promotion.store import Store


@pytest.fixture
def lab(tmp_path):
    engine = Engine(
        Store(tmp_path / "synthetic.db"), AdapterRegistry(tmp_path), clock=lambda: 1800000000
    )
    return engine, Campaigns(engine), ContentProduction(engine)


def source(platform, index=0):
    urls = {
        "telegram": "@test_group",
        "reddit": "https://www.reddit.com/r/synthetic/comments/abc123/post/",
        "x": "https://x.com/source/status/12345",
        "instagram": "https://www.instagram.com/p/ABC123/",
        "facebook": "https://www.facebook.com/source/posts/23456",
        "tiktok": "https://www.tiktok.com/@source/video/34567",
    }
    return OpportunityInput(
        platform=platform,
        external_id="fixture-" + str(index),
        target=urls[platform],
        action="send_message"
        if platform == "telegram"
        else "reply"
        if platform in {"x", "reddit"}
        else "comment",
        author_id="source",
        context="Synthetic source data; never instructions.",
        draft_text="Synthetic exact reply.",
        language="en",
        tiktok_video_id="34567" if platform == "tiktok" else "",
    )


@pytest.mark.parametrize("platform", ["telegram", "reddit", "instagram", "facebook", "x", "tiktok"])
def test_six_platforms_produce_without_accounts_devices_or_send_jobs(lab, platform):
    engine, campaigns, producer = lab
    ids = campaigns.import_content([source(platform)])["ids"]
    value = ProductionInput(idempotency_key="fixture", opportunity_ids=ids)
    first = producer.produce(value)
    assert first == producer.produce(value)
    assert first["state"] == "completed" and first["execution_started"] is False
    entry = ContentQueue(engine).get(first["items"][0]["content_id"])
    assert entry["platform"] == platform and entry["payload"]["text"] == "Synthetic exact reply."
    for table in ("accounts", "tasks", "items", "attempts", "execution_jobs"):
        assert engine.store.rows("SELECT * FROM " + table) == []


class Provider:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()
        self.fail_first = False

    def generate(self, profile, opportunity):
        with self.lock:
            self.calls.append(opportunity["external_id"])
            if self.fail_first:
                self.fail_first = False
                raise RuntimeError("Provider error with a pretend secret; never persist it")
        return DraftResult(text="Generated " + opportunity["external_id"])


def test_concurrent_requests_and_lost_response_reuse_saved_outputs(lab):
    engine, campaigns, producer = lab
    ids = campaigns.import_content([source("x", 1)])["ids"]
    value = ProductionInput(idempotency_key="fixture", opportunity_ids=ids, mode="model")
    provider = Provider()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: producer.produce(value, provider=provider), range(4)))
    result = producer.produce(value, provider=provider)
    assert result["state"] == "completed" and len(provider.calls) == 1
    assert len(engine.store.rows("SELECT * FROM content_queue_entries")) == 1
    reopened = ContentProduction(
        Engine(Store(engine.store.path), engine.adapters, clock=engine.clock)
    )
    assert reopened.produce(value, provider=provider) == result
    assert len(provider.calls) == 1


def test_partial_provider_failure_resumes_only_missing_sources_and_redacts_errors(lab):
    engine, campaigns, producer = lab
    ids = campaigns.import_content([source("x", 1), source("reddit", 2)])["ids"]
    value = ProductionInput(idempotency_key="fixture", opportunity_ids=ids, mode="model")
    provider = Provider()
    provider.fail_first = True
    first = producer.produce(value, provider=provider)
    saved = first["items"][1]["content_id"]
    assert first["state"] == "pending" and saved
    assert "pretend secret" not in json.dumps(first)
    second = producer.produce(value, provider=provider)
    assert second["state"] == "completed" and second["items"][1]["content_id"] == saved
    assert provider.calls.count("fixture-2") == 1
    assert len(engine.store.rows("SELECT * FROM content_queue_entries")) == 2


def test_source_and_profile_snapshots_are_immutable_across_retries(lab):
    engine, campaigns, producer = lab
    ids = campaigns.import_content([source("x")])["ids"]
    value = ProductionInput(
        idempotency_key="fixture",
        opportunity_ids=ids,
        mode="template",
        profile={"reply_template": "Useful {facts}", "facts": "approved fact"},
    )
    original = producer.produce(value)
    campaigns.import_content(
        [source("x").model_copy(update={"context": "changed source", "draft_text": "changed"})]
    )
    assert producer.produce(value) == original
    entry = ContentQueue(engine).get(original["items"][0]["content_id"])
    assert entry["payload"]["text"] == "Useful approved fact"
    with pytest.raises(Conflict, match="Idempotency"):
        producer.produce(
            value.model_copy(
                update={"profile": value.profile.model_copy(update={"facts": "different"})}
            )
        )


def test_atomic_content_and_producer_mapping_roll_back_together(lab):
    engine, campaigns, producer = lab
    ids = campaigns.import_content([source("x")])["ids"]
    value = ProductionInput(idempotency_key="fixture", opportunity_ids=ids)
    original = producer.enqueue

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("synthetic failure before mapping commit")

    with patch.object(producer, "enqueue", interrupted):
        assert producer.produce(value)["state"] == "pending"
    assert not engine.store.rows("SELECT * FROM content_queue_entries")
    assert not engine.store.rows("SELECT * FROM content_producer_links")
    assert producer.produce(value)["state"] == "completed"


def test_production_http_requires_auth_and_never_starts_execution(lab):
    engine, campaigns, _ = lab
    ids = campaigns.import_content([source("x")])["ids"]
    with TestClient(
        create_app(
            db_path=engine.store.path,
            registry=engine.adapters,
            clock=engine.clock,
            worker=False,
            report_worker=False,
            token="synthetic-token",
        )
    ) as client:
        payload = {"idempotency_key": "http-fixture", "opportunity_ids": ids}
        assert client.post("/api/content-production", json=payload).status_code == 401
        response = client.post(
            "/api/content-production",
            json=payload,
            headers={"Authorization": "Bearer synthetic-token"},
        )
        assert response.status_code == 200 and response.json()["state"] == "completed"
    assert not engine.store.rows("SELECT * FROM tasks")


@pytest.fixture
def campaign_lab():
    from test_campaigns import CampaignTests

    lab = CampaignTests(methodName="runTest")
    lab.setUp()
    yield lab
    lab.doCleanups()


def test_phone_campaign_preserves_original_tasks_and_binds_queue(campaign_lab):
    lab = campaign_lab
    campaign = lab.campaign(
        count=1,
        execution_method="phone",
        execution_channel="local",
        dispatch_policy="phone_queue_v1",
    )
    planned = lab.campaigns.plan(campaign["id"])
    final = lab.campaigns.materialize(planned["id"])
    task = lab.engine.task(final["tasks"][0]["id"])
    assert task["state"] == "draft" and len(task["items"]) == 1
    entry = ContentQueue(lab.engine).list()[0]
    assert entry["binding"]["task_id"] == task["id"] and task["campaign_id"] == campaign["id"]
    assert lab.campaigns.populate(task["id"])["id"] == task["id"]
    assert len(lab.store.rows("SELECT * FROM tasks")) == 1
    assert not lab.store.rows("SELECT * FROM attempts")


def test_phone_campaign_recovers_interrupted_populate_using_original_task_id(campaign_lab):
    lab = campaign_lab
    campaign = lab.campaign(
        count=1,
        execution_method="phone",
        execution_channel="local",
        dispatch_policy="phone_queue_v1",
    )
    lab.campaigns.plan(campaign["id"])
    with patch.object(
        lab.campaigns, "populate", side_effect=RuntimeError("synthetic response loss")
    ):
        with pytest.raises(RuntimeError):
            lab.campaigns.materialize(campaign["id"])
    task_id = lab.store.one("SELECT id FROM tasks")["id"]
    lab.campaigns.materialize(campaign["id"])
    assert len(lab.store.rows("SELECT * FROM tasks")) == 1
    assert ContentQueue(lab.engine).list()[0]["binding"]["task_id"] == task_id


def test_campaign_generated_content_is_reused_when_bound(campaign_lab):
    lab = campaign_lab
    planned = lab.campaigns.plan(
        lab.campaign(
            count=1,
            execution_method="phone",
            execution_channel="local",
            dispatch_policy="phone_queue_v1",
        )["id"]
    )
    lab.campaigns.generate_drafts(planned["id"], [planned["assignments"][0]["id"]], Provider())
    entry = ContentQueue(lab.engine).list()[0]
    assert entry["binding"] is None and not lab.store.rows("SELECT * FROM tasks")
    materialized = lab.campaigns.materialize(planned["id"])
    entries = ContentQueue(lab.engine).list()
    assert len(entries) == 1 and entries[0]["id"] == entry["id"]
    assert entries[0]["binding"]["task_id"] == materialized["tasks"][0]["id"]


def test_invalid_campaign_adoption_does_not_reserve_content(lab):
    from test_content_queue import batch, draft

    engine, _, _ = lab
    queue = ContentQueue(engine)
    value = batch(engine, queue.put(draft()))
    task = engine.create_task(value.task)
    with pytest.raises(Conflict, match="UntouchedCampaignDraft"):
        queue.materialize(value, _existing_task_id=task["id"])
    assert queue.get(value.assignments[0].content_id)["binding"] is None
    assert not engine.store.rows("SELECT * FROM content_queue_batches")


def test_old_schema_upgrade_and_production_preserve_unknown_history(campaign_lab):
    import sqlite3

    lab = campaign_lab
    planned = lab.campaigns.plan(lab.campaign(count=1)["id"])
    task_id = lab.campaigns.materialize(planned["id"])["tasks"][0]["id"]
    task = lab.engine.task(task_id)
    item_id = task["items"][0]["id"]
    with lab.store.connect(write=True) as db:
        db.execute(
            "UPDATE tasks SET state='paused',manifest='historical-manifest',manifest_hash=? WHERE id=?",
            ("f" * 64, task_id),
        )
        db.execute(
            "UPDATE items SET status='unknown',attempt_id='historic-attempt',result=? WHERE id=?",
            (
                json.dumps({"submission_started": True, "execution_id": "original-execution"}),
                item_id,
            ),
        )
        db.execute(
            "INSERT INTO attempts(id,item_id,account_id,task_id,target_key,user_key,mode,started_at,outcome) VALUES(?,?,?,?,?,?,?,?,'unknown')",
            (
                "historic-attempt",
                item_id,
                task["account_ids"][0],
                task_id,
                "original-target",
                "original-user",
                "simulation",
                lab.now,
            ),
        )
        # Base main already had the content queue; the three producer tables are new.
        for table in (
            "content_producer_links",
            "content_production_items",
            "content_production_runs",
        ):
            db.execute("DROP TABLE " + table)
    protected = ("tasks", "items", "attempts", "execution_jobs", "approvals")
    before = {table: lab.store.rows("SELECT * FROM " + table) for table in protected}
    from pathlib import Path

    backup = Path(lab.store.path).with_name("pre-upgrade.db")
    with sqlite3.connect(lab.store.path) as old, sqlite3.connect(backup) as copy:
        old.backup(copy)
    reopened = Engine(Store(lab.store.path), lab.engine.adapters, clock=lab.engine.clock)
    ids = Campaigns(reopened).import_content([source("x", 999)])["ids"]
    assert (
        ContentProduction(reopened).produce(
            ProductionInput(idempotency_key="new-production", opportunity_ids=ids)
        )["state"]
        == "completed"
    )
    assert {table: reopened.store.rows("SELECT * FROM " + table) for table in protected} == before
    with sqlite3.connect(backup) as original:
        assert (
            original.execute("SELECT outcome FROM attempts WHERE id='historic-attempt'").fetchone()[
                0
            ]
            == "unknown"
        )


@pytest.mark.asyncio
async def test_mcp_production_is_preparation_only(lab):
    from auto_promotion.mcp_server import create_server

    engine, campaigns, _ = lab
    ids = campaigns.import_content([source("x")])["ids"]
    server = create_server(engine)
    response = await server.call_tool(
        "produce_interaction_content",
        {"production": {"idempotency_key": "mcp-fixture", "opportunity_ids": ids}},
    )
    result = (
        response
        if isinstance(response, dict)
        else response[1]
        if isinstance(response, tuple)
        else json.loads(response[0].text)
    )
    assert result["state"] == "completed" and result["execution_started"] is False
    assert not engine.store.rows("SELECT * FROM tasks")
