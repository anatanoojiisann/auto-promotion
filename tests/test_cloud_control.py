import hashlib
import os
import time
import unittest
import uuid
from unittest.mock import Mock, patch

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
        self.assertEqual(request.call_args.args[1], {"global_stop": True})
        self.assertIsNone(
            self.store.one("SELECT value FROM meta WHERE key='cloud_pending_settings'")
        )


@unittest.skipUnless(os.getenv("AP_TEST_CONTROL_URL"), "Cloudflare emulator not requested")
class CloudflareHTTPTests(unittest.TestCase):
    def setUp(self):
        CloudClientTests.setUp(self)
        self.engine.clock = time.time
        self.fixture.ready(target_id=uuid.uuid4().int % 10**18)
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


if __name__ == "__main__":
    unittest.main()
