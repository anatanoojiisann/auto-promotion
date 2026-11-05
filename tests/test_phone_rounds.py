"""Synthetic six-app rounds; no hardware, social credentials or external effects."""

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.api import create_app
from auto_promotion.core import Conflict, Engine
from auto_promotion.models import AccountInput, canonical, digest
from auto_promotion.phone_rounds import PLATFORMS, PhoneRounds, submission_blocker
from auto_promotion.store import Store


def make_lab(tmp_path):
    now = [10000.0]
    engine = Engine(
        Store(tmp_path / "synthetic.db"),
        AdapterRegistry(tmp_path),
        clock=lambda: now[0],
        live_enabled=True,
    )
    ids = []
    for platform in sorted(PLATFORMS):
        account = engine.add_account(
            AccountInput(
                platform=platform,
                label="Synthetic " + platform,
                backend_label="synthetic-" + platform,
                mode="live",
            )
        )
        task_id, item_id = "task-" + platform, "item-" + platform
        policy = {
            "execution_method": "phone",
            "execution_channel": "local",
            "max_joins": 0,
            "starts_at": "1970-01-01T02:46:40+00:00",
            "ends_at": "1970-01-01T03:46:40+00:00",
        }
        snapshot = [{"id": account["id"], "platform": platform, "mode": "live"}]
        payload = {
            "media": [],
            "action": {"telegram": "send_message", "x": "reply", "reddit": "reply"}.get(
                platform, "comment"
            ),
            "target": "https://example.invalid/" + platform,
            "text": "Synthetic reply",
        }
        manifest = {"task_id": task_id, "accounts": snapshot, "policy": policy, "items": [payload]}
        with engine.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO tasks(id,name,mode,account_ids,account_snapshot,policy,policy_hash,state,phase,manifest,manifest_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'draft','review',?,?,?,?)",
                (
                    task_id,
                    "Synthetic round",
                    "live",
                    canonical([account["id"]]),
                    canonical(snapshot),
                    canonical(policy),
                    digest({"accounts": snapshot, "policy": policy}),
                    canonical(manifest),
                    digest(manifest),
                    now[0],
                    now[0],
                ),
            )
            db.execute(
                "INSERT INTO items(id,task_id,account_id,position,payload,payload_hash,due_at) VALUES(?,?,?,0,?,?,?)",
                (item_id, task_id, account["id"], canonical(payload), digest(payload), now[0]),
            )
        ids.append(task_id)
    with engine.store.connect(write=True) as db:
        db.execute("INSERT INTO meta VALUES('execution_runtime','pooled')")
        db.execute("UPDATE meta SET value='0' WHERE key='global_stop'")
    calls = []

    def synthetic_start(task_id, **kwargs):
        with engine.store.connect(write=True) as db:
            assert submission_blocker(db, task_id, now[0]) is None
            db.execute("UPDATE tasks SET state='active',phase='ready' WHERE id=?", (task_id,))
        calls.append(task_id)

    engine.start_and_run = synthetic_start

    def synthetic_readiness(rounds, round_id):
        return {
            "round_id": round_id,
            "ready_count": len(rounds.get(round_id)["members"]),
            "submission_authorized": False,
            "members": [
                {"platform": m["platform"], "ready": True, "blockers": []}
                for m in rounds.get(round_id)["members"]
            ],
        }

    rounds = PhoneRounds(engine, preflight_check=synthetic_readiness)
    return SimpleNamespace(
        engine=engine, rounds=rounds, now=now, ids=ids, calls=calls, store=engine.store
    )


@pytest.fixture
def lab(tmp_path):
    return make_lab(tmp_path)


def create(lab, key="synthetic-round-1"):
    return lab.rounds.create(lab.ids, key)


def blocker(lab, task_id=None):
    with lab.store.connect() as db:
        return submission_blocker(db, task_id or lab.ids[0], lab.now[0])


def finish(lab, platform, *, outcome="confirmed", verified=True, at=None):
    at = lab.now[0] if at is None else at
    task_id, item_id, attempt_id = "task-" + platform, "item-" + platform, "attempt-" + platform
    with lab.store.connect(write=True) as db:
        # Load explicit synthetic result history, without presenting this as an executor claim.
        # Real guarded-claim/one-use-grant behavior is tested by test_phone_social/reddit.
        policy = db.execute("SELECT policy FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
        db.execute("UPDATE meta SET value='legacy' WHERE key='execution_runtime'")
        db.execute(
            "UPDATE tasks SET policy=json_set(policy,'$.execution_channel','inherit') WHERE id=?",
            (task_id,),
        )
        item = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        db.execute(
            "INSERT INTO attempts(id,item_id,account_id,task_id,target_key,user_key,mode,started_at,finished_at,outcome) VALUES(?,?,?,?,?,?,'live',?,?,?)",
            (
                attempt_id,
                item_id,
                item["account_id"],
                task_id,
                platform,
                platform,
                lab.now[0],
                at,
                outcome,
            ),
        )
        db.execute(
            "UPDATE items SET status=?,attempt_id=?,external_reference=?,result=? WHERE id=?",
            (
                outcome,
                attempt_id,
                "https://example.invalid/receipt/" + platform,
                canonical({"verified": verified}),
                item_id,
            ),
        )
        db.execute("UPDATE tasks SET policy=? WHERE id=?", (policy, task_id))
        db.execute("UPDATE meta SET value='pooled' WHERE key='execution_runtime'")


def test_create_idempotent_without_starting_or_changing_stop(lab):
    round_ = create(lab)
    assert round_["state"] == "draft" and not lab.calls
    assert lab.rounds.create(list(reversed(lab.ids)), "synthetic-round-1")["id"] == round_["id"]
    assert lab.store.one("SELECT COUNT(*) n FROM attempts")["n"] == 0
    assert blocker(lab) == "PhoneRoundNotRunning"
    assert lab.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"] == "0"


def test_cannot_reuse_original_tasks_in_another_round(lab):
    create(lab)
    with pytest.raises(Conflict, match="AlreadyAssigned"):
        create(lab, "synthetic-round-2")


@pytest.mark.parametrize(
    "change,reason",
    [
        ("UPDATE tasks SET state='active'", "PreparedDraft"),
        ("UPDATE items SET status='unknown'", "Unattempted"),
        ("UPDATE tasks SET manifest_hash=NULL", "PreparedDraft"),
        ("UPDATE accounts SET mode='simulation'", "账号配置"),
    ],
)
def test_rejects_ineligible_originals(lab, change, reason):
    with lab.store.connect(write=True) as db:
        db.execute(change)
    with pytest.raises(Conflict, match=reason):
        create(lab)


def test_repeated_start_keeps_first_deadline(lab):
    identifier = create(lab)["id"]
    first = lab.rounds.start(identifier)
    lab.now[0] += 100
    repeated = lab.rounds.start(identifier)
    assert first["deadline"] == repeated["deadline"] == 10600
    assert repeated["elapsed_seconds"] == 100
    assert not lab.calls


@pytest.mark.parametrize("setting", ["legacy", "missing", "live_disabled"])
def test_start_never_enables_runtime_or_live_writes(lab, setting):
    identifier = create(lab)["id"]
    if setting == "live_disabled":
        lab.engine.live_enabled = False
    else:
        with lab.store.connect(write=True) as db:
            db.execute("DELETE FROM meta WHERE key='execution_runtime'")
            if setting == "legacy":
                db.execute("INSERT INTO meta VALUES('execution_runtime','legacy')")
    with pytest.raises(Conflict):
        lab.rounds.start(identifier)
    assert lab.rounds.get(identifier)["state"] == "draft"


def test_parallel_workers_admit_each_original_once(lab):
    identifier = create(lab)["id"]
    lab.rounds.start(identifier)
    with ThreadPoolExecutor(max_workers=6) as pool:
        for _ in range(6):
            list(pool.map(lambda _: lab.rounds.tick(), range(6)))
    assert sorted(lab.calls) == sorted(lab.ids)
    assert all(m["stage"] == "queued" for m in lab.rounds.get(identifier)["members"])


def test_global_stop_wait_consumes_round_budget_without_clearing_stop(lab):
    identifier = create(lab)["id"]
    lab.rounds.start(identifier)
    with lab.store.connect(write=True) as db:
        db.execute("UPDATE meta SET value='1' WHERE key='global_stop'")
    lab.rounds.tick()
    lab.now[0] += 600
    lab.rounds.tick()
    assert not lab.calls
    assert lab.rounds.get(identifier)["state"] == "expired"
    assert blocker(lab) == "PhoneRoundNotRunning"


def test_cutoff_enforced_without_worker_or_status_poll(lab):
    identifier = create(lab)["id"]
    lab.rounds.start(identifier)
    lab.rounds.tick()
    assert blocker(lab) is None
    lab.now[0] += 480
    assert blocker(lab) == "PhoneRoundSubmissionDeadlineElapsed"
    assert lab.engine._claim("item-facebook") is None
    assert lab.store.one("SELECT COUNT(*) n FROM attempts")["n"] == 0
    lab.rounds.tick()
    assert len(lab.calls) == 1


def test_stop_fences_new_submission_but_preserves_unknown(lab):
    identifier = create(lab)["id"]
    lab.rounds.start(identifier)
    lab.rounds.tick()
    finish(lab, "facebook", outcome="unknown", verified=False)
    lab.rounds.stop(identifier)
    lab.rounds.start(identifier)
    lab.rounds.tick()
    row = lab.rounds.get(identifier)
    assert row["state"] == "stopped" and row["members"][0]["stage"] == "unknown"
    assert len(lab.calls) == 1 and blocker(lab) == "PhoneRoundNotRunning"
    assert lab.store.one("SELECT status FROM items WHERE id='item-facebook'")["status"] == "unknown"


def test_start_failure_is_not_retried_and_other_apps_continue(lab):
    identifier = create(lab)["id"]
    lab.rounds.start(identifier)
    original = lab.engine.start_and_run

    def fail(task_id, **kwargs):
        if task_id == lab.ids[0]:
            raise RuntimeError("synthetic private response must not appear in log")
        return original(task_id, **kwargs)

    lab.engine.start_and_run = fail
    for _ in range(12):
        lab.rounds.tick()
    row = lab.rounds.get(identifier)
    assert row["members"][0]["stage"] == "blocked"
    assert row["members"][0]["reason"] == "PhoneRoundStartFailed"
    assert len(lab.calls) == 5 and "private response" not in json.dumps(row)


def test_restart_does_not_repeat_inflight_start(lab):
    identifier = create(lab)["id"]
    lab.rounds.start(identifier)
    with lab.store.connect(write=True) as db:
        db.execute(
            "UPDATE phone_round_members SET state='starting',start_requested_at=?,start_lease_until=? WHERE task_id=?",
            (10000, 10120, lab.ids[0]),
        )
    lab.rounds = PhoneRounds(lab.engine)
    lab.rounds.tick()
    assert lab.ids[0] not in lab.calls
    lab.now[0] += 121
    lab.rounds.tick()
    member = lab.rounds.get(identifier)["members"][0]
    assert member["stage"] == "blocked" and member["reason"] == "PhoneRoundStartInterrupted"
    assert lab.ids[0] not in lab.calls


def test_changed_frozen_manifest_fences_grant(lab):
    identifier = create(lab)["id"]
    lab.rounds.start(identifier)
    lab.rounds.tick()
    with lab.store.connect(write=True) as db:
        db.execute("UPDATE items SET payload_hash='changed' WHERE id='item-facebook'")
    assert blocker(lab) == "PhoneRoundManifestChanged"


def test_only_six_native_confirmations_complete_round(lab):
    identifier = create(lab)["id"]
    lab.rounds.start(identifier)
    for _ in lab.ids:
        lab.rounds.tick()
    lab.now[0] += 540
    for platform in PLATFORMS:
        finish(lab, platform)
    lab.rounds.tick()
    row = lab.rounds.get(identifier)
    assert row["state"] == "completed"
    assert row["confirmed_count"] == row["within_budget_count"] == 6
    assert row["all_six_within_budget"] and row["elapsed_seconds"] == 540


def test_unverified_or_late_receipt_never_satisfies_budget(lab):
    identifier = create(lab)["id"]
    lab.rounds.start(identifier)
    for _ in lab.ids:
        lab.rounds.tick()
    for platform in PLATFORMS - {"x"}:
        finish(lab, platform, at=10550)
    finish(lab, "x", verified=False, at=10550)
    lab.now[0] += 600
    lab.rounds.tick()
    cutoff = lab.rounds.get(identifier)["cutoff_snapshot"]
    assert not lab.rounds.get(identifier)["all_six_within_budget"]
    with lab.store.connect(write=True) as db:
        db.execute("UPDATE items SET result=? WHERE id='item-x'", (canonical({"verified": True}),))
        db.execute("UPDATE attempts SET finished_at=10601 WHERE id='attempt-x'")
    row = lab.rounds.get(identifier)
    assert row["state"] == "expired" and row["confirmed_count"] == 6
    assert row["within_budget_count"] == 5 and row["cutoff_snapshot"] == cutoff


def test_old_non_round_tasks_are_unaffected(tmp_path):
    store = Store(tmp_path / "old.db")
    with store.connect() as db:
        assert submission_blocker(db, "existing-original", 10000) is None


@pytest.mark.asyncio
async def test_api_opt_in_and_existing_auth_boundary(tmp_path):
    for enabled in [False, True]:
        app = create_app(
            db_path=tmp_path / (str(enabled) + ".db"),
            registry=AdapterRegistry(tmp_path),
            token="synthetic-key",
            phone_rounds_enabled=enabled,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            path = "/api/phone-rounds/missing"
            assert (await client.get(path)).status_code == 401
            response = await client.get(path, headers={"Authorization": "Bearer synthetic-key"})
            assert response.status_code == 404
            if enabled:
                assert response.json()["error"] == "PhoneRoundNotFound"
            else:
                assert not app.state.store.one(
                    "SELECT 1 FROM sqlite_master WHERE name='phone_rounds'"
                )


@pytest.mark.parametrize("lose_first_sync", [False, True])
def test_recovery_repairs_original_queue_without_restarting_task(lab, monkeypatch, lose_first_sync):
    from auto_promotion.queue_store import JobQueue

    identifier = create(lab)["id"]
    lab.rounds.start(identifier)
    task_id = lab.ids[0]
    with lab.store.connect(write=True) as db:
        db.execute("UPDATE tasks SET state='active',phase='ready' WHERE id=?", (task_id,))
        db.execute(
            "UPDATE phone_round_members SET state='starting',start_requested_at=10000,start_lease_until=10001 WHERE task_id=?",
            (task_id,),
        )
    lab.now[0] += 2
    original_sync, calls = JobQueue.sync, []

    def sync(queue, selected=None):
        calls.append(selected)
        if lose_first_sync and len(calls) == 1:
            raise RuntimeError("synthetic metadata sync failure")
        return original_sync(queue, selected)

    monkeypatch.setattr(JobQueue, "sync", sync)
    lab.rounds = PhoneRounds(lab.engine)
    lab.rounds.tick()
    if lose_first_sync:
        assert lab.rounds.get(identifier)["members"][0]["stage"] == "starting"
    lab.rounds.tick()
    assert task_id not in lab.calls
    assert lab.rounds.get(identifier)["members"][0]["stage"] == "queued"
    jobs = lab.store.rows("SELECT * FROM execution_jobs WHERE task_id=?", (task_id,))
    assert len(jobs) == 1 and jobs[0]["reference_id"] == "item-facebook"
