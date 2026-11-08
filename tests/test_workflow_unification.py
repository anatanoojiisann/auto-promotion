"""Behavioral tests for the real AP queue with explicitly synthetic platform responses."""

import json
import sqlite3
import time

import httpx
import pytest
from phone_fleet_protocol import fingerprint

from auto_promotion.account_control_state import snapshot
from auto_promotion.core import Conflict
from auto_promotion.phone_social import CommissionedSinglePass
from auto_promotion.queue_store import JobQueue, job_scope
from auto_promotion.task_channels import TaskChannelRouter
from auto_promotion.telegram_phone_observer import TelegramObserverConfig
from auto_promotion.temporal_tasks import admit, register
from tests.test_phone_social import pilot as pilot


@pytest.mark.parametrize("pilot", ["telegram"], indirect=True)
def test_light_control_read_has_no_queue_scan_and_keeps_stops(pilot, monkeypatch):
    s = pilot
    monkeypatch.setattr(JobQueue, "pools", lambda *_: (_ for _ in ()).throw(AssertionError("overview scan")))
    assert snapshot(s.engine, s.account["id"])["allowed_read"]
    with s.store.connect(write=True) as db:
        db.execute("UPDATE accounts SET disabled=1 WHERE id=?", (s.account["id"],))
    assert snapshot(s.engine, s.account["id"])["reason"] == "account_disabled"
    with s.store.connect(write=True) as db:
        db.execute("UPDATE accounts SET disabled=0 WHERE id=?", (s.account["id"],))
        db.execute("UPDATE meta SET value='1' WHERE key='global_stop'")
    assert snapshot(s.engine, s.account["id"])["reason"] == "global_stop"


@pytest.mark.parametrize("pilot", ["telegram"], indirect=True)
def test_selected_phone_evidence_does_not_overwrite_desktop_connection(pilot):
    s = pilot
    with s.store.connect(write=True) as db:
        db.execute("UPDATE accounts SET status='unhealthy',health=? WHERE id=?",
                   (json.dumps({"reason": "BrowserIdentityExpired"}), s.account["id"]))
    before = s.store.one("SELECT status,health FROM accounts WHERE id=?", (s.account["id"],))
    result = s.engine.account_readiness(s.engine.task(s.task["id"]), s.engine.account(s.account["id"]))
    assert result["ready"] and result["identity_verified"] and result["execution_method"] == "phone"
    assert before == s.store.one("SELECT status,health FROM accounts WHERE id=?", (s.account["id"],))


def configure_single_pass(s):
    binding = s.service.config.bindings[s.account["id"]].model_copy(update={"receipt_reader": "telegram_api"})
    observer = TelegramObserverConfig(backend_label="synthetic-only", user_id=44,
                                      api_account_id=s.account["id"], api_database=str(s.store.path))
    s.service.config = s.service.config.model_copy(update={
        "bindings": {s.account["id"]: binding}, "telegram_observers": {s.account["id"]: observer},
        "submit_enabled": True, "authorized_task_ids": [s.task["id"]], "single_pass_task_ids": [s.task["id"]],
        "single_pass_adapters": {s.account["id"]: CommissionedSinglePass(adapter_id="telegram-synthetic", adapter_sha256="a" * 64)}})
    calls = []

    class Reader:
        def baseline(self, *args):
            calls.append("baseline")
            return {"api_user_id": "44", "peer_id": "-1000000000123", "peer_username": "test_group",
                    "peer_title": "Test group", "identity_sha256": fingerprint({"account": binding.identity}),
                    "joined": True, "can_send": True, "sends_as_self": True, "last_message_id": 10,
                    "observed_at": time.time()}

        def reserve_receipt(self, account_id):
            calls.append("reserve")
            return {"read_identity": "synthetic-identity", "window_end": time.time() + 3600, "reserved": 16}

        def check_receipt_reservation(self, *args, **kwargs):
            pass

    s.service.telegram_observer = Reader()

    def fleet(request):
        response = s.fake(request)
        value = response.json()
        if request.method == "GET" and (value.get("result") or {}).get("social", {}).get("receipt"):
            value["result"]["social"]["receipt"]["source"] = "telegram_api"
            s.fake.snapshots[value["id"]] = value
        return httpx.Response(response.status_code, json=value)

    s.service.transport = httpx.MockTransport(fleet)
    s.monkeypatch.setattr("auto_promotion.core.telegram_blocker", lambda *_: None)
    return calls


@pytest.mark.parametrize("pilot", ["telegram"], indirect=True)
def test_commissioned_item_has_one_submit_pass_and_one_baseline(pilot, monkeypatch):
    s = pilot
    calls = configure_single_pass(s)
    monkeypatch.setattr(s.service, "prepare_telegram_claim", lambda *_: (_ for _ in ()).throw(AssertionError("duplicate prepare")))
    s.engine.start_task(s.task["id"], source="explicit_synthetic_test")
    queue = JobQueue(s.engine)
    queue.sync(s.task["id"])
    job = queue.claim("telegram.phone", "synthetic-worker", lease_seconds=180, reference_id=s.item["id"])
    assert job
    context, _ = TaskChannelRouter(s.engine).prepare(job)
    with job_scope(job), s.engine.adapters.execution_job(context):
        claim = s.engine._claim(s.item["id"], {"allowed": True})
    assert claim
    s.fake.authorize = lambda body: s.service.authorize(body, s.config["runtime_token"])
    before = len(s.fake.requests)
    outcome = s.service.execute(s.engine.account(s.account["id"]), claim, context)
    operations = [json.loads(r.content)["operation"] for r in s.fake.requests[before:] if r.method == "POST"]
    assert operations == ["social_submit"]
    assert calls == ["baseline", "reserve"]
    assert outcome["verified"] and outcome["message_id"] == "98765"
    assert s.store.one("SELECT count(*) n FROM phone_social_authorizations")["n"] == 1


@pytest.mark.parametrize("pilot", ["telegram"], indirect=True)
def test_temporal_gate_precedes_any_pool_claim_and_replay_is_idempotent(pilot, monkeypatch):
    s = pilot
    configure_single_pass(s)
    s.engine.prepare(s.task["id"])
    registered = register(s.engine, s.task["id"], "Explicit synthetic task instruction")
    assert registered["state"] == "registered"
    s.engine.start_task(s.task["id"], source="explicit_synthetic_test")
    queue = JobQueue(s.engine)
    queue.sync(s.task["id"])
    assert queue.claim("telegram.phone", "old-pool") is None
    with pytest.raises(sqlite3.IntegrityError, match="TemporalTaskAdmissionRequired"):
        with s.store.connect(write=True) as db:
            db.execute("UPDATE execution_jobs SET state='leased' WHERE reference_id=?", (s.item["id"],))
    with pytest.raises(Conflict, match="Correlation"):
        admit(s.engine, s.task["id"], "f" * 64, registered["workflow_id"])
    reply = admit(s.engine, s.task["id"], registered["manifest_hash"], registered["workflow_id"])
    assert reply.get("admitted"), reply
    assert admit(s.engine, s.task["id"], registered["manifest_hash"], registered["workflow_id"]) == reply
    assert queue.claim("telegram.phone", "new-pool") is not None
    assert s.store.one("SELECT count(*) n FROM attempts")["n"] == 0


@pytest.mark.parametrize("pilot", ["telegram"], indirect=True)
def test_temporal_registration_never_imports_an_unknown_or_attempted_task(pilot):
    s = pilot
    with s.store.connect(write=True) as db:
        db.execute("UPDATE items SET status='unknown',result=? WHERE id=?", (json.dumps({"submission_started": True}),s.item["id"]))
    before = s.store.one("SELECT status,result FROM items WHERE id=?", (s.item["id"],))
    with pytest.raises(Conflict, match="NewFrozenUnattempted"):
        register(s.engine, s.task["id"], "Cannot import unknown")
    assert before == s.store.one("SELECT status,result FROM items WHERE id=?", (s.item["id"],))
