"""Offline tests: no real accounts, devices, network calls, or social submissions."""

import json
from unittest.mock import patch

import test_persistent_pools as pool_fixtures
import test_phone_rounds as round_fixtures

from auto_promotion.cloud_control import CloudControl
from auto_promotion.execution_channels import binding
from auto_promotion.local_primary import enable, uncertain_duplicate, valid
from auto_promotion.models import AccountInput, canonical
from auto_promotion.phone_rounds import round_task_authorized
from auto_promotion.pool_runtime import run_pool
from auto_promotion.queue_store import JobQueue
from auto_promotion.task_channels import TaskChannelRouter, guard_context


def grant(engine, account_ids):
    store_id = binding(engine)
    with engine.store.connect(write=True) as db:
        mode = {"enabled": True, "node_id": "test-node", "store_id": store_id, "generation": "mode"}
        db.execute("INSERT INTO meta VALUES('execution_local_primary',?)", (canonical(mode),))
        for account_id in account_ids:
            account = dict(db.execute("SELECT id,platform,mode,backend_label,identity_hash FROM accounts WHERE id=?", (account_id,)).fetchone())
            account.update(node_id="test-node", store_id=store_id, generation="grant-" + account_id,
                           mode_generation="mode", prior_generation=None)
            db.execute("INSERT INTO meta VALUES(?,?)", ("local_primary_account:" + account_id, canonical(account)))
        db.execute("INSERT INTO meta VALUES('cloud_control_required','1') ON CONFLICT(key) DO UPDATE SET value='1'")
        db.execute("INSERT INTO meta VALUES('execution_runtime','pooled') ON CONFLICT(key) DO UPDATE SET value='pooled'")


def make_pool():
    lab = pool_fixtures.PersistentPoolTests()
    lab.setUp()
    return lab


def test_local_executes_without_cloud_despite_expired_legacy_scope():
    lab = make_pool()
    try:
        task, account = lab.ready(channel="local")
        with patch.dict("os.environ", {"AP_NODE_ID": "test-node"}):
            grant(lab.engine, [account["id"]])
            with lab.store.connect(write=True) as db:
                db.execute("INSERT INTO meta VALUES('execution_channel',?)", (canonical({"active":"local", "authorization":"local_queue", "until":1, "offline_only":True, "account_ids":["other"]}),))
            with patch.object(CloudControl, "configured", side_effect=AssertionError("cloud called")):
                lab.queue = JobQueue(lab.engine)
                lab.queue.sync()
                run_pool(lab.engine, "simulation.x", "test-node", max_jobs=1)
            assert lab.engine.task(task["id"])["summary"] == {"confirmed": 1}
    finally:
        lab.doCleanups()


def test_account_grant_is_independent_and_cloud_task_stays_held():
    lab = make_pool()
    try:
        blocked, first = lab.ready(channel="local")
        second = lab.engine.add_account(AccountInput(platform="x", label="Second", backend_label="second", mode="simulation", min_interval=0))
        allowed, _ = lab.ready(account=second, channel="local", target_id=567)
        online, _ = lab.ready(account=second, channel="cloud", target_id=568)
        with patch.dict("os.environ", {"AP_NODE_ID": "test-node"}):
            grant(lab.engine, [second["id"]])
            queue = JobQueue(lab.engine)
            queue.sync()
            with patch.object(CloudControl, "configured", side_effect=AssertionError("cloud called")):
                job = queue.claim("simulation.x", "owner")
                assert job["task_id"] == allowed["id"]
                context, cloud = TaskChannelRouter(lab.engine).prepare(job)
                assert cloud is None
                guard_context(lab.engine, context)
                with lab.store.connect(write=True) as db:
                    db.execute("DELETE FROM meta WHERE key=?", ("local_primary_account:" + second["id"],))
                try:
                    guard_context(lab.engine, context)
                except Exception:
                    pass
                else:
                    raise AssertionError("revoked authority was accepted")
            assert lab.engine.task(blocked["id"])["summary"] == {"not_attempted": 1}
            assert lab.engine.task(online["id"])["summary"] == {"not_attempted": 1}
    finally:
        lab.doCleanups()


def test_new_round_authorizes_only_original_started_item_and_stops_on_change(tmp_path):
    lab = round_fixtures.make_lab(tmp_path)
    account = lab.store.one("SELECT id FROM accounts WHERE platform='telegram'")["id"]
    task_id = "task-telegram"
    with patch.dict("os.environ", {"AP_NODE_ID": "test-node"}):
        grant(lab.engine, [account])
        round_ = lab.rounds.create(lab.ids, "round-authority")
        with lab.store.connect() as db:
            assert not round_task_authorized(db, task_id, account, lab.now[0])
        lab.rounds._begin_check(round_["id"], "telegram", lab.now[0])
        with lab.store.connect() as db:
            assert round_task_authorized(db, task_id, account, lab.now[0])
            assert not round_task_authorized(db, task_id, "different-account", lab.now[0])
        with lab.store.connect(write=True) as db:
            db.execute("UPDATE phone_round_members SET payload_hash='changed' WHERE task_id=?", (task_id,))
        with lab.store.connect() as db:
            assert not round_task_authorized(db, task_id, account, lab.now[0])


def test_unknown_same_message_cannot_be_recreated_but_different_target_can(tmp_path):
    lab = round_fixtures.make_lab(tmp_path)
    account = lab.store.one("SELECT * FROM accounts WHERE platform='telegram'")
    payload = {"action":"send_message", "target":"https://t.me/example_group", "text":"same text"}
    with lab.store.connect(write=True) as db:
        db.execute("UPDATE items SET status='unknown',payload=? WHERE id='item-telegram'", (canonical(payload),))
        duplicate = uncertain_duplicate(db, account, {**payload, "target":"@example_group"}, "another-item")
        other = uncertain_duplicate(db, account, {**payload, "target":"@other_group"}, "another-item")
    assert duplicate["blocked_by_item_id"] == "item-telegram"
    assert other is None


def test_expired_prior_task_control_is_superseded_without_contacting_cloud():
    lab = make_pool()
    try:
        task, account = lab.ready(channel="local")
        now = lab.engine.clock()
        with patch.dict("os.environ", {"AP_NODE_ID": "test-node"}):
            grant(lab.engine, [account["id"]])
            old = {"account_id": account["id"], "generation": "old", "task_id": "prior-task",
                   "channel": "local", "until_at": now - 10, "coordination_task_id": "prior-task"}
            with lab.store.connect(write=True) as db:
                entry = json.loads(db.execute("SELECT value FROM meta WHERE key=?", ("local_primary_account:" + account["id"],)).fetchone()[0])
                entry["prior_generation"] = "old"
                db.execute("UPDATE meta SET value=? WHERE key=?", (canonical(entry), "local_primary_account:" + account["id"]))
                db.execute("INSERT INTO meta VALUES(?,?)", ("account_channel_binding:" + account["id"], canonical(old)))
                db.execute("INSERT INTO account_task_channels VALUES(?,?,?,?,?,'handoff','pending',?,?,?)", (account["id"], "local", "old", now - 10, "prior-task", now - 20, "OldError", now + 500))
            with patch.object(CloudControl, "configured", side_effect=AssertionError("cloud called")):
                channel = lab.engine.task(task["id"])["channel"]
                assert channel["accounts"][0]["blocking_reason"] is None
                queue = JobQueue(lab.engine)
                queue.sync()
                job = queue.claim("simulation.x", "owner")
                assert job is not None
                context, cloud = TaskChannelRouter(lab.engine).prepare(job)
                assert cloud is None and context["generation"] != "old"
                guard_context(lab.engine, context)
            assert lab.engine.task(task["id"])["id"] == task["id"]
    finally:
        lab.doCleanups()


def test_cutover_grants_only_accounts_with_verified_expired_owner(tmp_path):
    lab = round_fixtures.make_lab(tmp_path)
    telegram = lab.store.one("SELECT id FROM accounts WHERE platform='telegram'")["id"]
    instagram = lab.store.one("SELECT id FROM accounts WHERE platform='instagram'")["id"]
    binding(lab.engine)
    with lab.store.connect(write=True) as db:
        db.execute("UPDATE accounts SET identity_hash='synthetic-' || id")
        db.execute("INSERT INTO account_task_channels VALUES(?,?,?,?,?,'ready','cloud_acknowledged',?,NULL,0)", (instagram, "cloud", "active", lab.now[0] + 10, "old", lab.now[0]))
    evidence = {telegram: {"exclusive_local": True, "observed_at": lab.now[0]},
                instagram: {"exclusive_local": True, "observed_at": lab.now[0]}}
    with patch.dict("os.environ", {"AP_NODE_ID": "test-node"}):
        result = enable(lab.engine, evidence)
        assert result["accepted"] == [telegram]
        assert result["rejected"][instagram] == "PriorOwnershipStillActive"
        with lab.store.connect() as db:
            assert valid(db, telegram)
            assert not valid(db, instagram)
        with lab.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET identity_hash='different' WHERE id=?", (telegram,))
        with lab.store.connect() as db:
            assert not valid(db, telegram)
