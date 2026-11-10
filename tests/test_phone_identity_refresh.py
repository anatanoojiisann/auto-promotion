"""Explicit starts renew expired native evidence; health polls never operate the phone."""

import json

import pytest

from auto_promotion.core import Conflict
from tests.test_phone_social import pilot as pilot


def expire(s):
    with s.store.connect(write=True) as db:
        db.execute(
            "UPDATE phone_social_accounts SET observed_at=0 WHERE account_id=?", (s.account["id"],)
        )


def test_expired_start_refreshes_before_account_health_gate(pilot):
    s = pilot
    expire(s)
    before = len([r for r in s.fake.requests if r.method == "POST"])
    s.engine._check_start_accounts(s.engine.task(s.task["id"]))
    assert len([r for r in s.fake.requests if r.method == "POST"]) == before + 1
    assert s.service.fresh(s.engine.account(s.account["id"]))
    assert s.store.one("SELECT COUNT(*) n FROM attempts")["n"] == 0
    assert s.store.one("SELECT COUNT(*) n FROM audit WHERE kind='task.started'")["n"] == 0


def test_health_poll_and_unexpired_start_do_not_grab_device(pilot):
    s = pilot
    before = len(s.fake.requests)
    assert not s.service.ensure_identity(s.account["id"])["refreshed"]
    expire(s)
    assert not s.service.backend(s.account, "health", {})["healthy"]
    assert len(s.fake.requests) == before


def test_unsettled_identity_create_is_only_looked_up_not_reposted(pilot):
    s = pilot
    expire(s)
    with s.store.connect(write=True) as db:
        row = db.execute(
            "SELECT * FROM phone_social_operations WHERE account_id=? ORDER BY created_at DESC LIMIT 1",
            (s.account["id"],),
        ).fetchone()
        snapshot = json.loads(row["result"])
        snapshot["status"] = "unknown"
        s.fake.snapshots[row["execution_id"]] = snapshot
        db.execute(
            "UPDATE phone_social_operations SET result=? WHERE id=?",
            (json.dumps(snapshot), row["id"]),
        )
    before = len([r for r in s.fake.requests if r.method == "POST"])
    with pytest.raises(Conflict, match="ReconciliationRequired"):
        s.service.ensure_identity(s.account["id"])
    assert len([r for r in s.fake.requests if r.method == "POST"]) == before


def test_queued_worker_renews_identity_with_the_items_task_id(pilot):
    s = pilot
    s.service.config = s.service.config.model_copy(
        update={"submit_enabled": True, "authorized_task_ids": [s.task["id"]]}
    )
    s.service.prepare(s.item["id"], s.metadata)
    if s.app == "telegram":
        s.monkeypatch.setattr("auto_promotion.core.telegram_blocker", lambda *a, **k: None)
    s.engine.start_task(s.task["id"], source="synthetic_explicit_start")
    expire(s)
    rows = s.store.rows(
        "SELECT i.id,i.account_id,i.task_id,i.payload,i.due_at,t.policy,t.mode,t.next_at,t.state,t.phase FROM items i JOIN tasks t ON t.id=i.task_id WHERE i.id=?",
        (s.item["id"],),
    )

    class BeforeClaim(Exception):
        pass

    def health(account_id, **kwargs):
        assert account_id == s.account["id"]
        assert s.service.fresh(s.engine.account(account_id))
        raise BeforeClaim

    s.monkeypatch.setattr(s.engine, "check_account", health)
    s.monkeypatch.setattr("auto_promotion.target_checks.TargetChecks.read", lambda *a, **k: None)
    with pytest.raises(BeforeClaim):
        s.engine._run_account_rows(rows, False)
    assert s.store.one("SELECT COUNT(*) n FROM attempts")["n"] == 0
