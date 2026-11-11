"""Synthetic receipt service; these tests never open a browser or send externally."""

from copy import deepcopy

import pytest
from test_phone_social import claim_original
from test_phone_social import pilot as _pilot

from auto_promotion.backends.common import BackendError
from auto_promotion.phone_receipt_recovery import run_due

pilot = _pilot


def uncertain(s):
    claim, context = claim_original(s)
    s.fake.authorize = lambda body: s.service.authorize(
        body, "synthetic-runtime-token-of-sufficient-length"
    )
    s.service.execute(s.engine.account(s.account["id"]), claim, context)
    s.engine.finish(
        claim,
        {
            "status": "unknown",
            "verified": False,
            "submission_started": True,
            "reason": "synthetic_pending",
        },
    )
    original = [
        r for r in s.fake.snapshots.values() if r["request"]["operation"] == "social_submit"
    ][0]
    receipt = deepcopy(original["result"]["social"]["receipt"])
    original.update(status="unknown", external_effect="unknown")
    original["result"]["status"] = "unknown"
    original["result"]["social"]["receipt"] = None
    original["result"]["external_effect"] = "unknown"
    request = s.service.request
    calls = []
    state = {"phase": "pending"}

    def fake(method, path, body=None, *args, **kwargs):
        if "/receipt-checks" not in path:
            return request(method, path, body, *args, **kwargs)
        calls.append((method, path, body))
        if method == "DELETE":
            state["phase"] = "cancelled"
            return {"cancelled": 1}
        proof = {
            "state": "found",
            "receipt": receipt,
            "baseline_sha256": "b" * 64,
            "execution_id": original["id"],
            "command_id": original["command_id"],
            "device_id": original["device_id"],
            "node_id": original["node_id"],
            "command_sha256": "c" * 64,
        }
        return {
            "id": "receipt-test",
            "execution_id": original["id"],
            "command_id": original["command_id"],
            "command_sha256": "c" * 64,
            "phase": state["phase"],
            "observations": [proof] if state["phase"] == "confirmed" else [],
        }

    s.monkeypatch.setattr(s.service, "request", fake)
    claim = {**claim, "allow_receipt_lookup": True}
    return claim, original, receipt, calls, state


@pytest.mark.parametrize("pilot", ["facebook", "instagram"], indirect=True)
def test_normal_verify_recovers_id_without_another_attempt_or_execution(pilot):
    s = pilot
    claim, original, receipt, calls, state = uncertain(s)
    before = len(s.fake.requests)
    result = s.engine.reconcile(s.item["id"], phone_receipt_lookup=True)
    assert result["status"] == "unknown" and calls[0][0] == "POST"
    state["phase"] = "confirmed"
    with s.store.connect(write=True) as db:
        db.execute("UPDATE phone_social_receipt_jobs SET next_at=0")
    result = s.engine.reconcile(s.item["id"], phone_receipt_lookup=True)
    assert result["status"] == "confirmed" and result["comment_id"] == "98765"
    assert s.store.one("SELECT count(*) n FROM attempts")["n"] == 1
    assert s.store.one("SELECT count(*) n FROM phone_social_authorizations")["n"] == 1
    assert len([r for r in s.fake.requests[before:] if r.method == "POST"]) == 0
    assert original["status"] == "unknown" and original["result"]["social"]["receipt"] is None


@pytest.mark.parametrize("pilot", ["facebook", "instagram"], indirect=True)
def test_pause_after_completion_cancels_pending_read_and_preserves_unknown(pilot):
    s = pilot
    claim, original, receipt, calls, state = uncertain(s)
    s.engine.reconcile(s.item["id"], phone_receipt_lookup=True)
    s.engine.change_state(s.task["id"], "paused")
    run_due(s.service)
    assert calls[-1][0] == "DELETE"
    assert (
        s.store.one("SELECT status FROM items WHERE id=?", (s.item["id"],))["status"] == "unknown"
    )
    count = len(calls)
    assert (
        s.service.verify(s.engine.account(s.account["id"]), claim)["reason"]
        == "PhoneSocialReceiptPaused"
    )
    assert len(calls) == count


@pytest.mark.parametrize("pilot", ["instagram"], indirect=True)
def test_wrong_original_or_new_receipt_never_confirms(pilot):
    s = pilot
    claim, original, receipt, calls, state = uncertain(s)
    s.engine.reconcile(s.item["id"], phone_receipt_lookup=True)
    receipt["author"] = "@wrong"
    state["phase"] = "confirmed"
    with s.store.connect(write=True) as db:
        db.execute("UPDATE phone_social_receipt_jobs SET next_at=0")
    with pytest.raises(BackendError):
        s.service.verify(s.engine.account(s.account["id"]), claim)
    assert (
        s.store.one("SELECT status FROM items WHERE id=?", (s.item["id"],))["status"] == "unknown"
    )


@pytest.mark.parametrize("pilot", ["instagram"], indirect=True)
def test_stop_during_read_wins_over_late_confirmation(pilot):
    s = pilot
    claim, original, receipt, calls, state = uncertain(s)
    s.service.verify(s.engine.account(s.account["id"]), claim)
    state["phase"] = "confirmed"
    request = s.service.request

    def pause_during(method, path, body=None, *args, **kwargs):
        value = request(method, path, body, *args, **kwargs)
        if "/receipt-checks" in path:
            s.service.pause_receipts(s.task["id"])
        return value

    s.monkeypatch.setattr(s.service, "request", pause_during)
    with s.store.connect(write=True) as db:
        db.execute("UPDATE phone_social_receipt_jobs SET next_at=0")
    result = s.service.verify(s.engine.account(s.account["id"]), claim)
    assert result["status"] == "unknown" and result["reason"] == "PhoneSocialReceiptPaused"


@pytest.mark.parametrize("pilot", ["instagram"], indirect=True)
def test_statistics_cannot_start_read_and_resume_is_explicit(pilot):
    s = pilot
    claim, original, receipt, calls, state = uncertain(s)
    s.service.config = s.service.config.model_copy(update={"receipt_followup_enabled": True})
    assert s.engine.reconcile(s.item["id"])["status"] == "unknown"
    assert not calls
    s.engine.reconcile(s.item["id"], phone_receipt_lookup=True)
    first = calls[0][2]["request_key"]
    s.service.pause_receipts(s.task["id"])
    run_due(s.service)
    assert (
        s.service.verify(s.engine.account(s.account["id"]), claim)["reason"]
        == "PhoneSocialReceiptPaused"
    )
    s.engine.reconcile(s.item["id"], phone_receipt_lookup=True, resume_phone_receipt=True)
    keys = [body["request_key"] for method, path, body in calls if method == "POST"]
    assert len(keys) == 2 and keys[-1] != first
    assert s.store.one("SELECT count(*) n FROM attempts")["n"] == 1
    assert s.store.one("SELECT count(*) n FROM phone_social_authorizations")["n"] == 1


@pytest.mark.parametrize("pilot", ["instagram"], indirect=True)
def test_global_stop_fences_existing_and_new_reads_until_explicit_resume(pilot):
    s = pilot
    claim, original, receipt, calls, state = uncertain(s)
    s.engine.reconcile(s.item["id"], phone_receipt_lookup=True)
    s.engine.set_stop(True)
    assert (
        s.service.verify(s.engine.account(s.account["id"]), claim)["reason"]
        == "PhoneSocialReceiptPaused"
    )
    run_due(s.service)
    assert state["phase"] == "cancelled"
    posts = len([call for call in calls if call[0] == "POST"])
    s.engine.reconcile(s.item["id"], phone_receipt_lookup=True, resume_phone_receipt=True)
    assert len([call for call in calls if call[0] == "POST"]) == posts
    s.engine.set_stop(False)
    run_due(s.service)
    s.engine.reconcile(s.item["id"], phone_receipt_lookup=True)
    assert len([call for call in calls if call[0] == "POST"]) == posts
    s.engine.reconcile(s.item["id"], phone_receipt_lookup=True, resume_phone_receipt=True)
    assert len([call for call in calls if call[0] == "POST"]) == posts + 1
    assert s.store.one("SELECT count(*) n FROM attempts")["n"] == 1
    assert s.store.one("SELECT count(*) n FROM phone_social_authorizations")["n"] == 1


@pytest.mark.parametrize("pilot", ["instagram"], indirect=True)
def test_global_stop_before_first_receipt_job_never_starts_one(pilot):
    s = pilot
    claim, original, receipt, calls, state = uncertain(s)
    s.engine.set_stop(True)
    s.engine.reconcile(s.item["id"], phone_receipt_lookup=True)
    assert not calls
    assert s.store.one("SELECT count(*) n FROM phone_social_receipt_jobs")["n"] == 0


@pytest.mark.parametrize("pilot", ["instagram"], indirect=True)
def test_evidence_export_links_original_attempt_native_command_and_receipt_without_reading_platform(
    pilot,
):
    from auto_promotion.task_tools import TaskTools

    s = pilot
    claim, original, receipt, calls, state = uncertain(s)
    s.engine.reconcile(s.item["id"], phone_receipt_lookup=True)
    before = len(calls)
    evidence = TaskTools(s.engine).evidence(s.task["id"])
    chain = evidence["phone_execution_chain"]
    assert len(chain) == 1
    assert chain[0]["execution_id"] == original["id"]
    assert chain[0]["attempt_id"] == claim["attempt_id"]
    assert chain[0]["native_command_id"]
    assert chain[0]["receipt"]["check_id"] == "receipt-test"
    assert len(calls) == before
    assert "synthetic-runtime-token" not in str(evidence)
    with s.store.connect() as db:
        from auto_promotion.phone_receipt_diagnostics import chain as read_chain

        with pytest.raises(TimeoutError):
            read_chain(db, evidence["items"], budget_seconds=0)
        assert db.execute("SELECT 1").fetchone()[0] == 1
