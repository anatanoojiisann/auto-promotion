"""Isolated regression: no real accounts, service, or execution configuration."""

import asyncio
import importlib.util
import io
import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import URLError

import test_persistent_pools as fixtures
import test_readiness_scope_display as scope_fixtures

from auto_promotion.cloud_control import (
    CloudControl,
    CloudJournal,
    CloudUnavailable,
    export_snapshot,
)
from auto_promotion.readiness import task_workflow


class QueueBlockageRepairTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PersistentPoolTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.engine, self.store = self.fixture.engine, self.fixture.store
        self.cloud = CloudControl("http://127.0.0.1:8791", node_id="synthetic", node_token="test")

    def response(self, value):
        return patch(
            "auto_promotion.cloud_control.urlopen",
            return_value=io.BytesIO(json.dumps(value).encode()),
        )

    def test_empty_block_response_does_not_acquire_writer(self):
        CloudJournal(self.engine, self.cloud)
        self.store.lock_timeout = 0
        with self.store.connect(write=True), self.response({"account_blocks": []}):
            self.assertEqual(self.cloud.request("/v1/heartbeat", {}), {"account_blocks": []})

    def test_duplicate_block_generation_does_not_acquire_writer(self):
        _, account = self.fixture.ready()
        identity = export_snapshot(self.engine)["accounts"][0]["identity"]
        response = {
            "account_blocks": [
                {
                    "account_id": account["id"],
                    "identity": identity,
                    "outbound_block_state": {
                        "state": "active",
                        "reason": "x_challenge",
                        "since": self.engine.clock(),
                        "generation": 2,
                    },
                }
            ]
        }
        self.cloud.import_account_blocks(self.engine, response)
        self.store.lock_timeout = 0
        with self.store.connect(write=True):
            self.cloud.import_account_blocks(self.engine, response)
        self.assertEqual(self.engine.account(account["id"])["outbound_block"]["generation"], 2)

    def test_actual_sqlite_lock_is_local_failure_and_diagnostics_redact(self):
        _, account = self.fixture.ready()
        identity = export_snapshot(self.engine)["accounts"][0]["identity"]
        response = {
            "account_blocks": [
                {
                    "account_id": account["id"],
                    "identity": identity,
                    "outbound_block_state": {
                        "state": "active",
                        "reason": "x_challenge",
                        "since": self.engine.clock(),
                        "generation": 2,
                    },
                }
            ]
        }
        CloudJournal(self.engine, self.cloud)
        self.store.lock_timeout = 0
        self.store.lock_retry_delays = ()
        with self.store.connect(write=True), self.response(response):
            with self.assertRaisesRegex(CloudUnavailable, "LocalDatabaseUnavailable") as caught:
                self.cloud.request("/v1/heartbeat", {})
        self.assertEqual(caught.exception.diagnostics["cause"], "sqlite_busy")
        self.assertEqual(caught.exception.diagnostics["outcome"], "local_persistence_failed")
        self.assertNotIn(account["id"], json.dumps(caught.exception.diagnostics))
        self.assertNotIn(identity, json.dumps(caught.exception.diagnostics))

    def test_network_failure_remains_transport(self):
        with patch(
            "auto_promotion.cloud_control.urlopen",
            side_effect=URLError(ConnectionResetError("secret")),
        ):
            with self.assertRaisesRegex(CloudUnavailable, "CloudTransportUnavailable") as caught:
                self.cloud.request("/v1/heartbeat", {})
        self.assertEqual(caught.exception.diagnostics["cause"], "connection_reset")
        self.assertNotIn("secret", json.dumps(caught.exception.diagnostics))

    def test_lost_claim_recovers_exact_nonce_without_second_claim(self):
        lease = {"id": "job", "lease_token": "secret", "lease_until": 180}
        with patch.object(
            self.cloud,
            "request",
            side_effect=[CloudUnavailable("CloudTransportUnavailable"), {"job": lease}],
        ) as call:
            result = self.cloud.claim("simulation.x", "job")
        self.assertEqual(
            [c.args[0] for c in call.call_args_list], ["/v1/claim", "/v1/claim/recover"]
        )
        self.assertEqual(call.call_args_list[0].args[1], call.call_args_list[1].args[1])
        self.assertEqual(result["claim_nonce"], call.call_args.args[1]["claim_nonce"])

    def test_unknown_is_not_recovered_or_reclaimed(self):
        with patch.object(
            self.cloud, "request", side_effect=[{"job": None}, {"job": {"state": "unknown"}}]
        ) as call:
            with self.assertRaisesRegex(CloudUnavailable, "AlreadySettled"):
                self.cloud.claim("simulation.x", "job")
        self.assertEqual([c.args[0] for c in call.call_args_list], ["/v1/claim", "/v1/job"])

    def test_claim_response_is_not_lost_to_local_import_lock(self):
        CloudJournal(self.engine, self.cloud)
        with (
            self.response({"job": {"id": "job", "lease_token": "secret"}, "account_blocks": []}),
            patch.object(
                self.cloud, "import_account_blocks", side_effect=sqlite3.OperationalError("locked")
            ) as imported,
        ):
            self.assertEqual(self.cloud.claim("simulation.x", "job")["id"], "job")
        imported.assert_not_called()

    def test_api_and_mcp_preserve_safe_local_error(self):
        from test_api import request

        from auto_promotion.api import create_app

        app = create_app(db_path=self.store.path, registry=self.engine.adapters, token="synthetic")
        with patch.object(
            app.state.engine, "task", side_effect=CloudUnavailable("LocalDatabaseUnavailable")
        ):
            status, body, _ = asyncio.run(
                request(app, "/api/tasks/synthetic", headers={"authorization": "Bearer synthetic"})
            )
        self.assertEqual(status, 409)
        self.assertEqual(body, {"error": "LocalDatabaseUnavailable"})
        if importlib.util.find_spec("mcp") is None:
            return  # Existing MCP suite reports the optional dependency as skipped.
        from mcp.server.fastmcp.exceptions import ToolError

        from auto_promotion.mcp_server import create_server

        server = create_server(self.engine)
        with patch.object(
            self.engine, "task", side_effect=CloudUnavailable("LocalDatabaseUnavailable")
        ):
            with self.assertRaisesRegex(ToolError, "LocalDatabaseUnavailable"):
                asyncio.run(server.call_tool("get_task", {"task_id": "synthetic"}))

    def test_local_journal_failure_releases_unexecuted_claim(self):
        from auto_promotion.pool_runtime import run_pool
        from auto_promotion.queue_store import JobQueue

        self.fixture.ready()
        JobQueue(self.engine).sync()
        cloud = Mock()
        cloud.claim.side_effect = lambda pool, jid: {
            **self.store.one("SELECT * FROM execution_jobs WHERE id=?", (jid,)),
            "lease_token": "synthetic",
            "claim_nonce": "synthetic-nonce",
        }
        with (
            patch.object(CloudControl, "configured", return_value=cloud),
            patch.object(
                CloudJournal, "remember", side_effect=sqlite3.OperationalError("synthetic lock")
            ),
            patch.object(self.engine, "run_reference_job") as execute,
        ):
            run_pool(self.engine, "simulation.x", "test", max_jobs=1)
        execute.assert_not_called()
        cloud.release_claim.assert_called_once()
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM attempts")["n"], 0)
        self.assertEqual(self.store.one("SELECT status FROM items")["status"], "not_attempted")

    def test_claim_version_mismatch_does_not_execute(self):
        from auto_promotion.pool_runtime import run_pool
        from auto_promotion.queue_store import JobQueue

        self.fixture.ready()
        JobQueue(self.engine).sync()
        cloud = Mock()
        cloud.claim.side_effect = lambda pool, jid: {
            **self.store.one("SELECT * FROM execution_jobs WHERE id=?", (jid,)),
            "lease_token": "synthetic",
            "claim_nonce": "synthetic-nonce",
            "version": "wrong",
        }
        with (
            patch.object(CloudControl, "configured", return_value=cloud),
            patch.object(self.engine, "run_reference_job") as execute,
        ):
            run_pool(self.engine, "simulation.x", "test", max_jobs=1)
        execute.assert_not_called()
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM attempts")["n"], 0)

    def test_gbk_json_is_lossless(self):
        spec = importlib.util.spec_from_file_location(
            "meta_entry", Path(__file__).parents[1] / "scripts/meta_browser.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        output = io.BytesIO()
        stream = io.TextIOWrapper(output, encoding="gbk", write_through=True)
        payload = {"text": "评论 🎉", "status": "not_attempted"}
        with patch("sys.stdout", stream):
            module.emit_result(payload)
        self.assertEqual(json.loads(output.getvalue().decode("gbk")), payload)


class DesktopEvidenceRepairTests(unittest.TestCase):
    def test_real_registered_desktop_connection_requires_session_and_fresh_identity(self):
        from test_meta_browser import MetaBrowserTests

        fixture = MetaBrowserTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        task, item = fixture.ready()
        current = fixture.engine.task(task["id"])
        self.assertTrue(current["desktop_execution"])
        self.assertNotEqual(current["readiness"]["workflow"]["status"], "waiting_schedule")
        with fixture.store.connect(write=True) as db:
            db.execute(
                "UPDATE meta_browser_accounts SET observation='{}' WHERE account_id=?",
                (fixture.aid,),
            )
        current = fixture.engine.task(task["id"])
        self.assertFalse(current["readiness"]["accounts_ready"])
        self.assertEqual(current["readiness"]["workflow"]["status"], "account_unverified")
        self.assertEqual(current["items"][0]["status"], "not_attempted")
        self.assertEqual(fixture.store.one("SELECT COUNT(*) n FROM attempts")["n"], 0)


class WorkflowBlockageRepairTests(unittest.TestCase):
    def setUp(self):
        f = scope_fixtures.ScopedReadinessDisplayTests()
        f.setUp()
        self.task, self.now = f.task, f.now

    def test_account_failures_take_priority_over_future_schedule(self):
        for state in (
            "unverified",
            "authorization_required",
            "verification_required",
            "restricted",
            "cooldown",
            "disabled",
        ):
            with self.subTest(state=state):
                self.task["account_checks"] = [
                    {
                        "account_id": "a",
                        "state": state,
                        "ready": False,
                        "message": state,
                        "retry_at": self.now + 120,
                    }
                ]
                value = task_workflow(self.task, [], [], self.now)
                self.assertEqual(value["status"], "account_" + state)
                self.assertEqual(value["next_at_kind"], "account_retry")
                self.assertEqual(value["schedule_next_at"], self.now + 3600)

    def test_task_scoped_local_failure_names_local_storage(self):
        cloud = self.task["execution"]["pools"]["cloud"]
        cloud.update(state="connected", execution_ready=True)
        cloud["task_scope"].update(state="LocalDatabaseUnavailable", execution_ready=False)
        value = task_workflow(self.task, [], [], self.now)
        self.assertEqual(value["status"], "waiting_execution_service")
        self.assertIn("本机数据库", value["message"])
        self.assertEqual(value["schedule_next_at"], self.now + 3600)

    def test_desktop_requires_interactive_executor_even_with_future_schedule(self):
        self.task["desktop_execution"] = True
        value = task_workflow(self.task, [], [], self.now)
        self.assertEqual(value["status"], "waiting_desktop_executor")
        self.assertEqual(value["schedule_next_at"], self.now + 3600)
        self.assertIsNone(value["next_at"])

    def test_expired_authority_precedes_desktop_and_does_not_start(self):
        self.task["desktop_execution"] = True
        self.task["channel"] = {"local_authority_expired": True}
        value = task_workflow(self.task, [], [], self.now)
        self.assertEqual(value["status"], "waiting_local_authority")
        self.assertEqual(self.task["state"], "active")


class CloudUnknownReflectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PersistentPoolTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.engine, self.store = self.fixture.engine, self.fixture.store
        self.task, self.account = self.fixture.ready()
        with self.store.connect(write=True) as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES('execution_runtime','pooled')")
        from auto_promotion.queue_store import JobQueue

        self.queue = JobQueue(self.engine)
        self.queue.sync()

    def claim(self):
        return self.queue.claim("simulation.x", "original-owner")

    def test_owned_remote_unknown_survives_expiry_without_attempt_receipt_or_replay(self):
        from auto_promotion.cloud_control import CloudJobClosed
        from auto_promotion.pool_runtime import run_pool

        original = self.engine.task(self.task["id"])
        manifest = original["manifest_hash"]
        payload_hash = original["items"][0]["payload_hash"]
        cloud = Mock()
        cloud.claim.side_effect = CloudJobClosed("unknown")
        with (
            patch.object(CloudControl, "configured", return_value=cloud),
            patch.object(
                self.engine.adapters.simulation, "call", wraps=self.engine.adapters.simulation.call
            ) as adapter,
        ):
            run_pool(self.engine, "simulation.x", "original-owner", max_jobs=1)
            for advance in (0, 4000):
                self.engine.clock = lambda advance=advance: 1800000000 + advance
                self.engine.recover()
                self.queue.recover()
                self.queue.sync()
                self.engine.run_once(self.task["id"])
                self.assertIsNone(self.queue.claim("simulation.x", "replacement"))
                current = self.engine.task(self.task["id"])
                item = current["items"][0]
                self.assertEqual(item["status"], "unknown")
                self.assertIsNone(item["attempt_id"])
                self.assertIsNone(item["external_reference"])
                self.assertFalse(item["result"]["verified"])
                self.assertEqual(
                    current["readiness"]["workflow"]["status"], "completed_pending_verification"
                )
                self.assertEqual(current["readiness"]["workflow"]["counts"]["verified_sent"], 0)
                self.assertEqual(current["manifest_hash"], manifest)
                self.assertEqual(item["payload_hash"], payload_hash)
            self.assertFalse(any(c.args[1] == "execute" for c in adapter.call_args_list))
        self.assertEqual(cloud.claim.call_count, 1)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM attempts")["n"], 0)
        audit = self.store.rows(
            "SELECT details FROM audit WHERE kind='queue.cloud_outcome_unknown'"
        )
        self.assertEqual(len(audit), 1)
        self.assertEqual(json.loads(audit[0]["details"])["previous_status"], "not_attempted")

    def test_stale_owner_cannot_change_replacement_lease_or_item(self):
        job = self.claim()
        with self.store.connect(write=True) as db:
            db.execute("UPDATE execution_jobs SET owner='replacement-owner'")
        self.assertFalse(self.queue.mark_cloud_unknown(job))
        self.assertEqual(
            self.store.one("SELECT owner,state FROM execution_jobs"),
            {"owner": "replacement-owner", "state": "leased"},
        )
        self.assertEqual(self.store.one("SELECT status FROM items")["status"], "not_attempted")

    def test_expired_local_lease_is_not_used_as_authoritative_unknown(self):
        job = self.claim()
        self.engine.clock = lambda: job["lease_until"] + 1
        self.queue.clock = self.engine.clock
        self.assertFalse(self.queue.mark_cloud_unknown(job))
        self.assertEqual(self.store.one("SELECT status FROM items")["status"], "not_attempted")

    def test_changed_job_snapshot_cannot_change_source(self):
        job = self.claim()
        self.assertFalse(self.queue.mark_cloud_unknown({**job, "version": "wrong-snapshot"}))
        self.assertEqual(self.store.one("SELECT state FROM execution_jobs")["state"], "leased")
        self.assertEqual(self.store.one("SELECT status FROM items")["status"], "not_attempted")

    def test_mismatched_source_version_keeps_job_fenced_without_mutating_item(self):
        job = self.claim()
        with self.store.connect(write=True) as db:
            db.execute("UPDATE items SET payload_hash='different-version'")
        self.assertTrue(self.queue.mark_cloud_unknown(job))
        self.assertEqual(self.store.one("SELECT state FROM execution_jobs")["state"], "unknown")
        self.assertEqual(self.store.one("SELECT status FROM items")["status"], "not_attempted")

    def test_payload_tampering_without_new_hash_does_not_authorize_reflection(self):
        job = self.claim()
        with self.store.connect(write=True) as db:
            db.execute("UPDATE items SET payload=json_set(payload,'$.text','changed draft')")
        self.assertTrue(self.queue.mark_cloud_unknown(job))
        self.assertEqual(self.store.one("SELECT state FROM execution_jobs")["state"], "unknown")
        self.assertEqual(self.store.one("SELECT status FROM items")["status"], "not_attempted")

    def test_prior_verified_receipt_is_never_overwritten(self):
        job = self.claim()
        receipt = {"verified": True, "external_reference": "simulation://independent-receipt"}
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE items SET status='confirmed',external_reference=?,result=?",
                (receipt["external_reference"], json.dumps(receipt)),
            )
        self.assertTrue(self.queue.mark_cloud_unknown(job))
        item = self.store.one("SELECT status,external_reference,result FROM items")
        self.assertEqual(item["status"], "confirmed")
        self.assertEqual(item["external_reference"], receipt["external_reference"])
        self.assertEqual(json.loads(item["result"]), receipt)

    def test_historical_unknown_is_not_backfilled_or_reopened(self):
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE execution_jobs SET state='unknown',owner=NULL,result=?",
                (json.dumps({"reason": "cloud_job_already_settled"}),),
            )
        self.queue.recover()
        self.queue.sync()
        self.assertIsNone(self.queue.claim("simulation.x", "replacement"))
        self.assertEqual(self.store.one("SELECT status FROM items")["status"], "not_attempted")
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM attempts")["n"], 0)

    def test_item_attempt_evidence_without_item_link_is_preserved(self):
        from auto_promotion.queue_store import job_scope

        job = self.claim()
        with job_scope(job), self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO attempts(id,item_id,account_id,task_id,target_key,user_key,mode,started_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    "existing-attempt",
                    job["reference_id"],
                    job["account_id"],
                    job["task_id"],
                    "synthetic-target",
                    "synthetic-user",
                    "simulation",
                    self.engine.clock(),
                ),
            )
        self.assertTrue(self.queue.mark_cloud_unknown(job))
        self.assertEqual(self.store.one("SELECT status FROM items")["status"], "not_attempted")
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM attempts")["n"], 1)

    def test_persistence_failure_rolls_back_queue_and_item_together(self):
        job = self.claim()
        with patch.object(
            self.store,
            "audit",
            side_effect=sqlite3.OperationalError("synthetic audit write failure"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.queue.mark_cloud_unknown(job)
        self.assertEqual(
            self.store.one("SELECT state,owner FROM execution_jobs"),
            {"state": "leased", "owner": "original-owner"},
        )
        self.assertEqual(self.store.one("SELECT status FROM items")["status"], "not_attempted")
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM attempts")["n"], 0)
