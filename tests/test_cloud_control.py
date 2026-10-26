import hashlib
import io
import json
import os
import shutil
import subprocess
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import test_persistent_pools as fixtures

from auto_promotion.cloud_control import (
    CloudControl,
    CloudJournal,
    CloudUnavailable,
    export_snapshot,
)
from auto_promotion.models import canonical
from auto_promotion.pool_runtime import run_pool
from auto_promotion.queue_store import JobQueue, verify_job_claim


class CloudClientTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PersistentPoolTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.engine, self.store = self.fixture.engine, self.fixture.store

    def test_remote_block_import_is_identity_bound_and_requires_local_recovery(self):
        _, account = self.fixture.ready()
        mirrored = export_snapshot(self.engine)["accounts"][0]
        cloud = CloudControl("http://127.0.0.1:8791", operator_token="test")
        state = {
            "state": "active",
            "reason": "x_challenge",
            "since": self.engine.clock(),
            "until": None,
            "generation": 4,
            "health_ok": True,
            "health_checked_at": self.engine.clock(),
        }
        response = {
            "account_blocks": [
                {"account_id": account["id"], "identity": "wrong", "outbound_block_state": state}
            ]
        }
        cloud.import_account_blocks(self.engine, response)
        self.assertIsNone(self.engine.account(account["id"])["outbound_block"])
        response["account_blocks"][0]["identity"] = mirrored["identity"]
        cloud.import_account_blocks(self.engine, response)
        block = self.engine.account(account["id"])["outbound_block"]
        self.assertEqual(block["generation"], 4)
        self.assertFalse(block["health_ok"])
        cloud.import_account_blocks(self.engine, response)
        self.assertEqual(self.engine.account(account["id"])["outbound_block"]["generation"], 4)

    def test_heartbeat_response_imports_cloud_only_block_without_new_network_request(self):
        _, account = self.fixture.ready()
        mirrored = export_snapshot(self.engine)["accounts"][0]
        response = {
            "ready": True,
            "account_blocks": [
                {
                    "account_id": account["id"],
                    "identity": mirrored["identity"],
                    "affected_tasks": ["remote-task"],
                    "outbound_block_state": {
                        "state": "active",
                        "reason": "x_challenge",
                        "since": self.engine.clock(),
                        "until": None,
                        "generation": 3,
                    },
                }
            ],
        }
        cloud = CloudControl("http://127.0.0.1:8791", node_id="synthetic", node_token="test")
        CloudJournal(self.engine, cloud)
        with patch(
            "auto_promotion.cloud_control.urlopen",
            return_value=io.BytesIO(json.dumps(response).encode()),
        ) as transport:
            cloud.request("/v1/heartbeat", {})
        self.assertEqual(transport.call_count, 1)
        block = self.engine.account(account["id"])["outbound_block"]
        self.assertEqual(block["generation"], 3)
        self.assertIn("remote-task", block["affected_tasks"])

    def test_snapshot_exports_versioned_block_tombstone_without_health_identity(self):
        _, account = self.fixture.ready()
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO account_blocks(account_id,state,reason,since,until_at,generation,"
                "details,health_checked_at,health_ok,health_identity_hash,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    account["id"],
                    "cleared",
                    "x_challenge",
                    self.engine.clock() - 20,
                    None,
                    2,
                    "{}",
                    self.engine.clock(),
                    1,
                    "private-health-identity",
                    self.engine.clock(),
                ),
            )
        exported = export_snapshot(self.engine)["accounts"][0]
        self.assertEqual(exported["outbound_block_state"]["state"], "cleared")
        self.assertEqual(exported["outbound_block_state"]["generation"], 2)
        self.assertTrue(exported["outbound_block_state"]["health_ok"])
        self.assertNotIn("private-health-identity", json.dumps(exported))

    def test_real_explicit_resume_exports_cloud_accepted_clear_generation(self):
        from auto_promotion.account_blocks import observe

        _, account = self.fixture.ready()
        health = {"healthy": True, "identity_verified": True, "account_identity": "synthetic"}
        with patch.object(self.engine, "call", return_value=health):
            self.engine.check_account(account["id"], force=True)
        with self.store.connect(write=True) as db:
            observe(
                db,
                account["id"],
                {"account_block_reason": "x_challenge"},
                now=self.engine.clock(),
                operation="preflight",
            )
        active = export_snapshot(self.engine)["accounts"][0]["outbound_block_state"]
        with patch.object(self.engine, "call", return_value=health):
            checked = self.engine.check_account_block(account["id"])
        self.assertTrue(checked["outbound_block"]["health_ok"])
        self.engine.resume_account_block(account["id"], active["generation"])
        cleared = export_snapshot(self.engine)["accounts"][0]["outbound_block_state"]
        self.assertEqual(cleared["state"], "cleared")
        self.assertGreater(cleared["generation"], active["generation"])
        self.assertIs(cleared["health_ok"], True)
        self.assertGreaterEqual(cleared["health_checked_at"], cleared["since"])
        node = shutil.which("node")
        if (
            node
            and subprocess.run(
                [node, "-e", "require('node:sqlite')"],
                capture_output=True,
                timeout=10,
            ).returncode
            == 0
        ):
            # Consume the actual Python resume/export payload in the production
            # Worker kernel, rather than hand-building a purported clear proof.
            script = """
import { DatabaseSync } from 'node:sqlite';
import { ControlKernel } from './cloudflare/control-plane/kernel.mjs';
let input=''; for await (const part of process.stdin) input+=part;
const {active,cleared,account}=JSON.parse(input), db=new DatabaseSync(':memory:');
const storage={sql:{exec(q,...a){
  if(q.includes('CREATE TABLE')){db.exec(q);return {toArray:()=>[]};}
  const s=db.prepare(q), rows=s.columns().length?s.all(...a):(s.run(...a),[]);
  return {toArray:()=>rows};
}},transactionSync(fn){db.exec('BEGIN');try{const r=fn();db.exec('COMMIT');return r;}
catch(e){db.exec('ROLLBACK');throw e;}}};
const k=new ControlKernel(storage,{clock:()=>1800000000});
const sync=b=>k.sync({accounts:[{...account,outbound_block_state:b}],tasks:[],jobs:[]});
sync(active);const blocked=k.accountOutboundBlocked(account.identity);
sync(cleared);const resumed=!k.accountOutboundBlocked(account.identity);
sync(active);const staleIgnored=!k.accountOutboundBlocked(account.identity);
process.stdout.write(JSON.stringify({blocked,resumed,staleIgnored}));
"""
            result = subprocess.run(
                [node, "--input-type=module", "-e", script],
                input=json.dumps(
                    {
                        "active": active,
                        "cleared": cleared,
                        "account": export_snapshot(self.engine)["accounts"][0],
                    }
                ),
                text=True,
                capture_output=True,
                timeout=30,
                cwd=Path(__file__).resolve().parents[1],
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(result.stdout), {"blocked": True, "resumed": True, "staleIgnored": True}
            )

    def test_delta_sync_sends_each_definition_once_and_skips_unchanged(self):
        self.fixture.ready()
        snapshot = export_snapshot(self.engine)
        task, job = snapshot["tasks"][0], snapshot["jobs"][0]
        snapshot["tasks"] = [{**task, "id": f"t-{i}"} for i in range(201)]
        snapshot["jobs"] = [{**job, "id": f"j-{i}", "task_id": f"t-{i}"} for i in range(201)]
        cloud = CloudControl("http://127.0.0.1:8791", operator_token="test")
        with (
            patch("auto_promotion.cloud_control.export_snapshot", return_value=snapshot),
            patch.object(cloud, "request", return_value={}) as request,
        ):
            result = cloud.sync(self.engine)
            self.assertEqual(result["changed_records"], 403)
            self.assertEqual(sum(len(c.args[1]["tasks"]) for c in request.call_args_list), 201)
            self.assertEqual(sum(len(c.args[1]["jobs"]) for c in request.call_args_list), 201)
            request.reset_mock()
            self.assertTrue(cloud.sync(self.engine)["unchanged"])
            request.assert_not_called()

    def test_task_scoped_sync_keeps_global_ledger_and_omits_other_frozen_jobs(self):
        first, account = self.fixture.ready()
        second, _ = self.fixture.ready(account=account, target_id=999)
        snapshot = export_snapshot(self.engine)
        snapshot["quota_events"] = [
            {
                "attempt_id": "historical",
                "scope": "cloud_global",
                "key": "simulation",
                "at": self.engine.clock(),
            }
        ]
        cloud = CloudControl("http://127.0.0.1:8791", operator_token="test")
        with (
            patch("auto_promotion.cloud_control.export_snapshot", return_value=snapshot),
            patch.object(cloud, "request", return_value={}) as request,
        ):
            cloud.sync(self.engine, task_ids=[second["id"]])
        tasks = [t for c in request.call_args_list for t in c.args[1]["tasks"]]
        jobs = [j for c in request.call_args_list for j in c.args[1]["jobs"]]
        quotas = [q for c in request.call_args_list for q in c.args[1]["quota_events"]]
        self.assertEqual([t["id"] for t in tasks], [second["id"]])
        self.assertTrue(jobs)
        self.assertTrue(all(j["task_id"] == second["id"] for j in jobs))
        self.assertNotIn(first["id"], [t["id"] for t in tasks])
        self.assertEqual(quotas[0]["attempt_id"], "historical")

    def test_scoped_export_does_not_validate_unrelated_broken_definitions(self):
        old, account = self.fixture.ready()
        current, _ = self.fixture.ready(account=account, target_id=999)
        with self.store.connect(write=True) as db:
            db.execute("UPDATE tasks SET policy='{}' WHERE id=?", (old["id"],))
        snapshot = export_snapshot(self.engine, task_ids=[current["id"]])
        self.assertEqual([t["id"] for t in snapshot["tasks"]], [current["id"]])
        self.assertEqual(len(snapshot["jobs"]), 1)
        with self.assertRaisesRegex(CloudUnavailable, "CloudSnapshotInconsistent"):
            export_snapshot(self.engine)

    def test_failed_delta_resumes_without_replaying_acknowledged_slices(self):
        self.fixture.ready()
        snapshot = export_snapshot(self.engine)
        task = snapshot["tasks"][0]
        snapshot["tasks"] = [{**task, "id": f"t-{i}"} for i in range(201)]
        cloud = CloudControl("http://127.0.0.1:8791", operator_token="test")

        def first(path, payload, **kwargs):
            if payload["tasks"] and payload["tasks"][0]["id"] == "t-100":
                raise CloudUnavailable("CloudTransportUnavailable")
            return {}

        with patch("auto_promotion.cloud_control.export_snapshot", return_value=snapshot):
            with patch.object(cloud, "request", side_effect=first):
                with self.assertRaises(CloudUnavailable):
                    cloud.sync(self.engine)
            with patch.object(cloud, "request", return_value={}) as request:
                cloud.sync(self.engine)
                resent = [t["id"] for c in request.call_args_list for t in c.args[1]["tasks"]]
                self.assertEqual(resent, [f"t-{i}" for i in range(100, 201)])

    def test_unfrozen_outbound_is_not_exported_as_an_execution_job(self):
        task, _ = self.fixture.ready(start=False)
        self.assertEqual(export_snapshot(self.engine)["jobs"], [])
        self.engine.start_task(task["id"])
        self.assertEqual(len(export_snapshot(self.engine)["jobs"]), 1)

    def test_cloud_quota_error_keeps_safe_code_and_retry_delay(self):
        cloud = CloudControl("http://127.0.0.1:8791", operator_token="test")
        error = HTTPError(
            cloud.url,
            503,
            "Unavailable",
            {},
            io.BytesIO(
                json.dumps(
                    {"error": "CloudWriteQuotaExceeded", "retry_after_seconds": 3600}
                ).encode()
            ),
        )
        with patch("auto_promotion.cloud_control.urlopen", side_effect=error):
            with self.assertRaises(CloudUnavailable) as raised:
                cloud.request("/v1/status", operator=True, method="GET")
        self.assertEqual(str(raised.exception), "CloudWriteQuotaExceeded")
        self.assertEqual(raised.exception.retry_after_seconds, 3600)

    def test_scheduler_backoff_persists_across_restart_without_platform_attempt(self):
        from auto_promotion.pool_runtime import PoolScheduler

        self.fixture.ready()
        cloud = Mock()
        cloud.request.side_effect = CloudUnavailable(
            "CloudWriteQuotaExceeded", retry_after_seconds=3600
        )
        with patch.object(CloudControl, "configured", return_value=cloud):
            scheduler = PoolScheduler(self.engine, popen=Mock())
            scheduler.tick()
            scheduler.tick()
            PoolScheduler(self.engine, popen=Mock()).tick()
        self.assertEqual(cloud.request.call_count, 1)
        self.assertEqual(self.store.one("SELECT count(*) n FROM attempts")["n"], 0)
        health = json.loads(
            self.store.one("SELECT value FROM meta WHERE key='cloud_control_health'")["value"]
        )
        self.assertFalse(health["execution_ready"])
        self.assertEqual(health["retry_at"], self.engine.clock() + 3600)

    def lease(self):
        self.fixture.ready()
        q = JobQueue(self.engine)
        q.sync()
        job = q.claim("simulation.x", "owner")
        return {**job, "lease_token": "test-only"}

    def test_snapshot_contains_immutable_content_but_no_profile_credentials(self):
        self.fixture.ready()
        snapshot = export_snapshot(self.engine)
        self.assertEqual(
            snapshot["jobs"][0]["version"],
            self.store.one("SELECT payload_hash FROM items")["payload_hash"],
        )
        self.assertEqual(
            snapshot["tasks"][0]["manifest_hash"],
            self.store.one("SELECT manifest_hash FROM tasks")["manifest_hash"],
        )
        self.assertFalse(
            any(
                key in canonical(snapshot)
                for key in ('"access_token"', '"cookies"', '"session_string"', '"api_hash"')
            )
        )

    def test_receipt_failure_is_persisted_and_recovery_uploads_only_receipt(self):
        lease = self.lease()
        cloud = Mock()
        cloud.complete.side_effect = CloudUnavailable("CloudTransportUnavailable")
        journal = CloudJournal(self.engine, cloud)
        journal.remember(lease)
        self.engine.run_reference_job(lease)
        JobQueue(self.engine).settle(lease)
        journal.finish(lease)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM cloud_receipt_outbox")["n"], 1)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM attempts")["n"], 1)
        cloud.complete.side_effect = None
        with self.store.connect(write=True) as db:
            db.execute("UPDATE cloud_receipt_outbox SET retry_at=0")
        CloudJournal(self.engine, cloud).flush()
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM cloud_receipt_outbox")["n"], 0)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM attempts")["n"], 1)
        self.assertEqual(cloud.complete.call_args.kwargs["state"], "confirmed")

    def test_restart_during_uncertain_attempt_uploads_unknown_without_execution(self):
        lease = self.lease()
        cloud = Mock()
        CloudJournal(self.engine, cloud).remember(lease)
        with self.store.connect(write=True) as db:
            db.execute("UPDATE execution_jobs SET lease_until=0")
            db.execute("UPDATE items SET status='executing',attempt_id='uncertain-attempt'")
        CloudJournal(self.engine, cloud).flush()
        self.assertEqual(cloud.complete.call_args.kwargs["state"], "unknown")
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM attempts")["n"], 0)

    def test_cloud_required_cannot_fall_back_to_local_writes(self):
        self.fixture.ready()
        JobQueue(self.engine).sync()
        with self.store.connect(write=True) as db:
            db.execute("INSERT INTO meta VALUES('cloud_control_required','1')")
        with patch.object(CloudControl, "configured", return_value=None):
            with self.assertRaisesRegex(CloudUnavailable, "ConfigurationMissing"):
                run_pool(self.engine, "simulation.x", "test", max_jobs=1)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM attempts")["n"], 0)

    def test_native_guard_rejects_expired_owner_and_missing_cloud_lease(self):
        lease = self.lease()
        request = {
            "execution_job": {"id": lease["id"], "owner": "wrong"},
            "account_id": lease["account_id"],
            "operation": "execute",
            "payload": {"item_id": lease["reference_id"]},
        }
        with self.store.connect() as db:
            with self.assertRaisesRegex(ValueError, "ClaimExpired"):
                verify_job_claim(db, request)
        with self.store.connect(write=True) as db:
            db.execute("INSERT INTO meta VALUES('cloud_control_required','1')")
            request["execution_job"]["owner"] = "owner"
            with self.assertRaisesRegex(ValueError, "CloudExecutionClaimRequired"):
                verify_job_claim(db, request)

    def test_cloud_authorization_failure_never_enters_adapter(self):
        self.fixture.ready()
        queue = JobQueue(self.engine)
        queue.sync()
        cloud = Mock()
        cloud.claim.side_effect = lambda pool, job_id: {
            **self.store.one("SELECT * FROM execution_jobs WHERE id=?", (job_id,)),
            "lease_token": "test-only",
        }
        cloud.guard.side_effect = CloudUnavailable("CloudTransportUnavailable")
        with (
            patch.object(CloudControl, "configured", return_value=cloud),
            patch.object(
                self.engine.adapters.simulation, "call", wraps=self.engine.adapters.simulation.call
            ) as adapter,
        ):
            run_pool(self.engine, "simulation.x", "test", max_jobs=1)
        self.assertFalse(any(call.args[1] == "execute" for call in adapter.call_args_list))
        self.assertEqual(self.store.one("SELECT status FROM items")["status"], "unknown")

    def test_readonly_reconciliation_updates_cloud_receipt_without_replaying(self):
        lease = self.lease()
        self.engine.adapters.overrides["x"] = fixtures.UnknownAdapter()
        journal = CloudJournal(self.engine, Mock())
        journal.remember(lease)
        self.engine.run_reference_job(lease)
        JobQueue(self.engine).settle(lease)
        journal.finish(lease)
        self.engine.reconcile(lease["reference_id"])
        journal.flush()
        self.assertEqual(journal.cloud.complete.call_args.kwargs["state"], "confirmed")
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM attempts")["n"], 1)

    def test_urls_reject_plaintext_remote_and_embedded_credentials(self):
        for url in (
            "http://example.com",
            "https://secret@example.com",
            "https://example.com?token=secret",
        ):
            with self.assertRaises(ValueError):
                CloudControl(url)

    def test_unavailable_profile_does_not_rewrite_historical_job_route(self):
        self.fixture.ready()
        first = export_snapshot(self.engine)
        from auto_promotion.routing import route_for

        def disconnected(account, payload=None):
            if payload is not None:
                raise ValueError("ProfileUnavailable")
            return route_for(account)

        with patch("auto_promotion.cloud_control.route_for", side_effect=disconnected):
            later = export_snapshot(self.engine)
        self.assertEqual(first["jobs"][0]["pool"], later["jobs"][0]["pool"])

    def test_explicit_stop_change_is_durable_until_cloud_acknowledges(self):
        with self.store.connect(write=True) as db:
            db.execute("INSERT INTO meta VALUES('cloud_control_required','1')")
        self.engine.set_stop(True)
        cloud = CloudControl("http://127.0.0.1:8791", operator_token="test")
        with patch.object(
            cloud, "request", side_effect=CloudUnavailable("CloudTransportUnavailable")
        ):
            with self.assertRaises(CloudUnavailable):
                cloud.flush_settings(self.engine)
        self.assertIsNotNone(
            self.store.one("SELECT value FROM meta WHERE key='cloud_pending_settings'")
        )
        with patch.object(cloud, "request", return_value={}) as request:
            cloud.flush_settings(self.engine)
        from auto_promotion.execution_channels import binding

        self.assertEqual(
            request.call_args.args[1], {"global_stop": True, "local_store_id": binding(self.engine)}
        )
        self.assertIsNone(
            self.store.one("SELECT value FROM meta WHERE key='cloud_pending_settings'")
        )

    def test_cloud_renewal_waits_for_margin_and_uses_monotonic_time(self):
        cloud = CloudControl("http://127.0.0.1:8791", node_id="n", node_token="test")
        now = [100.0]
        replies = [
            {
                "job": {
                    "id": "j",
                    "lease_token": "t",
                    "lease_until": 1000180,
                    "renew_after_seconds": 120,
                }
            },
            {"renewed": True, "lease_until": 1000300, "renew_after_seconds": 120},
        ]
        with (
            patch("auto_promotion.cloud_control.time.monotonic", side_effect=lambda: now[0]),
            patch.object(cloud, "request", side_effect=replies) as request,
        ):
            lease = cloud.claim("simulation.x", "j")
            for elapsed in range(0, 120, 10):
                now[0] = 100 + elapsed
                self.assertTrue(cloud.renew(lease))
            self.assertEqual(request.call_count, 1)
            now[0] = 220
            with patch("auto_promotion.cloud_control.time.time", return_value=-100000):
                self.assertTrue(cloud.renew(lease))
            self.assertEqual(request.call_count, 2)
            self.assertEqual(lease["_renew_at_monotonic"], 340)
            self.assertEqual(lease["lease_until"], 1000300)

    def test_failed_renewal_never_extends_the_local_remote_deadline(self):
        cloud = CloudControl("http://127.0.0.1:8791", node_id="n", node_token="test")
        lease = {"id": "j", "lease_token": "t", "_renew_at_monotonic": 100}
        with (
            patch("auto_promotion.cloud_control.time.monotonic", return_value=100),
            patch.object(cloud, "request", side_effect=CloudUnavailable("LostResponse")),
        ):
            with self.assertRaises(CloudUnavailable):
                cloud.renew(lease)
        self.assertEqual(lease["_renew_at_monotonic"], 100)

    def test_old_server_without_renewal_duration_keeps_safe_frequent_checks(self):
        cloud = CloudControl("http://127.0.0.1:8791", node_id="n", node_token="test")
        lease = {"id": "j", "lease_token": "t"}
        with patch.object(cloud, "request", return_value={"renewed": True}) as request:
            self.assertTrue(cloud.renew(lease))
            self.assertTrue(cloud.renew(lease))
        self.assertEqual(request.call_count, 2)

    def test_idle_scheduler_probes_every_five_minutes_but_flushes_stops_each_tick(self):
        from auto_promotion.pool_runtime import PoolScheduler

        now = [self.engine.clock()]
        self.engine.clock = lambda: now[0]
        cloud = Mock()
        cloud.sync.return_value = {"unchanged": True}
        with patch.object(CloudControl, "configured", return_value=cloud):
            scheduler = PoolScheduler(self.engine, popen=Mock())
            scheduler.tick()
            now[0] += 299
            scheduler.tick()
            self.assertEqual(cloud.request.call_count, 1)
            now[0] += 1
            scheduler.tick()
        self.assertEqual(cloud.request.call_count, 2)
        self.assertEqual(cloud.flush_settings.call_count, 3)

    def test_due_work_uses_active_probe_interval_and_recent_sync_can_supply_liveness(self):
        from auto_promotion.pool_runtime import PoolScheduler

        self.fixture.ready()
        now = [self.engine.clock()]
        self.engine.clock = lambda: now[0]
        cloud = Mock()
        cloud.sync.return_value = {"unchanged": True}
        with (
            patch.object(CloudControl, "configured", return_value=cloud),
            patch.dict(os.environ, {"AP_EXECUTOR_POOLS": "telegram.mtproto"}),
        ):
            scheduler = PoolScheduler(self.engine, popen=Mock())
            scheduler.tick()
            now[0] += 59
            scheduler.tick()
            self.assertEqual(cloud.request.call_count, 1)
            now[0] += 1
            scheduler.tick()
            self.assertEqual(cloud.request.call_count, 2)
            now[0] += 60
            cloud.last_contact_monotonic = time.monotonic()
            scheduler.tick()
            self.assertEqual(cloud.request.call_count, 2)


@unittest.skipUnless(os.getenv("AP_TEST_CONTROL_URL"), "Cloudflare emulator not requested")
class CloudflareHTTPTests(unittest.TestCase):
    def setUp(self):
        CloudClientTests.setUp(self)
        self.engine.clock = time.time
        self.fixture.ready(target_id=uuid.uuid4().int % 10**18)
        # Independent test databases share one emulator workspace. Give each
        # fixture a later cursor instead of replaying revision 1 after revision 2.
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO meta VALUES('cloud_snapshot_revision',?)",
                (str(int(time.time() * 1_000_000)),),
            )
        self.cloud = CloudControl(
            os.environ["AP_TEST_CONTROL_URL"],
            operator_token="test-control-only",
            node_id="test-" + uuid.uuid4().hex,
            node_token=uuid.uuid4().hex,
        )
        self.cloud.sync(self.engine)
        self.cloud.request(
            "/v1/nodes",
            {
                "id": self.cloud.node_id,
                "token_hash": hashlib.sha256(self.cloud.node_token.encode()).hexdigest(),
                "pools": ["simulation.x"],
                "account_ids": [a["id"] for a in self.engine.accounts()],
            },
            operator=True,
        )
        self.cloud.request(
            "/v1/settings",
            {
                "global_stop": False,
                "live_enabled": False,
                "max_workers": 6,
                "global_limit": {"daily": 100000, "hourly": 100000},
            },
            operator=True,
            method="PUT",
        )

    def test_real_worker_http_execution_and_receipt(self):
        queue = JobQueue(self.engine)
        queue.sync()
        with patch.object(CloudControl, "configured", return_value=self.cloud):
            run_pool(self.engine, "simulation.x", self.cloud.node_id, max_jobs=1)
        self.assertEqual(self.store.one("SELECT status FROM items")["status"], "confirmed")
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM cloud_receipt_outbox")["n"], 0)
        status = self.cloud.request("/v1/status", operator=True, method="GET")
        self.assertTrue(any(j["state"] == "confirmed" for j in status["jobs"]))

    def test_authentication_rejects_fake_operator_and_forged_headers(self):
        bad = CloudControl(self.cloud.url, operator_token="wrong")
        with self.assertRaises(CloudUnavailable):
            bad.request("/v1/status", operator=True, method="GET")

    def test_two_nodes_racing_for_the_same_job_get_one_lease_and_one_attempt(self):
        other = CloudControl(
            self.cloud.url,
            node_id="test-" + uuid.uuid4().hex,
            node_token=uuid.uuid4().hex,
        )
        self.cloud.request(
            "/v1/nodes",
            {
                "id": other.node_id,
                "token_hash": hashlib.sha256(other.node_token.encode()).hexdigest(),
                "pools": ["simulation.x"],
                "account_ids": [a["id"] for a in self.engine.accounts()],
            },
            operator=True,
        )
        job_id = export_snapshot(self.engine)["jobs"][0]["id"]
        clients = [self.cloud, other]
        with ThreadPoolExecutor(max_workers=2) as executor:
            leases = list(executor.map(lambda c: c.claim("simulation.x", job_id), clients))
        self.assertEqual(sum(lease is not None for lease in leases), 1)
        index = next(i for i, lease in enumerate(leases) if lease)
        client, lease = clients[index], leases[index]
        attempt = "attempt-" + uuid.uuid4().hex
        self.assertTrue(client.authorize(lease, attempt))
        self.assertTrue(client.authorize(lease, attempt))
        with self.assertRaisesRegex(CloudUnavailable, "AttemptAlreadyRecorded"):
            client.authorize(lease, "different")
        client.complete(lease, "unknown", {"verified": False}, attempt)
        with self.assertRaisesRegex(CloudUnavailable, "AlreadySettled"):
            other.claim("simulation.x", job_id)
        result = {"verified": True, "external_reference": "https://example.invalid/test"}
        client.complete(lease, "confirmed", result, attempt)
        client.complete(lease, "confirmed", result, attempt)

    def test_http_cannot_claim_before_due_but_can_claim_when_due(self):
        self.engine.clock = lambda: time.time() + 3
        task, account = self.fixture.ready(
            account=self.engine.accounts()[0], target_id=uuid.uuid4().int % 10**18
        )
        self.engine.clock = time.time
        self.cloud.sync(self.engine)
        self.cloud.request(
            "/v1/nodes",
            {
                "id": self.cloud.node_id,
                "token_hash": hashlib.sha256(self.cloud.node_token.encode()).hexdigest(),
                "pools": ["simulation.x"],
                "account_ids": [a["id"] for a in self.engine.accounts()],
            },
            operator=True,
        )
        job = next(j for j in export_snapshot(self.engine)["jobs"] if j["task_id"] == task["id"])
        self.assertIsNone(self.cloud.claim("simulation.x", job["id"]))
        time.sleep(max(0, job["available_at"] - time.time()) + 0.05)
        lease = self.cloud.claim("simulation.x", job["id"])
        self.assertIsNotNone(lease)
        self.assertEqual(lease["account_id"], account["id"])
        attempt = "attempt-" + uuid.uuid4().hex
        self.assertTrue(self.cloud.authorize(lease, attempt))
        self.cloud.complete(
            lease,
            "confirmed",
            {"verified": True, "external_reference": "https://example.invalid/timed-test"},
            attempt,
        )


if __name__ == "__main__":
    unittest.main()
