"""Synthetic API/fleet responses exercise the real persistent AP queue and permits."""

import json
import time

import httpx
import pytest
from phone_fleet_protocol import ExecutionRequest, ExecutionSnapshot, fingerprint
from phone_fleet_protocol.visual_review import context_sha256

from auto_promotion.account_control_state import account_check
from auto_promotion.phone_social import PhoneSocial
from auto_promotion.phone_visual_preparation import prepare_task, trusted_ready
from auto_promotion.queue_store import JobQueue, job_scope
from auto_promotion.task_channels import TaskChannelRouter
from auto_promotion.temporal_tasks import admit, commission_dynamic_admission
from tests.test_phone_social import pilot as pilot
from tests.test_workflow_unification import configure_single_pass


@pytest.fixture
def async_pilot(pilot):
    s = pilot
    configure_single_pass(s)
    s.service.config = s.service.config.model_copy(
        update={
            "dynamic_task_authorization_enabled": True,
            "async_visual_review_enabled": True,
            "authorized_task_ids": [],
            "single_pass_task_ids": [],
        }
    )
    with s.monkeypatch.context() as patch:
        patch.setattr(s.engine, "clock", lambda: s.task["created_at"])
        commission_dynamic_admission(s.service)
    base = s.service.transport
    reviews = {}
    settings = {"lose_reply": False}

    def transport(request):
        if "/visual-reviews/" in request.url.path:
            return httpx.Response(200, json=reviews[request.url.path.rsplit("/", 1)[1]])
        response = base.handle_request(request)
        body = json.loads(request.content) if request.method == "POST" and request.content else {}
        if body.get("operation") == "social_prepare" and body.get("social", {}).get(
            "defer_visual_review"
        ):
            value = response.json()
            typed = ExecutionRequest.model_validate(body)
            checkpoint = {
                "review_id": "review-" + value["id"],
                "image_sha256": "b" * 64,
                "context_sha256": context_sha256(typed.social),
                "expires_at": time.time() + 600,
            }
            value.update(
                status="waiting_for_human",
                blocked_reason="visual_review_pending",
                resource_released=True,
            )
            value["result"].update(
                status="waiting_for_human",
                code="visual_review_pending",
                observation_sha256=checkpoint["image_sha256"],
            )
            value["result"]["social"].update(
                visual_review=checkpoint,
                submit_control_observed=False,
                discarded=False,
                text_sha256=fingerprint({"text": typed.social.text}),
            )
            s.fake.snapshots[value["id"]] = value
            reviews[checkpoint["review_id"]] = {
                **checkpoint,
                "id": checkpoint["review_id"],
                "execution_id": value["id"],
                "phase": "pending",
                "updated_at": time.time(),
            }
            if settings["lose_reply"]:
                settings["lose_reply"] = False
                raise httpx.ReadTimeout("Synthetic lost native preparation reply")
            return httpx.Response(200, json=value)
        return response

    s.service.transport = httpx.MockTransport(transport)
    s.engine.start_task(s.task["id"], source="synthetic_explicit_async_start")
    link = s.store.one("SELECT * FROM temporal_task_links WHERE task_id=?", (s.task["id"],))
    assert admit(s.engine, s.task["id"], link["manifest_hash"], link["workflow_id"])["admitted"]
    s.async_link, s.reviews, s.async_settings = link, reviews, settings
    return s


def step(s):
    return prepare_task(
        s.engine, s.task["id"], s.async_link["manifest_hash"], s.async_link["workflow_id"]
    )


def approve(s):
    checkpoint = next(iter(s.reviews.values()))
    checkpoint.update(phase="reviewed", decision_sha256="d" * 64, updated_at=time.time())


@pytest.mark.parametrize("pilot", ["telegram"], indirect=True)
def test_original_item_waits_without_attempt_and_restores_before_single_native_submit(async_pilot):
    s = async_pilot
    queue = JobQueue(s.engine)
    queue.sync(s.task["id"])
    assert queue.claim("telegram.phone", "synthetic-worker", reference_id=s.item["id"]) is None
    state = step(s)
    for native in s.fake.snapshots.values():
        if native["request"]["operation"] == "social_prepare":
            ExecutionSnapshot.model_validate(native)
    assert state["pending_review"] and state["device_held"] is False, [
        (r["phase"], r["reason"]) for r in state["items"]
    ]
    assert s.store.one("SELECT COUNT(*) n FROM attempts")["n"] == 0
    assert s.store.one("SELECT COUNT(*) n FROM phone_social_authorizations")["n"] == 0
    assert queue.claim("telegram.phone", "synthetic-worker", reference_id=s.item["id"]) is None
    # Replace the service object as on process restart; original IDs and request remain.
    original = s.store.one(
        "SELECT * FROM phone_visual_preparations WHERE item_id=?", (s.item["id"],)
    )
    s.service = PhoneSocial(
        s.engine,
        s.service.config.model_dump(mode="python"),
        transport=s.service.transport,
        telegram_observer=s.service.telegram_observer,
    )
    assert step(s)["pending_review"]
    assert s.store.one(
        "SELECT operation_id,native_execution_id FROM phone_visual_preparations WHERE item_id=?",
        (s.item["id"],),
    ) == {
        "operation_id": original["operation_id"],
        "native_execution_id": original["native_execution_id"],
    }
    approve(s)
    assert not step(s)["pending_review"]
    job = queue.claim(
        "telegram.phone", "synthetic-worker", lease_seconds=180, reference_id=s.item["id"]
    )
    assert job
    context, _ = TaskChannelRouter(s.engine).prepare(job)
    with job_scope(job), s.engine.adapters.execution_job(context):
        claim = s.engine._claim(s.item["id"], {"allowed": True})
    s.fake.authorize = lambda body: s.service.authorize(
        body, "synthetic-runtime-token-of-sufficient-length"
    )
    result = s.service.execute(s.engine.account(s.account["id"]), claim, context)
    assert result["verified"] and result["external_reference"].endswith("/98765")
    s.engine.finish(claim, result)
    native = [
        json.loads(r.content)
        for r in s.fake.requests
        if r.method == "POST" and r.url.path.endswith("/executions")
    ]
    preparations = [r for r in native if r["operation"] == "social_prepare"]
    submissions = [r for r in native if r["operation"] == "social_submit"]
    assert len(preparations) == len(submissions) == 1
    assert (
        submissions[0]["social"]["visual_preparation_execution_id"]
        == original["native_execution_id"]
    )
    assert submissions[0]["social"]["visual_review_id"] == original["review_id"]
    assert not submissions[0]["social"].get("defer_visual_review")
    assert (
        s.store.one("SELECT COUNT(*) n FROM phone_social_authorizations WHERE used_at IS NOT NULL")[
            "n"
        ]
        == 1
    )


@pytest.mark.parametrize("pilot", ["telegram"], indirect=True)
def test_lost_preparation_create_reply_only_looks_up_original_native_execution(async_pilot):
    s = async_pilot
    s.async_settings["lose_reply"] = True
    assert step(s)["items"][0]["phase"] == "waiting_connection"
    assert step(s)["pending_review"]
    creates = [
        r
        for r in s.fake.requests
        if r.method == "POST" and json.loads(r.content).get("operation") == "social_prepare"
    ]
    assert len(creates) == 1
    assert s.store.one("SELECT COUNT(*) n FROM attempts")["n"] == 0


@pytest.mark.parametrize("pilot", ["telegram"], indirect=True)
def test_old_identity_cache_is_not_refreshed_by_approval_and_account_stops_still_win(async_pilot):
    s = async_pilot
    step(s)
    approve(s)
    step(s)
    stale = time.time() - 400
    with s.store.connect(write=True) as db:
        db.execute(
            "UPDATE phone_social_accounts SET observed_at=? WHERE account_id=?",
            (stale, s.account["id"]),
        )
    assert trusted_ready(s.service, s.account["id"], s.task["id"], item_id=s.item["id"])
    result = account_check(s.engine, s.engine.task(s.task["id"]), s.engine.account(s.account["id"]))
    assert (
        result["ready"]
        and result["identity_verified"] is False
        and result["native_identity_check_pending"]
    )
    assert (
        s.store.one(
            "SELECT observed_at FROM phone_social_accounts WHERE account_id=?", (s.account["id"],)
        )["observed_at"]
        == stale
    )
    with s.store.connect(write=True) as db:
        db.execute("UPDATE accounts SET disabled=1 WHERE id=?", (s.account["id"],))
    assert not account_check(
        s.engine, s.engine.task(s.task["id"]), s.engine.account(s.account["id"])
    )["ready"]
    queue = JobQueue(s.engine)
    queue.sync(s.task["id"])
    assert queue.claim("telegram.phone", "synthetic-worker", reference_id=s.item["id"]) is None


@pytest.mark.parametrize("pilot", ["telegram"], indirect=True)
@pytest.mark.parametrize("expire", [False, True])
def test_observer_lane_wait_does_not_dispatch_or_fail_and_is_bounded(async_pilot, expire):
    from auto_promotion.dispatcher import AccountLanes

    s = async_pilot
    before = len(s.fake.requests)
    with AccountLanes(s.store).acquire(s.account["id"]):
        value = step(s)
        assert value["items"][0]["phase"] == "waiting_account" and value["account_waits"]
        first = s.store.one(
            "SELECT * FROM phone_visual_preparations WHERE item_id=?", (s.item["id"],)
        )
        assert not first["request"] and not first["native_execution_id"]
        step(s)
        assert (
            s.store.one(
                "SELECT updated_at FROM phone_visual_preparations WHERE item_id=?", (s.item["id"],)
            )["updated_at"]
            == first["updated_at"]
        )
        assert len(s.fake.requests) == before
        if expire:
            s.monkeypatch.setattr(s.engine, "clock", lambda: first["updated_at"] + 121)
            assert step(s)["items"][0]["reason"] == "account_read_wait_timeout"
    if not expire:
        assert step(s)["pending_review"]
    assert s.store.one("SELECT COUNT(*) n FROM attempts")["n"] == 0
    assert s.store.one("SELECT COUNT(*) n FROM phone_social_authorizations")["n"] == 0
