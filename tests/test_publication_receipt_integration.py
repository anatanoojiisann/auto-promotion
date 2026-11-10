"""Six-App protocol-to-business integration with synthetic reports, never hardware acceptance."""

import copy
import json
from importlib.resources import files

import pytest
from phone_fleet_protocol import ExecutionRequest, ExecutionSnapshot, RuntimeResult, fingerprint

from auto_promotion.content_queue import publication_status
from auto_promotion.phone_reddit import PhoneReddit
from auto_promotion.phone_social import PhoneSocial

CASES = json.loads(
    files("phone_fleet_protocol").joinpath("schemas/receipt-v2.synthetic.json").read_text()
)


def report(case):
    platform = case["platform"]
    if platform == "reddit":
        payload = {
            "target_url": "https://www.reddit.com/r/synthetic/comments/abc123/post/",
            "post_id": "abc123",
            "subreddit": "synthetic",
            "title": "Synthetic target",
            "author": "source",
            "identity": case["author"],
            "text": "Synthetic fixture; never publish.",
            "authorization_id": "synthetic-authority",
        }
        kind, context = "reddit", payload
    else:
        targets = {
            "telegram": "https://t.me/test_group",
            "x": "https://x.com/source/status/12345",
            "instagram": "https://www.instagram.com/p/ABC123/",
            "facebook": "https://www.facebook.com/source/posts/23456",
            "tiktok": "https://www.tiktok.com/@source/video/34567",
        }
        context = {
            "platform": platform,
            "action": case["action"],
            "identity": case["author"],
            "target_url": targets[platform],
            "target_id": case["target_id"],
            "target_author": "@test_group"
            if platform == "telegram"
            else "source"
            if platform == "facebook"
            else "@source",
            "target_text": "Synthetic target",
            "text": "Synthetic fixture; never publish.",
            "authorization_id": "synthetic-authority",
            "expected_adapter_sha256": "c" * 64,
            "discard_after_prepare": False,
        }
        if platform == "telegram":
            context["receipt_reader"] = "telegram_api"
        kind = "social"
    request = ExecutionRequest(
        business_task_id=case["business_task_id"],
        task_item_id=case["task_item_id"],
        manifest_sha256=case["manifest_sha256"],
        account_id="synthetic-account",
        device_id="synthetic-device",
        operation=kind + "_submit",
        execution_channel="local",
        not_before=99,
        deadline=120,
        expected_identity_sha256=fingerprint({"account": case["author"]}),
        **{kind: context},
    )
    # Synthetic transport fixture shaped as an independently observed native report.
    receipt = {**case, "simulated": False}
    proof = {
        "identity": case["author"],
        "submission_started": True,
        "submitted_at": 101,
        "receipt_v2": receipt,
    }
    if kind == "social":
        proof.update(platform=platform, adapter_id="synthetic-adapter", adapter_sha256="c" * 64)
    result = RuntimeResult(
        execution_id=case["execution_id"],
        command_id="synthetic-command",
        device_id="synthetic-device",
        status="succeeded",
        external_effect="confirmed",
        simulated=False,
        quiescent=True,
        **{kind: proof},
    )
    snapshot = ExecutionSnapshot(
        id=case["execution_id"],
        request=request,
        status="succeeded",
        desired="run",
        revision=3,
        created_at=99,
        updated_at=104,
        blocked_reason=None,
        command_id="synthetic-command",
        device_id="synthetic-device",
        node_id="synthetic-node",
        external_effect="confirmed",
        result=result,
    ).model_dump(mode="json")
    return snapshot, request.reddit if platform == "reddit" else request.social, kind


def consume(snapshot, context, kind):
    return (PhoneReddit if kind == "reddit" else PhoneSocial).outcome(snapshot, context)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["platform"])
def test_real_wire_models_feed_original_business_result_and_six_state_projection(case):
    snapshot, context, kind = report(case)
    # Round trip across the actual strict internal protocol, with no HTTP or platform call.
    snapshot = ExecutionSnapshot.model_validate_json(json.dumps(snapshot)).model_dump(mode="json")
    result = consume(snapshot, context, kind)
    assert result["verified"] is True
    assert result["native_id"] == case["native_id"]
    assert result["receipt_contract"] == "publication.receipt/2"
    assert (
        publication_status(
            {
                "id": "synthetic-item",
                "status": result["status"],
                "result": result,
                "external_reference": result["external_reference"],
            }
        )["status"]
        == "published"
    )
    snapshot["result"]["simulated"] = True
    assert consume(snapshot, context, kind)["status"] == "unknown"


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["platform"])
@pytest.mark.parametrize(
    "change",
    [
        "old_item",
        "other_manifest",
        "other_execution",
        "other_command",
        "moderation",
        "cancelled",
        "old_baseline",
    ],
)
def test_lost_response_stop_and_unbound_evidence_remain_query_only(case, change):
    snapshot, context, kind = report(case)
    receipt = snapshot["result"][kind]["receipt_v2"]
    if change == "old_item":
        receipt["task_item_id"] = "other-item"
    elif change == "other_manifest":
        receipt["manifest_sha256"] = "d" * 64
    elif change == "other_execution":
        receipt["execution_id"] = "other-execution"
    elif change == "other_command":
        snapshot["result"]["command_id"] = "other-command"
    elif change == "moderation":
        receipt["state"] = "pending_moderation"
    elif change == "cancelled":
        snapshot["status"] = "unknown"
        snapshot["desired"] = "cancelled"
    elif change == "old_baseline":
        receipt["baseline_at"] = 103
    original = copy.deepcopy(snapshot)
    result = consume(snapshot, context, kind)
    assert result["status"] == "unknown" and result["verified"] is False
    assert snapshot == original  # Verification cannot change the original attempt or queue.


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["platform"])
def test_invalid_native_evidence_is_unknown_not_a_retry_or_success(case):
    snapshot, context, kind = report(case)
    snapshot["result"][kind]["receipt_v2"]["reference"]["url"] = "https://evil.test/not-evidence"
    assert consume(snapshot, context, kind)["status"] == "unknown"
