import copy
import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import test_persistent_pools as fixtures

from auto_promotion.cloud_control import CloudControl, CloudUnavailable
from auto_promotion.continuation_preflight import local_handoff_preflight, replacement_blocker
from auto_promotion.models import ItemInput, canonical
from auto_promotion.queue_store import JobQueue

CAMPAIGN_SCRIPT = (
    Path(__file__).resolve().parents[1] / "outputs" / "ig20-start-20261026" / "continue.py"
)


class ContinuationPreflightTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PersistentPoolTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.engine, self.store = self.fixture.engine, self.fixture.store
        self.now = 1800000000.0
        self.engine.clock = lambda: self.now
        self.task, self.account = self.fixture.ready(channel="local")
        self.item = self.engine.task(self.task["id"])["items"][0]
        self.node = "synthetic-node"
        self.store_id = "synthetic-store"
        self.patch = patch.dict(os.environ, {"AP_NODE_ID": self.node})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO meta VALUES('execution_store_id',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (self.store_id,),
            )
            db.execute(
                "UPDATE items SET status='missed',result=? WHERE id=?",
                (canonical({"reason": "delivery_window_elapsed"}), self.item["id"]),
            )

    def channel(self, source="cloud_acknowledged", state="ready"):
        bound = {
            "account_id": self.account["id"],
            "task_id": self.task["id"],
            "channel": "local",
            "generation": "a" * 32,
            "until_at": self.now + 4000,
            "node_id": self.node,
            "store_id": self.store_id,
            "fence_source": source,
        }
        if source == "legacy":
            bound["legacy_generation"] = "local-generation"
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO account_task_channels VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    bound["account_id"],
                    "local",
                    bound["generation"],
                    bound["until_at"],
                    bound["task_id"],
                    state,
                    source,
                    self.now,
                    None,
                    0,
                ),
            )
            db.execute(
                "INSERT INTO meta VALUES(?,?)",
                ("account_channel_binding:" + self.account["id"], canonical(bound)),
            )
        return bound

    def check(self, *, replacement=True):
        return local_handoff_preflight(
            self.engine,
            self.account["id"],
            self.task["id"],
            required_until=self.now + 3600,
            replacement=replacement,
        )

    def test_pending_handoff_never_contacts_server_or_creates_attempt(self):
        self.channel(state="handoff")
        with patch.object(CloudControl, "configured") as configured:
            self.assertEqual(self.check()["reason"], "local_handoff_pending")
        configured.assert_not_called()
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)
        self.assertIsNone(replacement_blocker(self.engine, self.account["id"], self.item["id"]))

    def test_fresh_cloud_ack_is_verified_once_without_resync(self):
        bound = self.channel()
        cloud = CloudControl("http://127.0.0.1:8791", node_id=self.node, node_token="test")
        with patch.object(cloud, "request", return_value={}):
            cloud.sync(self.engine, task_ids=[self.task["id"]])
        with (
            patch.object(CloudControl, "configured", return_value=cloud),
            patch.object(cloud, "request", return_value={"channel": bound}) as request,
            patch.object(cloud, "sync", side_effect=AssertionError("must not force sync")),
        ):
            self.assertTrue(self.check(replacement=False)["ready"])
            request.assert_called_once_with(
                "/v1/account-channel/status", {"account_id": self.account["id"]}
            )
            request.reset_mock()
            self.now += 301
            self.assertEqual(self.check(replacement=False)["reason"], "task_sync_observation_stale")
            request.assert_not_called()
            check = self.check(replacement=True)
            self.assertTrue(check["ready"])
            self.assertTrue(check["new_task_scope_check_required"])
            request.assert_called_once()

    def test_mismatched_or_unavailable_server_never_establishes_authority(self):
        bound = self.channel()
        cloud = CloudControl("http://127.0.0.1:8791", node_id=self.node, node_token="test")
        with patch.object(cloud, "request", return_value={}):
            cloud.sync(self.engine, task_ids=[self.task["id"]])
        with patch.object(CloudControl, "configured", return_value=cloud):
            with patch.object(
                cloud, "request", return_value={"channel": {**bound, "generation": "b" * 32}}
            ):
                self.assertEqual(self.check()["reason"], "server_channel_mismatch")
            with patch.object(
                cloud, "request", side_effect=CloudUnavailable("CloudTransportUnavailable")
            ):
                self.assertEqual(self.check()["reason"], "server_channel_unavailable")

    def test_independent_scope_checks_account_binding_and_never_contacts_cloud(self):
        self.channel(source="legacy")
        state = {
            "active": "local",
            "authorization": "local_queue",
            "generation": "local-generation",
            "until": self.now + 5000,
            "node_id": self.node,
            "store_id": self.store_id,
            "account_ids": [self.account["id"]],
            "offline_only": True,
        }
        with self.store.connect(write=True) as db:
            for key, value in (
                ("execution_channel_policy", "cloud_then_local"),
                ("execution_channel", canonical(state)),
                ("local_channel_authority", canonical({"account_ids": state["account_ids"]})),
            ):
                db.execute(
                    "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
        with patch.object(
            CloudControl, "configured", side_effect=AssertionError("local scope is offline")
        ):
            self.assertTrue(self.check()["ready"])
            state["through_task_ids"] = [self.task["id"]]
            with self.store.connect(write=True) as db:
                db.execute(
                    "UPDATE meta SET value=? WHERE key='execution_channel'", (canonical(state),)
                )
            self.assertEqual(self.check()["reason"], "local_authority_task_scope_limited")
            self.assertTrue(
                local_handoff_preflight(
                    self.engine,
                    self.account["id"],
                    self.task["id"],
                    required_until=self.now + 3600,
                    replacement=False,
                )["ready"]
            )
            state.pop("through_task_ids")
            state["account_ids"] = ["other-account"]
            with self.store.connect(write=True) as db:
                db.execute(
                    "UPDATE meta SET value=? WHERE key='execution_channel'", (canonical(state),)
                )
            self.assertEqual(self.check()["reason"], "local_authority_unverified")

    def test_concurrent_channel_change_invalidates_server_reply(self):
        bound = self.channel()
        cloud = CloudControl("http://127.0.0.1:8791", node_id=self.node, node_token="test")
        with patch.object(cloud, "request", return_value={}):
            cloud.sync(self.engine, task_ids=[self.task["id"]])

        def changed(*args, **kwargs):
            with self.store.connect(write=True) as db:
                db.execute(
                    "UPDATE account_task_channels SET state='handoff' WHERE account_id=?",
                    (self.account["id"],),
                )
            return {"channel": bound}

        with (
            patch.object(CloudControl, "configured", return_value=cloud),
            patch.object(cloud, "request", side_effect=changed),
        ):
            self.assertEqual(self.check()["reason"], "local_authority_changed")

    def test_replacement_rejects_unknown_results_and_browser_claims(self):
        with self.store.connect(write=True) as db:
            db.execute("UPDATE items SET status='unknown' WHERE id=?", (self.item["id"],))
        self.assertEqual(
            replacement_blocker(self.engine, self.account["id"], self.item["id"]),
            "prior_item_not_proven_unsubmitted",
        )
        with self.store.connect(write=True) as db:
            db.execute("UPDATE items SET status='missed' WHERE id=?", (self.item["id"],))
            db.execute(
                "INSERT INTO meta_browser_claims(item_id,owner,job,context,claim,proof,expires_at) VALUES(?,?,?,?,?,?,?)",
                (self.item["id"], "owner", "job", "{}", "{}", "{}", self.now + 1),
            )
        self.assertEqual(
            replacement_blocker(self.engine, self.account["id"], self.item["id"]),
            "prior_browser_claim_exists",
        )

    def test_replacement_rejects_attempts_even_when_item_pointer_is_missing(self):
        task, account = self.fixture.ready(account=self.account, channel="inherit", target_id=999)
        item = self.engine.task(task["id"])["items"][0]
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE items SET status='missed',result=? WHERE id=?",
                (canonical({"reason": "delivery_window_elapsed"}), item["id"]),
            )
            db.execute(
                "INSERT INTO attempts(id,item_id,account_id,task_id,target_key,user_key,mode,started_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    "attempt",
                    item["id"],
                    account["id"],
                    task["id"],
                    "target",
                    "user",
                    "simulation",
                    self.now,
                ),
            )
        self.assertEqual(
            replacement_blocker(self.engine, account["id"], item["id"]), "prior_attempt_exists"
        )

    def test_stops_and_short_fences_block_preparation(self):
        self.channel(source="standalone")
        with patch.dict(os.environ, {"AP_CLOUD_CONTROL_URL": ""}):
            with self.store.connect(write=True) as db:
                db.execute("UPDATE meta SET value='1' WHERE key='global_stop'")
            self.assertEqual(self.check()["reason"], "global_stop")
            with self.store.connect(write=True) as db:
                db.execute("UPDATE meta SET value='0' WHERE key='global_stop'")
                db.execute(
                    "UPDATE account_task_channels SET until_at=? WHERE account_id=?",
                    (self.now + 10, self.account["id"]),
                )
            self.assertEqual(self.check()["reason"], "local_authority_window_too_short")

    def test_outstanding_account_receipt_blocks_even_a_valid_local_fence(self):
        self.channel(source="standalone")
        with patch.dict(os.environ, {"AP_CLOUD_CONTROL_URL": ""}):
            self.assertTrue(self.check()["ready"])
            with self.store.connect(write=True) as db:
                db.execute(
                    "INSERT INTO cloud_receipt_outbox(job_id,lease,outcome,updated_at) VALUES(?,?,?,?)",
                    ("orphan-job", canonical({"account_id": self.account["id"]}), None, self.now),
                )
            self.assertEqual(self.check()["reason"], "account_receipt_pending")

    def test_replacement_rejects_a_job_channel_claim(self):
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE items SET status='not_attempted',result='{}' WHERE id=?", (self.item["id"],)
            )
        JobQueue(self.engine).sync(self.task["id"])
        job = self.store.one(
            "SELECT id FROM execution_jobs WHERE reference_id=?", (self.item["id"],)
        )
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO task_channel_claims VALUES(?,?,?,?)",
                (job["id"], "owner", "gen", "local"),
            )
            db.execute(
                "UPDATE items SET status='missed',result=? WHERE id=?",
                (canonical({"reason": "delivery_window_elapsed"}), self.item["id"]),
            )
        self.assertEqual(
            replacement_blocker(self.engine, self.account["id"], self.item["id"]),
            "prior_channel_claim_exists",
        )

    @unittest.skipUnless(
        CAMPAIGN_SCRIPT.is_file(), "local campaign continuation is not in the checkout"
    )
    def test_preparation_waits_without_creating_new_task(self):
        path = (
            Path(__file__).resolve().parents[1] / "outputs" / "ig20-start-20261026" / "continue.py"
        )
        spec = importlib.util.spec_from_file_location("ig_continuation", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        data = {
            "items": [
                {
                    "candidate_rank": 15,
                    "current_task_id": self.task["id"],
                    "current_item_id": self.item["id"],
                    "expires_at": self.now + 5000,
                    "payload": self.item["payload"],
                }
            ],
            "service_wait": {"active": True, "next_check_at": self.now + 900},
            "execution_rules": {"source_age_is_discovery_only": True},
        }
        original = copy.deepcopy(data)
        plan = {"action": "ready", "candidate_rank": 15, "next_at": self.now}
        with (
            patch.object(module, "ACCOUNT", self.account["id"]),
            patch.object(
                module,
                "local_handoff_preflight",
                return_value={"ready": False, "reason": "local_handoff_pending"},
            ),
            patch.object(self.engine, "create_task") as create,
        ):
            result = module.prepare(self.engine, data, plan)
        self.assertEqual(result["reason"], "local_handoff_pending")
        self.assertEqual(result["action"], "wait")
        self.assertEqual(data, original)
        create.assert_not_called()

    @unittest.skipUnless(
        CAMPAIGN_SCRIPT.is_file(), "local campaign continuation is not in the checkout"
    )
    def test_proven_unattempted_replacement_keeps_target_text_and_history(self):
        path = (
            Path(__file__).resolve().parents[1] / "outputs" / "ig20-start-20261026" / "continue.py"
        )
        spec = importlib.util.spec_from_file_location("ig_continuation_success", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original_task = self.engine.task(self.task["id"])
        data = {
            "items": [
                {
                    "candidate_rank": 15,
                    "current_task_id": self.task["id"],
                    "current_item_id": self.item["id"],
                    "expires_at": self.now + 5000,
                    "renewals": [],
                    "payload": {
                        k: v for k, v in self.item["payload"].items() if k in ItemInput.model_fields
                    },
                    "template": {
                        "name": "same target",
                        "account_ids": [self.account["id"]],
                        "policy": original_task["policy"],
                    },
                }
            ],
            "authorization": {"start": "explicit test instruction"},
            "service_wait": {"active": True, "next_check_at": self.now + 900},
            "execution_rules": {"source_age_is_discovery_only": True},
        }
        plan = {"action": "ready", "candidate_rank": 15, "next_at": self.now}
        with (
            patch.object(module, "ACCOUNT", self.account["id"]),
            patch.object(module, "save"),
            patch.object(
                module,
                "local_handoff_preflight",
                return_value={
                    "ready": True,
                    "until_at": self.now + 2000,
                    "new_task_scope_check_required": True,
                },
            ),
        ):
            result = module.prepare(self.engine, data, plan)
        self.assertEqual(result["action"], "prepared_needs_explicit_start")
        self.assertTrue(result["new_task_scope_check_required_before_begin"])
        self.assertEqual(result["payload"]["target"], self.item["payload"]["target"])
        self.assertEqual(result["payload"]["text"], self.item["payload"]["text"])
        self.assertEqual(data["items"][0]["renewals"], [self.task["id"]])
        self.assertLessEqual(
            self.engine.task(result["task_id"])["policy"]["ends_at"], module.iso(self.now + 2000)
        )
        self.assertEqual(
            self.store.one("SELECT status FROM items WHERE id=?", (self.item["id"],))["status"],
            "missed",
        )
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    @unittest.skipUnless(
        CAMPAIGN_SCRIPT.is_file(), "local campaign continuation is not in the checkout"
    )
    def test_failed_new_item_creation_keeps_original_continuation_intact(self):
        path = (
            Path(__file__).resolve().parents[1] / "outputs" / "ig20-start-20261026" / "continue.py"
        )
        spec = importlib.util.spec_from_file_location("ig_continuation_failure", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        data = {
            "items": [
                {
                    "candidate_rank": 15,
                    "current_task_id": self.task["id"],
                    "current_item_id": self.item["id"],
                    "expires_at": self.now + 5000,
                    "renewals": [],
                    "payload": {
                        k: v for k, v in self.item["payload"].items() if k in ItemInput.model_fields
                    },
                    "template": {
                        "name": "same target",
                        "account_ids": [self.account["id"]],
                        "policy": self.engine.task(self.task["id"])["policy"],
                    },
                }
            ],
            "authorization": {},
            "service_wait": {"active": True},
            "execution_rules": {"source_age_is_discovery_only": True},
        }
        original = copy.deepcopy(data)
        plan = {"action": "ready", "candidate_rank": 15, "next_at": self.now}
        with (
            patch.object(module, "ACCOUNT", self.account["id"]),
            patch.object(module, "save") as save,
            patch.object(
                module,
                "local_handoff_preflight",
                return_value={"ready": True, "until_at": self.now + 2000},
            ),
            patch.object(self.engine, "set_items", side_effect=RuntimeError("injected failure")),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                module.prepare(self.engine, data, plan)
        self.assertEqual(data, original)
        save.assert_not_called()
        self.assertEqual(
            self.store.one("SELECT status FROM items WHERE id=?", (self.item["id"],))["status"],
            "missed",
        )
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    @unittest.skipUnless(
        CAMPAIGN_SCRIPT.is_file(), "local campaign continuation is not in the checkout"
    )
    def test_report_shows_handoff_wait_without_claiming_a_send(self):
        spec = importlib.util.spec_from_file_location("ig_continuation_report", CAMPAIGN_SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        data = {
            "items": [
                {
                    "candidate_rank": 15,
                    "priority": "P2",
                    "topic": "fixture",
                    "payload": self.item["payload"],
                    "status": "pending",
                    "current_task_id": self.task["id"],
                    "original_task_id": self.task["id"],
                }
            ],
            "service_wait": {"active": True},
            "execution_rules": {"source_age_is_discovery_only": True},
        }
        with patch.object(module, "OUT", Path(self.fixture.temp.name)):
            module.render(data)
            page = (Path(self.fixture.temp.name) / "IG互动任务启动记录.html").read_text(
                encoding="utf-8"
            )
        self.assertIn("账号交接尚未确认，待接续 1 条暂不发送", page)
        self.assertIn("不代表交接已完成", page)


if __name__ == "__main__":
    unittest.main()
