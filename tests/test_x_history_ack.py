import asyncio
import json
import unittest
from unittest.mock import patch

import test_persistent_pools as fixtures
from test_api import request

from auto_promotion.api import create_app
from auto_promotion.cloud_control import CloudControl, CloudUnavailable
from auto_promotion.continuation_preflight import local_handoff_preflight
from auto_promotion.local_authorization import authorize_local_channel, validate_scope
from auto_promotion.models import canonical, digest
from auto_promotion.queue_store import JobQueue, job_scope
from auto_promotion.task_channels import valid_context
from auto_promotion.x_history_ack import allowed_unknown_ids, record_ack


class XHistoryAcknowledgementTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PersistentPoolTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.engine, self.store = self.fixture.engine, self.fixture.store
        self.old, self.account = self.fixture.ready(target_id=111)
        self.old_item = self.store.one("SELECT * FROM items WHERE task_id=?", (self.old["id"],))
        with self.store.connect(write=True) as db:
            db.execute("UPDATE tasks SET state='completed' WHERE id=?", (self.old["id"],))
            db.execute(
                "UPDATE items SET status='unknown',attempt_id='old-attempt',claimed_at=? WHERE id=?",
                (self.engine.clock() - 10, self.old_item["id"]),
            )
            db.execute(
                "INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "old-attempt",
                    self.old_item["id"],
                    self.account["id"],
                    self.old["id"],
                    "target",
                    "user",
                    "live",
                    self.engine.clock() - 10,
                    self.engine.clock() - 9,
                    "unknown",
                ),
            )
        self.new, _ = self.fixture.ready(account=self.account, target_id=222, channel="local")
        self.item = self.store.one("SELECT * FROM items WHERE task_id=?", (self.new["id"],))
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE accounts SET mode='live',identity_hash=? WHERE id=?",
                ("a" * 64, self.account["id"]),
            )
            db.execute(
                "UPDATE tasks SET mode='live' WHERE id IN (?,?)", (self.old["id"], self.new["id"])
            )
        self.engine.live_enabled = True
        self.route = patch(
            "auto_promotion.local_authorization.independent_account_supported", return_value=True
        )
        self.route.start()
        self.addCleanup(self.route.stop)

    def scope(self):
        with self.store.connect() as db:
            return validate_scope(
                self.engine, db, self.new["id"], self.account["id"], self.engine.clock()
            )

    def ack(self):
        return record_ack(
            self.engine,
            self.new["id"],
            self.account["id"],
            source="explicit user: new targets only",
        )

    def allowed(self):
        with self.store.connect() as db:
            return allowed_unknown_ids(db, self.new["id"], self.account["id"], self.engine.clock())

    def test_explicit_ack_unblocks_only_new_target_and_preserves_old_unknown(self):
        with self.assertRaisesRegex(CloudUnavailable, "account_result_unresolved"):
            self.scope()
        self.assertEqual(self.ack()["history_count"], 1)
        self.scope()
        self.assertEqual(self.allowed(), {self.old_item["id"]})
        self.assertEqual(
            self.store.one("SELECT status FROM items WHERE id=?", (self.old_item["id"],))["status"],
            "unknown",
        )
        self.assertEqual(
            self.store.one("SELECT outcome FROM attempts WHERE id='old-attempt'")["outcome"],
            "unknown",
        )
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 1)

    def test_same_target_is_never_acknowledged(self):
        with self.store.connect(write=True) as db:
            old = json.loads(self.old_item["payload"])
            old["target"] = json.loads(self.item["payload"])["target"]
            db.execute(
                "UPDATE items SET payload=?,payload_hash=? WHERE id=?",
                (canonical(old), digest(old), self.old_item["id"]),
            )
        with self.assertRaisesRegex(ValueError, "TargetUnsafe|HistoryUnsafe"):
            self.ack()

    def test_changed_target_or_resumed_old_task_invalidates_ack(self):
        self.ack()
        with self.store.connect(write=True) as db:
            payload = json.loads(self.item["payload"])
            payload["target"] = "https://x.com/example/status/333"
            db.execute(
                "UPDATE items SET payload=?,payload_hash=? WHERE id=?",
                (canonical(payload), digest(payload), self.item["id"]),
            )
        self.assertIsNone(self.allowed())
        with self.assertRaisesRegex(CloudUnavailable, "XHistoryAcknowledgementChanged"):
            self.scope()
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE items SET payload=?,payload_hash=? WHERE id=?",
                (self.item["payload"], self.item["payload_hash"], self.item["id"]),
            )
            db.execute("UPDATE tasks SET state='active' WHERE id=?", (self.old["id"],))
        self.assertIsNone(self.allowed())

    def test_new_unacknowledged_unknown_invalidates_ack(self):
        self.ack()
        with self.store.connect(write=True) as db:
            db.execute("UPDATE items SET status='unknown' WHERE id=?", (self.item["id"],))
        self.assertIsNone(self.allowed())

    def test_existing_ack_survives_an_expired_item_without_an_attempt(self):
        self.ack()
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE items SET status='missed',result=? WHERE id=?",
                (canonical({"reason": "delivery_window_elapsed"}), self.item["id"]),
            )
        self.assertEqual(self.allowed(), {self.old_item["id"]})
        item = self.store.one("SELECT status,attempt_id FROM items WHERE id=?", (self.item["id"],))
        self.assertEqual(item["status"], "missed")
        self.assertIsNone(item["attempt_id"])

    def test_expired_item_with_an_attempt_still_invalidates_ack(self):
        self.ack()
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE items SET status='missed',attempt_id='unexpected',result=? WHERE id=?",
                (canonical({"reason": "delivery_window_elapsed"}), self.item["id"]),
            )
        self.assertIsNone(self.allowed())

    def test_new_ack_cannot_be_created_after_an_item_is_expired(self):
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE items SET status='missed',result=? WHERE id=?",
                (canonical({"reason": "delivery_window_elapsed"}), self.item["id"]),
            )
        with self.assertRaisesRegex(ValueError, "TargetUnsafe"):
            self.ack()

    def test_existing_ack_survives_proven_not_submitted_failure(self):
        self.ack()
        JobQueue(self.engine).sync(self.new["id"])
        result = {
            "status": "failed_known",
            "verified": False,
            "confirmed_not_submitted": True,
            "submission_started": False,
        }
        with job_scope({"owner": "test-owner"}), self.store.connect(write=True) as db:
            job_id = "send:" + self.item["id"]
            db.execute(
                "UPDATE execution_jobs SET state='leased',owner='test-owner',lease_until=? WHERE id=?",
                (self.engine.clock() + 600, job_id),
            )
            db.execute(
                "INSERT INTO account_task_channels VALUES(?,?,?,?,?,'ready','cloud_acknowledged',?,NULL,0)",
                (
                    self.account["id"],
                    "local",
                    "test-generation",
                    self.engine.clock() + 600,
                    self.new["id"],
                    self.engine.clock(),
                ),
            )
            db.execute(
                "INSERT INTO task_channel_claims VALUES(?,?,?,?)",
                (job_id, "test-owner", "test-generation", "local"),
            )
            db.execute(
                "UPDATE items SET status='failed_known',attempt_id='new-attempt',result=? WHERE id=?",
                (canonical(result), self.item["id"]),
            )
            db.execute(
                "INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "new-attempt",
                    self.item["id"],
                    self.account["id"],
                    self.new["id"],
                    "target",
                    "user",
                    "live",
                    self.engine.clock() - 2,
                    self.engine.clock() - 1,
                    "failed_known",
                ),
            )
            db.execute(
                "UPDATE execution_jobs SET state='failed_known',owner=NULL,lease_until=0 WHERE id=?",
                (job_id,),
            )
        self.assertEqual(self.allowed(), {self.old_item["id"]})
        self.assertEqual(
            self.store.one("SELECT status FROM items WHERE id=?", (self.item["id"],))["status"],
            "failed_known",
        )

    def test_failed_item_without_no_submit_proof_still_invalidates_ack(self):
        self.ack()
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE items SET status='failed_known',result=? WHERE id=?",
                (canonical({"status": "failed_known"}), self.item["id"]),
            )
        self.assertIsNone(self.allowed())

    def test_new_historical_unknown_after_ack_invalidates_it(self):
        self.ack()
        payload = json.loads(self.old_item["payload"])
        payload["target"] = "https://x.com/example/status/333"
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO items(id,task_id,account_id,position,payload,payload_hash,due_at,status) "
                "VALUES(?,?,?,?,?,?,?,'unknown')",
                (
                    "later-unknown",
                    self.old["id"],
                    self.account["id"],
                    1,
                    canonical(payload),
                    digest(payload),
                    self.engine.clock(),
                ),
            )
        self.assertIsNone(self.allowed())
        with self.assertRaisesRegex(CloudUnavailable, "XHistoryAcknowledgementChanged"):
            self.scope()

    def test_final_job_guard_rechecks_the_acknowledged_scope(self):
        self.ack()
        queue = JobQueue(self.engine)
        queue.sync(self.new["id"])
        now = self.engine.clock()
        job_id = "send:" + self.item["id"]
        until = now + 3600
        binding = {
            "account_id": self.account["id"],
            "task_id": self.new["id"],
            "node_id": "node",
            "store_id": "store",
            "generation": "generation",
            "channel": "local",
            "until_at": until,
            "fence_source": "cloud_acknowledged",
            "x_history_ack_required": True,
        }
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE execution_jobs SET state='leased',owner='owner',lease_until=? WHERE id=?",
                (now + 60, job_id),
            )
            db.execute(
                "INSERT INTO account_task_channels VALUES(?,?,?,?,?,'ready','cloud_acknowledged',?,NULL,0)",
                (self.account["id"], "local", "generation", until, self.new["id"], now),
            )
            db.execute(
                "INSERT INTO task_channel_claims VALUES(?,?,?,?)",
                (job_id, "owner", "generation", "local"),
            )
            db.execute("INSERT OR REPLACE INTO meta VALUES('execution_store_id','store')")
            db.execute(
                "INSERT OR REPLACE INTO meta VALUES(?,?)",
                ("account_channel_binding:" + self.account["id"], canonical(binding)),
            )
        context = {
            "id": job_id,
            "owner": "owner",
            "generation": "generation",
            "channel": "local",
            "task_channel": True,
            "store_id": "store",
            "node_id": "node",
            "account_id": self.account["id"],
            "task_id": self.new["id"],
        }
        with self.store.connect() as db:
            self.assertTrue(valid_context(db, context, now=now))
        with self.store.connect(write=True) as db:
            db.execute("UPDATE tasks SET state='active' WHERE id=?", (self.old["id"],))
        with self.store.connect() as db:
            self.assertFalse(valid_context(db, context, now=now))
        with self.store.connect(write=True) as db:
            db.execute("UPDATE tasks SET state='completed' WHERE id=?", (self.old["id"],))
            db.execute(
                "DELETE FROM meta WHERE key=?",
                ("x_history_ack:" + self.new["id"] + ":" + self.account["id"],),
            )
        with self.store.connect() as db:
            self.assertFalse(valid_context(db, context, now=now))

    def test_stop_or_account_block_remains_binding(self):
        self.ack()
        with self.store.connect(write=True) as db:
            db.execute("UPDATE meta SET value='1' WHERE key='global_stop'")
        with self.assertRaisesRegex(CloudUnavailable, "global_stop"):
            self.scope()

    def test_ack_does_not_bypass_account_disable_or_pending_receipt(self):
        self.ack()
        with self.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET disabled=1 WHERE id=?", (self.account["id"],))
        with self.assertRaisesRegex(CloudUnavailable, "AccountUnavailable"):
            self.scope()
        with self.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET disabled=0 WHERE id=?", (self.account["id"],))
            db.execute(
                "INSERT INTO cloud_receipt_outbox(job_id,lease,updated_at) VALUES(?,?,?)",
                (
                    "synthetic-old-receipt",
                    canonical({"account_id": self.account["id"]}),
                    self.engine.clock(),
                ),
            )
        with self.assertRaisesRegex(CloudUnavailable, "account_receipt_pending"):
            self.scope()

    def test_ack_does_not_ignore_scheduled_or_inflight_history(self):
        self.ack()
        for status in ("scheduled", "executing"):
            with self.subTest(status=status), self.store.connect(write=True) as db:
                db.execute("UPDATE items SET status=? WHERE id=?", (status, self.old_item["id"]))
            with self.assertRaisesRegex(CloudUnavailable, "account_result_unresolved"):
                self.scope()

    def test_ack_requires_user_source_and_x_local_task(self):
        with self.assertRaisesRegex(ValueError, "ExplicitXHistoryAcknowledgementRequired"):
            record_ack(self.engine, self.new["id"], self.account["id"], source="")
        with self.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET platform='facebook' WHERE id=?", (self.account["id"],))
        with self.assertRaisesRegex(ValueError, "ScopeInvalid"):
            self.ack()

    def test_public_api_requires_auth_and_records_no_send(self):
        app = create_app(
            db_path=self.store.path,
            registry=self.engine.adapters,
            clock=self.engine.clock,
            token="synthetic-api",
        )
        path = "/api/tasks/" + self.new["id"] + "/acknowledge-unrelated-x-history"
        body = {"account_id": self.account["id"], "source": "explicit user: new target"}
        status, _, _ = asyncio.run(request(app, path, "POST", body))
        self.assertEqual(status, 401)
        status, value, _ = asyncio.run(
            request(app, path, "POST", body, {"authorization": "Bearer synthetic-api"})
        )
        self.assertEqual(status, 200)
        self.assertEqual(value["history_count"], 1)
        self.assertEqual(
            self.store.one("SELECT status FROM items WHERE id=?", (self.old_item["id"],))["status"],
            "unknown",
        )
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 1)

    def test_mcp_tool_is_explicit_and_never_sends(self):
        from auto_promotion.mcp_server import create_server

        server = create_server(self.engine)
        tools = asyncio.run(server.list_tools())
        tool = next(t for t in tools if t.name == "acknowledge_unrelated_x_history")
        self.assertFalse(tool.annotations.readOnlyHint)
        asyncio.run(
            server.call_tool(
                "acknowledge_unrelated_x_history",
                {
                    "task_id": self.new["id"],
                    "account_id": self.account["id"],
                    "source": "explicit user: new target",
                },
            )
        )
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 1)

    def test_user_ack_then_real_channel_coordination_creates_no_attempt(self):
        self.ack()
        cloud = CloudControl("http://127.0.0.1:1", node_id="node", node_token="synthetic")
        seen = []
        remote = {}

        def request(path, payload=None, **kwargs):
            seen.append(path)
            if path == "/v1/account-channel/status":
                return {"channel": remote or None}
            if path == "/v1/account-channel":
                remote.update(
                    {
                        **{
                            k: payload[k]
                            for k in ("account_id", "task_id", "node_id", "generation", "channel")
                        },
                        "store_id": payload["local_store_id"],
                        "until_at": payload["until"],
                    }
                )
                return {"channel": remote}
            raise AssertionError(path)

        with (
            patch.dict("os.environ", {"AP_NODE_ID": "node"}),
            patch.object(CloudControl, "configured", return_value=cloud),
            patch.object(cloud, "request", side_effect=request),
            patch.object(cloud, "sync_channel_authorization", return_value={}),
            patch.object(self.engine, "_claim", side_effect=AssertionError("must not send")),
        ):
            result = authorize_local_channel(
                self.engine,
                self.new["id"],
                self.account["id"],
                coordinate_once=True,
                source="explicit user: local new targets",
            )
            preflight = local_handoff_preflight(
                self.engine,
                self.account["id"],
                self.new["id"],
                required_until=self.engine.clock() + 1000,
                replacement=False,
                item_ids=[self.item["id"]],
            )
        self.assertTrue(result["authorized"])
        self.assertFalse(result["attempt_created"])
        self.assertTrue(preflight["ready"])
        self.assertEqual(
            seen,
            ["/v1/account-channel/status", "/v1/account-channel", "/v1/account-channel/status"],
        )
        binding = self.store.one(
            "SELECT value FROM meta WHERE key=?", ("account_channel_binding:" + self.account["id"],)
        )
        self.assertTrue(json.loads(binding["value"])["x_history_ack_required"])
        self.assertEqual(
            self.store.one("SELECT status FROM items WHERE id=?", (self.old_item["id"],))["status"],
            "unknown",
        )
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 1)


if __name__ == "__main__":
    unittest.main()
