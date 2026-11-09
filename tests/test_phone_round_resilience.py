import sqlite3
import threading
import time
from unittest.mock import patch

from fastapi.testclient import TestClient

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.api import create_app
from auto_promotion.core import Engine
from auto_promotion.phone_rounds import PhoneRounds
from auto_promotion.store import Store


def test_idle_round_tick_does_not_compete_with_existing_writer(tmp_path):
    store = Store(tmp_path / "idle.sqlite")
    rounds = PhoneRounds(Engine(store, AdapterRegistry(tmp_path)))
    writer = sqlite3.connect(store.path, timeout=1)
    try:
        writer.execute("BEGIN IMMEDIATE")
        start = time.monotonic()
        rounds.tick()
        assert time.monotonic() - start < 1
    finally:
        writer.rollback()
        writer.close()


def test_health_survives_phone_round_error_and_reports_recovery(tmp_path):
    resumed = threading.Event()
    calls = []

    def transient_failure(_rounds):
        calls.append(True)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        resumed.set()

    app = create_app(db_path=tmp_path / "health.sqlite", worker=True,
                     report_worker=False, pooled_worker=True, phone_rounds_enabled=True)
    with patch.object(PhoneRounds, "tick", transient_failure):
        with TestClient(app) as client:
            assert resumed.wait(6)
            response = client.get("/health")
            assert response.status_code == 200
            status = response.json()["background"]["reports"]["phone_rounds"]
            assert status["alive"] is True
            assert status["last_error"] == "OperationalError"
            assert status["last_error_details"]["exception_type"] == "OperationalError"
            assert status["last_success_at"] >= status["last_error_at"]


def test_known_failed_member_is_visible_and_other_apps_continue(tmp_path):
    from tests.test_phone_rounds import create, finish, make_lab
    lab = make_lab(tmp_path)
    round_ = create(lab)
    lab.rounds.start(round_["id"])
    for _ in lab.ids:
        lab.rounds.tick()
    finish(lab, "telegram", outcome="failed_known", verified=False)
    snapshot = lab.rounds.get(round_["id"])
    member = next(x for x in snapshot["members"] if x["platform"] == "telegram")
    assert member["stage"] == "failed_known"
    assert snapshot["state"] == "running"
    for platform in ("reddit", "instagram", "facebook", "x", "tiktok"):
        finish(lab, platform)
    lab.rounds.tick()
    snapshot = lab.rounds.get(round_["id"])
    assert snapshot["state"] == "finished"
    assert snapshot["finished_at"] < snapshot["deadline"]
    assert snapshot["confirmed_count"] == 5
    assert not snapshot["all_members_within_budget"]


def test_unknown_outcome_ends_round_without_becoming_success_or_requeued(tmp_path):
    from tests.test_phone_rounds import create, finish, make_lab
    lab = make_lab(tmp_path)
    round_ = create(lab)
    lab.rounds.start(round_["id"])
    for _ in lab.ids:
        lab.rounds.tick()
    for platform in ("telegram", "reddit", "instagram", "facebook", "x", "tiktok"):
        finish(lab, platform, outcome="unknown", verified=False)
    lab.rounds.tick()
    snapshot = lab.rounds.get(round_["id"])
    assert snapshot["state"] == "finished"
    assert all(x["stage"] == "unknown" for x in snapshot["members"])
    starts = list(lab.calls)
    lab.rounds.start(round_["id"])
    lab.rounds.tick()
    assert lab.calls == starts


def test_busy_coordinator_writer_returns_control_within_one_short_wait(tmp_path):
    import sqlite3
    import time

    import pytest

    from tests.test_phone_rounds import create, make_lab
    lab = make_lab(tmp_path)
    round_ = create(lab)
    lab.rounds.start(round_["id"])
    with sqlite3.connect(lab.store.path, isolation_level=None) as other:
        other.execute("BEGIN IMMEDIATE")
        start = time.monotonic()
        with pytest.raises(sqlite3.OperationalError):
            lab.rounds.tick()
        assert time.monotonic() - start < 0.6
        other.rollback()
    lab.rounds.tick()
    assert lab.calls


def test_phone_health_uses_original_phone_task_and_its_own_cache(tmp_path):
    from types import SimpleNamespace

    from tests.test_phone_rounds import make_lab
    lab = make_lab(tmp_path)
    account = lab.store.one("SELECT a.id FROM accounts a WHERE a.platform='telegram'")
    binding = SimpleNamespace(device_id="synthetic-device", node_os="windows")
    lab.engine.adapters.phone_social = SimpleNamespace(shared_account_binding=lambda a: binding)
    calls = []
    def call(account, operation, payload=None, task_id=None):
        calls.append((operation, task_id))
        return {"healthy": True, "service_ready": True, "identity_verified": True,
                "account_identity": "@synthetic_phone", "permissions": ["send_message"]}
    lab.engine.call = call
    lab.engine.check_account(account["id"], force=True, task_id="task-telegram")
    lab.engine.check_account(account["id"], task_id="task-telegram")
    assert calls == [("health", "task-telegram")]


def test_completed_identity_refresh_resumes_only_untouched_original_member(tmp_path):
    from types import SimpleNamespace

    from tests.test_phone_rounds import create, make_lab
    lab = make_lab(tmp_path)
    identifier = create(lab)["id"]
    lab.rounds.start(identifier)
    with lab.store.connect(write=True) as db:
        db.execute("UPDATE phone_round_members SET state='blocked',error='PhoneIdentityRefreshPending' "
                   "WHERE round_id=? AND platform='telegram'", (identifier,))
    lab.engine.adapters.phone_social = SimpleNamespace(fresh=lambda account: True)
    def readiness(rounds, rid, *, platforms=None):
        return {"round_id": rid, "submission_authorized": False,
                "members": [{"platform": m["platform"], "ready": True, "blockers": []}
                            for m in rounds.get(rid)["members"]
                            if platforms is None or m["platform"] in platforms]}
    lab.rounds.preflight_check = readiness
    lab.rounds.tick()
    member = next(m for m in lab.rounds.get(identifier)["members"] if m["platform"] == "telegram")
    assert member["admission_state"] == "pending"
    assert member["attempt_id"] is None


def test_health_operation_dispatches_through_phone_task_route(tmp_path):
    from types import SimpleNamespace

    from tests.test_phone_rounds import make_lab
    lab = make_lab(tmp_path)
    account = lab.engine.account(lab.store.one("SELECT id FROM accounts WHERE platform='telegram'")["id"])
    binding = SimpleNamespace(device_id="synthetic-device", node_os="windows")
    lab.engine.adapters.phone_social = SimpleNamespace(shared_account_binding=lambda a: binding)
    calls = []
    def backend(account, operation, payload=None, task_id=None, *, phone=False):
        calls.append((operation, task_id, phone))
        return {"healthy": True, "identity_verified": True}
    lab.engine._call_unlocked = backend
    lab.engine.call(account, "health", task_id="task-telegram")
    assert calls == [("health", "task-telegram", True)]


def test_explicit_recheck_recovers_only_start_rejected_while_task_still_draft(tmp_path):
    from auto_promotion.core import Conflict
    from tests.test_phone_rounds import create, make_lab
    lab = make_lab(tmp_path)
    rid = create(lab)["id"]
    lab.rounds.start(rid)
    def rejected(task_id, **kwargs):
        raise Conflict("Synthetic health rejection before task activation")
    lab.engine.start_and_run = rejected
    chosen = lab.rounds.tick()
    member = next(m for m in lab.rounds.get(rid)["members"] if m["task_id"] == chosen)
    assert member["stage"] == "blocked"
    check = lab.rounds._begin_check(rid, member["platform"], lab.now[0])
    assert check is not None and check[1] == {member["platform"]}
    assert lab.store.one("SELECT count(*) n FROM attempts")["n"] == 0
    assert lab.store.one("SELECT count(*) n FROM audit WHERE kind='phone_round.start_recheck_requested'")["n"] == 1


def test_shared_telegram_health_preserves_numeric_identity_through_phone_route(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from auto_promotion.models import digest
    from auto_promotion.phone_social import PhoneSocial
    from tests.test_phone_rounds import make_lab
    lab = make_lab(tmp_path)
    aid = lab.store.one("SELECT id FROM accounts WHERE platform='telegram'")["id"]
    original = digest(["telegram", "123456"])
    with lab.store.connect(write=True) as db:
        db.execute("UPDATE accounts SET identity_hash=? WHERE id=?", (original, aid))
    binding = SimpleNamespace(platform="telegram", identity="phone_username",
                              device_id="synthetic-device", node_os="windows")
    service = SimpleNamespace(
        binding=lambda account_id: (lab.engine.account(account_id), binding),
        fresh=lambda account: True,
        shared_account_binding=lambda account: binding,
        config=SimpleNamespace(join_enabled=False, bindings={aid: binding}))
    monkeypatch.setattr("auto_promotion.phone_social.read_profile",
                        lambda *args: {"identity": "123456"})
    lab.engine.adapters.phone_social = service
    def native(account, operation, payload=None, task_id=None, *, phone=False):
        assert phone and task_id == "task-telegram"
        return PhoneSocial.backend(service, account, operation, payload)
    lab.engine._call_unlocked = native
    checked = lab.engine.check_account(aid, force=True, task_id="task-telegram")
    assert checked["status"] == "healthy"
    assert checked["identity_hash"] == original
    assert checked["connection_state"] == "healthy"
    lab.engine._check_start_accounts({"id": "task-telegram", "account_ids": [aid], "policy": {"execution_method": "phone"}}, refresh=False)
