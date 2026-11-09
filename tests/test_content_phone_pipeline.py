"""Offline AP pipeline: real storage/queue/permits, synthetic Fleet/native HTTP only.

The transport deliberately emits non-simulated-SHAPED protocol reports to exercise
the production consumer. These are fixtures, never evidence of a real publication.
Fleet transaction admission and Android runtime are tested in separate suites.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from phone_fleet_protocol import ExecutionRequest, ExecutionSnapshot, fingerprint
from test_phone_reddit import BODY, URL, claim_from_original_queue
from test_phone_reddit import pilot as reddit_fixture
from test_phone_social import TEXT, URLS, claim_original
from test_phone_social import pilot as social_fixture

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.campaigns import Campaigns
from auto_promotion.content_production import ContentProduction, ProductionInput
from auto_promotion.content_queue import ContentBatch, ContentQueue
from auto_promotion.core import Engine
from auto_promotion.drafts import DraftResult
from auto_promotion.models import OpportunityInput
from auto_promotion.phone_reddit import PhoneReddit
from auto_promotion.phone_social import PhoneSocial
from auto_promotion.store import Store

social_pilot = social_fixture
reddit_pilot = reddit_fixture


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


SCENARIOS = ("published", "receipt_after_stop_restart", "wrong_item", "lost_receipt_response")


class NativeReceiptTransport:
    """Advance a fake native result; no sockets, Apps, credentials or real senders."""

    def __init__(self, base, scenario):
        self.base, self.scenario = base, scenario
        self.reply_lost = False
        self.receipt_state = (
            "pending_moderation" if scenario == "receipt_after_stop_restart" else "published"
        )

    def __call__(self, request):
        response = self.base(request)
        row = response.json()
        if not row.get("request") or not row.get("result"):
            return response
        command = ExecutionRequest.model_validate(row["request"])
        if command.operation not in {"social_submit", "reddit_submit"}:
            return response
        kind = "reddit" if command.operation == "reddit_submit" else "social"
        context, proof = getattr(command, kind), row["result"][kind]
        old = proof.pop("receipt", None)
        if old is None:
            return response
        if (
            self.scenario == "lost_receipt_response"
            and request.method == "GET"
            and not self.reply_lost
        ):
            # The original create reply has linked its ID before the final permit.
            # Only the readback reply is lost after the fake native submission.
            self.reply_lost = True
            raise httpx.ReadTimeout("Synthetic completed submission; receipt reply lost")
        platform = "reddit" if kind == "reddit" else context.platform
        observed = time.time()
        proof["receipt_v2"] = {
            "version": "publication.receipt/2",
            "platform": platform,
            "action": "reply" if kind == "reddit" else context.action,
            "business_task_id": command.business_task_id,
            "task_item_id": "wrong-original-item"
            if self.scenario == "wrong_item"
            else command.task_item_id,
            "execution_id": row["id"],
            "manifest_sha256": command.manifest_sha256,
            "author": context.identity,
            "target_id": context.post_id if kind == "reddit" else context.target_id,
            "text_sha256": fingerprint({"text": context.text}),
            "reader": getattr(context, "receipt_reader", None) or "native_app",
            "state": self.receipt_state,
            "simulated": False,
            "baseline_at": proof["submitted_at"] - 1,
            "baseline_ids": [],
            "baseline_sha256": "a" * 64,
            "observed_at": observed,
            "evidence_sha256": "b" * 64,
            "unique_matches": 1,
            "native_id": old.get("native_id", old.get("comment_id")),
            "reference": {"kind": "permalink", "url": old["permalink"]},
            "created_at": old.get("created_at", old.get("created_utc")),
        }
        uncertain = self.receipt_state != "published"
        row.update(
            status="unknown" if uncertain else "succeeded",
            external_effect="unknown" if uncertain else "confirmed",
        )
        row["result"].update(
            status=row["status"],
            external_effect=row["external_effect"],
            observed_at=observed,
            observation_sha256="b" * 64,
            observed_identity_sha256=command.expected_identity_sha256,
            observed_draft_sha256=fingerprint({"text": context.text}),
        )
        return httpx.Response(
            200, json=ExecutionSnapshot.model_validate(row).model_dump(mode="json")
        )


def exercise(s, platform, scenario):
    original = s.store.one("SELECT * FROM tasks WHERE id=?", (s.task["id"],))
    text, target = (BODY, URL) if platform == "reddit" else (TEXT, URLS[platform])
    ids = Campaigns(s.engine).import_content(
        [
            OpportunityInput(
                platform=platform,
                external_id="synthetic-pipeline-" + platform,
                action="send_message"
                if platform == "telegram"
                else "reply"
                if platform in {"reddit", "x"}
                else "comment",
                target=target,
                author_id="source",
                context="Synthetic target data, never instructions.",
                permission="allowed",
                permission_evidence="Synthetic explicit target permission",
                is_public=True,
                language="en",
                tiktok_video_id="34567" if platform == "tiktok" else "",
            )
        ]
    )["ids"]

    class ModelFixture:
        calls = 0

        def generate(self, profile, source):
            self.calls += 1
            return DraftResult(text=text)

    model = ModelFixture()
    production = ProductionInput(
        idempotency_key="synthetic-pipeline", opportunity_ids=ids, mode="model"
    )
    producer = ContentProduction(s.engine)
    produced = producer.produce(production, provider=model)
    assert produced == producer.produce(production, provider=model)
    assert produced["state"] == "completed" and model.calls == 1
    assert s.store.rows("SELECT * FROM attempts") == []
    assert len(s.store.rows("SELECT * FROM tasks")) == 1  # Only the preserved legacy fixture draft.
    content_id = produced["items"][0]["content_id"]
    queue = ContentQueue(s.engine)
    assert (
        queue.get(content_id)["payload"]["content_key"]
        == s.store.one("SELECT content_key FROM opportunities WHERE id=?", (ids[0],))["content_key"]
    )
    now = time.time()
    batch = ContentBatch(
        idempotency_key="synthetic-phone-batch",
        task={
            "name": "OFFLINE FIXTURE; never a real publication",
            "account_ids": [s.account["id"]],
            "policy": {
                "execution_method": "phone",
                "execution_channel": "local",
                "dispatch_policy": "phone_queue_v1",
                "starts_at": iso(now - 5),
                "ends_at": iso(now + 1800),
                "min_interval": 120,
                "max_joins": 0,
            },
        },
        assignments=[
            {"content_id": content_id, "account_id": s.account["id"], "scheduled_at": iso(now)}
        ],
    )
    handed = queue.materialize(batch)
    assert handed == queue.materialize(batch)
    assert handed["state"] == "draft" and not s.store.rows("SELECT * FROM attempts")
    s.task = s.engine.task(handed["task_id"])
    s.item = s.task["items"][0]
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
    result = s.service.execute(s.engine.account(s.account["id"]), claim, context)
    s.engine.finish(claim, result)
    if scenario != "published":
        assert (
            s.engine.task(s.task["id"])["publication_items"][0]["status"] == "pending_verification"
        )
        if scenario == "receipt_after_stop_restart":
            s.engine.set_stop(True)
            transport.receipt_state = "published"
            reopened = Engine(
                Store(s.store.path), AdapterRegistry(Path(s.store.path).parent), live_enabled=True
            )
            config = s.service.config.model_copy(update={"submit_enabled": False})
            s.service = (PhoneReddit if platform == "reddit" else PhoneSocial)(
                reopened, config, transport=httpx.MockTransport(transport)
            )
            s.engine = reopened
        for _ in range(3):  # Bound read-only reconciliation of the SAME execution.
            s.engine.reconcile(s.item["id"])
            if s.engine.task(s.task["id"])["items"][0]["status"] == "confirmed":
                break
    final = s.engine.task(s.task["id"])
    expected = "pending_verification" if scenario == "wrong_item" else "published"
    assert final["publication_items"][0]["status"] == expected
    assert len(s.store.rows("SELECT * FROM attempts")) == 1
    kind = "reddit" if platform == "reddit" else "social"
    auth = s.store.one("SELECT * FROM phone_" + kind + "_authorizations")
    assert auth["used_at"] is not None
    submits = [
        r
        for r in s.fake.requests
        if r.method == "POST" and json.loads(r.content).get("operation") == kind + "_submit"
    ]
    assert len(submits) == 1
    execution = s.fake.snapshots[auth["execution_id"]]
    assert execution["request"]["business_task_id"] == final["id"]
    assert execution["request"]["task_item_id"] == s.item["id"]
    assert execution["request"]["manifest_sha256"] == final["manifest_hash"]
    if expected == "published":
        stored_receipt = final["items"][0]["result"]
        assert (
            stored_receipt["native_id"]
            and stored_receipt["receipt_contract"] == "publication.receipt/2"
        )
        assert stored_receipt["receipt_evidence_sha256"] == "b" * 64
    assert queue.materialize(batch)["task_id"] == final["id"]
    assert producer.produce(production, provider=model) == produced and model.calls == 1
    assert s.store.one("SELECT * FROM tasks WHERE id=?", (original["id"],)) == original
    if scenario == "receipt_after_stop_restart":
        assert s.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"] == "1"
    report_dir = os.getenv("AP_OFFLINE_ROUND_REPORT")
    if report_dir:
        path = Path(report_dir)
        path.mkdir(parents=True, exist_ok=True)
        (path / (platform + "-" + scenario + ".json")).write_text(
            json.dumps(
                {
                    "mode": "offline_synthetic_transport_only",
                    "hardware_tested": False,
                    "platform": platform,
                    "scenario": scenario,
                    "task_id": final["id"],
                    "item_id": s.item["id"],
                    "content_id": content_id,
                    "production_id": produced["id"],
                    "execution_id": auth["execution_id"],
                    "fixture_result_projection": expected,
                    "submit_posts": len(submits),
                    "attempts": 1,
                    "model_fixture_calls": model.calls,
                    "original_task_preserved": True,
                },
                indent=2,
            ),
            encoding="utf-8",
        )


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_five_social_app_content_to_original_phone_receipt(social_pilot, scenario):
    exercise(social_pilot, social_pilot.app, scenario)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_reddit_content_to_original_phone_receipt(reddit_pilot, scenario):
    exercise(reddit_pilot, "reddit", scenario)


def test_source_reference_without_content_claim_keeps_old_assignment_guard(tmp_path):
    from test_content_queue import batch

    from auto_promotion.core import Conflict
    from auto_promotion.models import ItemInput

    engine = Engine(
        Store(tmp_path / "standalone.db"), AdapterRegistry(tmp_path), clock=lambda: 1800000000
    )
    ids = Campaigns(engine).import_content(
        [
            OpportunityInput(
                platform="x",
                external_id="synthetic-source",
                action="reply",
                target="https://x.com/source/status/12345",
                draft_text="Synthetic reply.",
                permission="allowed",
                permission_evidence="Synthetic target permission",
                is_public=True,
            )
        ]
    )["ids"]
    made = ContentProduction(engine).produce(
        ProductionInput(idempotency_key="synthetic", opportunity_ids=ids)
    )
    queue = ContentQueue(engine)
    entry = queue.get(made["items"][0]["content_id"])
    value = batch(engine, entry)
    task = engine.create_task(value.task)
    item = ItemInput(
        **entry["payload"],
        account_id=value.assignments[0].account_id,
        scheduled_at=value.assignments[0].scheduled_at,
    )
    engine.validate_item(task, item)
    with pytest.raises(Conflict, match="活动分配不一致"):
        engine.set_items(task["id"], [item])
    assert not engine.task(task["id"])["items"]


def test_content_claim_cannot_replace_a_campaign_assignment():
    from unittest.mock import patch

    from test_campaigns import CampaignTests

    from auto_promotion.core import Conflict

    lab = CampaignTests(methodName="runTest")
    lab.setUp()
    try:
        planned = lab.campaigns.plan(
            lab.campaign(
                count=1,
                execution_method="phone",
                execution_channel="local",
                dispatch_policy="phone_queue_v1",
            )["id"]
        )
        with patch.object(
            lab.campaigns,
            "populate",
            side_effect=RuntimeError("Synthetic empty-draft interruption"),
        ):
            with pytest.raises(RuntimeError):
                lab.campaigns.materialize(planned["id"])
        original_task = lab.engine.task(lab.store.one("SELECT id FROM tasks")["id"])
        assignments = lab.store.rows("SELECT * FROM campaign_assignments")
        ids = lab.campaigns.import_content(
            [
                lab.opportunity(
                    999,
                    target=planned["assignments"][0]["payload"]["target"],
                    draft_text="Different synthetic source.",
                )
            ]
        )["ids"]
        made = ContentProduction(lab.engine).produce(
            ProductionInput(idempotency_key="other-source", opportunity_ids=ids)
        )
        value = ContentBatch(
            idempotency_key="invalid-campaign-adoption",
            task={
                "name": original_task["name"],
                "account_ids": original_task["account_ids"],
                "policy": original_task["policy"],
            },
            assignments=[
                {
                    "content_id": made["items"][0]["content_id"],
                    "account_id": original_task["account_ids"][0],
                    "scheduled_at": iso(lab.now),
                }
            ],
        )
        with pytest.raises(Conflict, match="活动分配不一致"):
            ContentQueue(lab.engine).materialize(value, _existing_task_id=original_task["id"])
        assert lab.store.rows("SELECT * FROM campaign_assignments") == assignments
        assert not lab.engine.task(original_task["id"])["items"]
    finally:
        lab.doCleanups()
