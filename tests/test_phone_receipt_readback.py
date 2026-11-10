"""Synthetic normal verification path; no real accounts or native submissions."""

import copy
import time
from types import SimpleNamespace

import httpx
import pytest
from phone_fleet_protocol import ExecutionRequest, fingerprint
from phone_fleet_protocol.social import SocialReceipt

from auto_promotion.backends.common import BackendError
from auto_promotion.phone_receipt_readback import reconcile_x_receipt
from auto_promotion.telegram_phone_observer import bound_telegram_receipt


@pytest.fixture
def recorded():
    now = time.time()
    context = {
        "platform": "x",
        "action": "reply",
        "identity": "@Reader",
        "target_author": "@Editor",
        "target_id": "100",
        "target_url": "https://x.com/Editor/status/100",
        "target_text": "Synthetic target",
        "text": "Synthetic reply",
        "expected_adapter_sha256": "a" * 64,
        "authorization_id": "test-auth",
        "discard_after_prepare": False,
    }
    request = ExecutionRequest(
        business_task_id="test-task",
        task_item_id="test-item",
        manifest_sha256="a" * 64,
        operation="social_submit",
        account_id="test-account",
        device_id="test-phone",
        expected_identity_sha256=fingerprint({"account": "@Reader"}),
        execution_channel="local",
        not_before=now - 10,
        deadline=now + 90,
        social=context,
    )
    original = {
        "submission_started": True,
        "submitted_at": now - 1,
        "identity": "@Reader",
        "target_id": "100",
    }
    snapshot = {
        "id": "original-execution",
        "command_id": "original-command",
        "device_id": "test-phone",
        "node_id": "test-node",
        "status": "unknown",
        "result": {"simulated": False, "social": original},
        "resource_released": True,
    }
    receipt = SocialReceipt(
        platform="x",
        action="reply",
        native_id="11",
        target_id="100",
        author="@Reader",
        text_sha256=fingerprint({"text": "Synthetic reply"}),
        created_at=now - 2,
        observed_at=now,
        permalink="https://x.com/Reader/status/11",
    )
    proof = {
        "version": "fleet.receipt-readback/0.1",
        "nonce": "original-readback",
        "execution_id": "original-execution",
        "command_id": "original-command",
        "device_id": "test-phone",
        "node_id": "test-node",
        "command_sha256": "a" * 64,
        "original_result_sha256": fingerprint(original),
        "context_sha256": fingerprint(request.social),
        "baseline_ids": ["9"],
        "baseline_observed_at": now - 5,
        "observed_at": now,
        "reason": "matched",
        "receipt": receipt.model_dump(mode="json"),
        "signature": "A" * 88,
    }
    response = {"phase": "confirmed", "nonce": proof["nonce"], "proof": proof}
    calls = []

    def read(method, path, body):
        calls.append((method, path, body))
        assert path == "/executions/original-execution/receipt-readback" and body == {}
        return copy.deepcopy(response)

    service = SimpleNamespace(request=read)
    return request, snapshot, response, service, calls


def test_only_original_readback_route_can_confirm_without_new_attempt(recorded):
    request, snapshot, response, service, calls = recorded
    before = copy.deepcopy(snapshot)
    output = reconcile_x_receipt(service, request, snapshot)
    assert output["status"] == "confirmed" and output["verified"] and output["reply_id"] == "11"
    assert output["original_runtime_status"] == "unknown" and snapshot == before
    assert calls == [("POST", "/executions/original-execution/receipt-readback", {})]


@pytest.mark.parametrize(
    "bad",
    ["nonce", "execution", "account", "text", "context", "original", "baseline_late", "old_time"],
)
def test_receipt_mismatch_never_becomes_confirmation(recorded, bad):
    request, snapshot, response, service, _ = recorded
    proof = response["proof"]
    if bad == "nonce":
        response["nonce"] = "other"
    if bad == "execution":
        proof["execution_id"] = "another-execution"
    if bad == "account":
        proof["receipt"]["author"] = "@Other"
    if bad == "text":
        proof["receipt"]["text_sha256"] = "b" * 64
    if bad == "context":
        proof["context_sha256"] = "c" * 64
    if bad == "original":
        proof["original_result_sha256"] = "d" * 64
    if bad == "baseline_late":
        proof["baseline_observed_at"] = snapshot["result"]["social"]["submitted_at"] + 2
    if bad == "old_time":
        proof["receipt"]["created_at"] = snapshot["result"]["social"]["submitted_at"] - 3
    assert not reconcile_x_receipt(service, request, snapshot)["verified"]


@pytest.mark.parametrize(
    "unavailable_error",
    [
        BackendError("SyntheticOldFleet"),
        httpx.ReadTimeout("SyntheticReadTimeout"),
        ValueError("SyntheticInvalidJson"),
    ],
)
def test_pending_and_old_server_preserve_unknown(recorded, unavailable_error):
    request, snapshot, response, service, _ = recorded
    response.update(phase="pending", proof=None)
    assert reconcile_x_receipt(service, request, snapshot)["receipt_rejection"] == "pending"

    def unavailable(*_):
        raise unavailable_error

    service.request = unavailable
    assert reconcile_x_receipt(service, request, snapshot) is None


def test_telegram_tolerance_is_two_seconds_once_including_historical_reader():
    from phone_fleet_protocol.social import SocialInteraction

    context = SocialInteraction(
        platform="telegram",
        action="send_message",
        identity="@Reader",
        target_id="test_group",
        target_author="@test_group",
        target_url="https://t.me/test_group",
        target_text="Synthetic group",
        text="Synthetic message",
    )
    for delta, matched in [(-2.001, False), (-2, True), (-1.574, True), (120.001, False)]:
        receipt = SocialReceipt(
            platform="telegram",
            source="telegram_api",
            action="send_message",
            native_id="11",
            target_id="test_group",
            author="@Reader",
            text_sha256=fingerprint({"text": context.text}),
            created_at=1000 + delta,
            observed_at=1300,
            permalink="https://t.me/test_group/11",
        )
        assert bound_telegram_receipt(receipt, context, 1000) is matched


def test_existing_business_verify_reads_original_operation_and_accepts_sidecar(
    recorded, monkeypatch
):
    from auto_promotion.phone_social import PhoneSocial

    request, snapshot, response, service, calls = recorded
    record = {
        "id": "original-operation",
        "account_id": "test-account",
        "item_id": "test-item",
        "request": request.model_dump_json(),
    }
    service.store = SimpleNamespace(one=lambda *_: record)

    def dispatch(original, operation_id, account_id, item_id):
        assert (
            original == request
            and operation_id == "original-operation"
            and account_id == "test-account"
            and item_id == "test-item"
        )
        return snapshot

    service.dispatch = dispatch
    service.reconcile_protocol_receipt = lambda *_: None
    service.outcome = PhoneSocial.outcome
    assert (
        PhoneSocial._verify(service, {"id": "test-account"}, {"item_id": "test-item"})["reply_id"]
        == "11"
    )
    assert len(calls) == 1


def test_cooldown_preserves_last_readonly_rejection(recorded):
    request, snapshot, response, service, _ = recorded
    response.update(phase="unconfirmed", proof={"reason": "before_submission_window"})
    methods = []

    def cached(method, route, body):
        methods.append(method)
        if method == "POST":
            raise BackendError("SyntheticReceiptReadBudgetExhausted")
        return response

    service.request = cached
    output = reconcile_x_receipt(service, request, snapshot)
    assert output["status"] == "unknown" and not output["verified"]
    assert output["receipt_rejection"] == "before_submission_window" and methods == ["POST", "GET"]
