"""Automatic producers/campaigns use real AP code and synthetic native transports only."""

import json
import time
from pathlib import Path

import httpx
import pytest
from test_content_phone_pipeline import SCENARIOS, NativeReceiptTransport, iso
from test_phone_reddit import BODY, META, URL, claim_from_original_queue
from test_phone_reddit import pilot as reddit_fixture
from test_phone_social import TEXT, URLS, claim_original
from test_phone_social import pilot as social_fixture

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.campaigns import Campaigns
from auto_promotion.content_queue import ContentBatch, ContentQueue
from auto_promotion.core import Engine
from auto_promotion.interaction_models import ConfigSave, InteractionConfig
from auto_promotion.interaction_simulation import SimulationConnectors, source
from auto_promotion.interactions import Interactions
from auto_promotion.models import (
    AccountInput,
    AccountProfile,
    CampaignInput,
    OpportunityInput,
    PoolInput,
)
from auto_promotion.phone_reddit import PhoneReddit
from auto_promotion.phone_social import PhoneSocial
from auto_promotion.pools import Pools
from auto_promotion.quotas import user_identifier
from auto_promotion.store import Store

social_pilot = social_fixture
reddit_pilot = reddit_fixture


def campaign_task(s, platform):
    pools = Pools(s.engine)
    pools.set_profile(s.account["id"], AccountProfile(languages=["en"]))
    pool = pools.save(PoolInput(name="Synthetic phone producer", account_ids=[s.account["id"]]))
    campaigns = Campaigns(s.engine)
    target, text = (URL, BODY) if platform == "reddit" else (URLS[platform], TEXT)
    opportunity = OpportunityInput(
        platform=platform,
        external_id="synthetic-campaign-target",
        target=target,
        action="send_message"
        if platform == "telegram"
        else "reply"
        if platform in {"x", "reddit"}
        else "comment",
        title=META["title"] if platform == "reddit" else "Synthetic target",
        context="Frozen AI video target",
        author_id="synthetic-target-author",
        draft_text=text,
        permission="allowed",
        permission_evidence="Synthetic explicit target permission",
        is_public=True,
        language="en",
        tiktok_video_id="34567" if platform == "tiktok" else "",
    )
    ids = campaigns.import_content([opportunity])["ids"]
    now = time.time()
    campaign = campaigns.create(
        CampaignInput(
            name="Synthetic campaign-to-phone test",
            pool_id=pool["id"],
            opportunity_ids=ids,
            policy={
                "starts_at": iso(now - 5),
                "ends_at": iso(now + 1800),
                "execution_method": "phone",
                "execution_channel": "local",
                "dispatch_policy": "phone_queue_v1",
                "min_interval": 120,
                "max_joins": 0,
                "languages": ["en"],
            },
        )
    )
    planned = campaigns.plan(campaign["id"])
    assert planned["summary"] == {"planned": 1}, [a["reasons"] for a in planned["assignments"]]
    assert planned["assignments"][0]["payload"]["recipient_id"] == ""
    assert planned["assignments"][0]["payload"]["source_author_id"] == opportunity.author_id
    materialized = campaigns.materialize(campaign["id"])
    s.task = s.engine.task(materialized["tasks"][0]["id"])
    s.item = s.task["items"][0]
    # Advance only the synthetic clock to the planner's real due time.
    clock = time.time
    offset = max(0, s.item["due_at"] - clock()) + 0.01
    s.monkeypatch.setattr(time, "time", lambda: clock() + offset)
    s.engine.clock = time.time
    return campaign["id"]


def hf_task(s):
    owner = s.engine.add_account(
        AccountInput(
            platform="x",
            label="Synthetic search owner",
            backend_label="synthetic-search-owner",
            mode="simulation",
            min_interval=0,
        )
    )
    s.engine.check_account(owner["id"])
    io = SimulationConnectors(s.engine)
    service = Interactions(s.engine, connectors=io)
    now = time.time()
    config = InteractionConfig(
        policy_key="synthetic-content-only",
        content_queue_only=True,
        enabled=True,
        run_mode="live",
        account_ids=[owner["id"]],
        queries=["synthetic query"],
        facts=[
            {
                "id": "demo-fact",
                "text": "Synthetic verified capability",
                "source": "simulation://fact",
                "verified_at": now - 100,
                "valid_until": now + 3600,
            }
        ],
    )
    policy = service.save(ConfigSave(config=config))
    value = source(12345, "P1", now)
    value["url"] = URLS["x"]
    io.sources = [value]
    service.trigger(policy["id"], manual=True)
    service.run_stage("scan", max_jobs=1)
    service.run_stage("classify", max_jobs=10)
    service.run_stage("generate", max_jobs=10)
    candidate = service.candidates(policy["id"])[0]
    entry = ContentQueue(s.engine).list()[0]
    assert (
        entry["payload"]["recipient_id"] == ""
        and entry["payload"]["source_author_id"] == value["author_id"]
    )
    batch = ContentBatch(
        idempotency_key="synthetic-hf-phone",
        task={
            "name": "Synthetic HF-to-phone test",
            "account_ids": [s.account["id"]],
            "policy": {
                "starts_at": iso(now - 5),
                "ends_at": iso(now + 1800),
                "execution_method": "phone",
                "execution_channel": "local",
                "dispatch_policy": "phone_queue_v1",
                "min_interval": 120,
            },
        },
        assignments=[
            {"content_id": entry["id"], "account_id": s.account["id"], "scheduled_at": iso(now)}
        ],
    )
    task = ContentQueue(s.engine).materialize(batch)
    s.task = s.engine.task(task["task_id"])
    s.item = s.task["items"][0]
    bound = service.candidate(candidate["id"])
    assert bound["item_id"] == s.item["id"] and bound["owner_account_id"] == owner["id"]
    assert bound["execution_account_id"] == s.account["id"]
    return candidate["id"], owner["id"]


def execute(s, platform, scenario, candidate_id=None, owner_id=None):
    transport = NativeReceiptTransport(s.fake, scenario)
    s.service.transport = httpx.MockTransport(transport)
    token = (
        "synthetic-runtime-token"
        if platform == "reddit"
        else "synthetic-runtime-token-of-sufficient-length"
    )

    def grant(body):
        return s.service.authorize(body, token)

    if platform == "reddit":
        s.fake.submit_authorize = grant
        claim, context = claim_from_original_queue(s)
    else:
        s.fake.authorize = grant
        claim, context = claim_original(s)
    attempt = s.store.one("SELECT * FROM attempts WHERE id=?", (claim["attempt_id"],))
    from auto_promotion.models import digest

    assert attempt["user_key"] == digest([platform, user_identifier(s.item["payload"])])
    native = s.service.execute(s.engine.account(s.account["id"]), claim, context)
    s.engine.finish(claim, native)
    if scenario != "published":
        assert (
            s.engine.task(s.task["id"])["publication_items"][0]["status"] == "pending_verification"
        )
        if candidate_id:
            assert Interactions(s.engine).candidate(candidate_id)["state"] == "VERIFYING"
        if scenario == "receipt_after_stop_restart":
            s.engine.set_stop(True)
            transport.receipt_state = "published"
            s.engine = Engine(
                Store(s.store.path),
                AdapterRegistry(Path(s.store.path).parent),
                live_enabled=True,
                clock=s.engine.clock,
            )
            s.service = (PhoneReddit if platform == "reddit" else PhoneSocial)(
                s.engine,
                s.service.config.model_copy(update={"submit_enabled": False}),
                transport=httpx.MockTransport(transport),
            )
        for _ in range(3):
            s.engine.reconcile(s.item["id"])
            if s.engine.task(s.task["id"])["items"][0]["status"] == "confirmed":
                break
    final = s.engine.task(s.task["id"])
    expected = "pending_verification" if scenario == "wrong_item" else "published"
    assert final["publication_items"][0]["status"] == expected
    if candidate_id:
        candidate = Interactions(s.engine).candidate(candidate_id)
        assert candidate["state"] == ("VERIFYING" if scenario == "wrong_item" else "PUBLISHED")
        assert candidate["task_id"] == final["id"] and candidate["item_id"] == s.item["id"]
        assert candidate["owner_account_id"] == owner_id and candidate["version"] == 1
        assert len(candidate["content_executions"]) == 1
        assert not s.store.rows(
            "SELECT * FROM execution_jobs WHERE kind IN ('hf_publish','hf_verify')"
        )
    submits = [
        r
        for r in s.fake.requests
        if r.method == "POST"
        and json.loads(r.content).get("operation") in {"social_submit", "reddit_submit"}
    ]
    assert len(submits) == 1 and len(s.store.rows("SELECT * FROM attempts")) == 1
    if scenario == "receipt_after_stop_restart":
        assert s.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"] == "1"


@pytest.mark.parametrize("social_pilot", ["x"], indirect=True)
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_hf_production_to_phone_and_origin_result(social_pilot, scenario):
    s = social_pilot
    candidate_id, owner_id = hf_task(s)
    execute(s, "x", scenario, candidate_id, owner_id)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_five_app_campaign_to_original_phone_task(social_pilot, scenario):
    s = social_pilot
    campaign_id = campaign_task(s, s.app)
    task_id = s.task["id"]
    execute(s, s.app, scenario)
    assert s.engine.task(task_id)["campaign_id"] == campaign_id
    assert len(s.store.rows("SELECT * FROM tasks WHERE campaign_id=?", (campaign_id,))) == 1


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_reddit_campaign_to_original_phone_task(reddit_pilot, scenario):
    s = reddit_pilot
    campaign_task(s, "reddit")
    execute(s, "reddit", scenario)


@pytest.mark.parametrize("social_pilot", ["x"], indirect=True)
def test_malformed_origin_does_not_roll_back_native_receipt(social_pilot):
    s = social_pilot
    candidate_id, _ = hf_task(s)
    with s.store.connect(write=True) as db:
        db.execute("UPDATE content_queue_entries SET context='[]'")
    execute(s, "x", "published")
    candidate = Interactions(s.engine).candidate(candidate_id)
    assert candidate["state"] == "PUBLISHING"
    assert candidate["content_executions"][0]["verified_binding"] is False
    assert s.engine.task(s.task["id"])["items"][0]["status"] == "confirmed"
