"""Fixed-count fault injections: every platform client and credential is synthetic."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import test_persistent_pools as fixtures
from test_native_backends import FakeTelegram

from auto_promotion import native_bridge
from auto_promotion.adapters import AdapterRegistry
from auto_promotion.backends.common import Receipts, save_profile
from auto_promotion.backends.telegram import TelegramBackend
from auto_promotion.cloud_control import CloudControl, CloudUnavailable, export_snapshot
from auto_promotion.core import Engine
from auto_promotion.execution_channels import activate_local
from auto_promotion.models import canonical
from auto_promotion.pool_runtime import PoolScheduler
from auto_promotion.queue_store import JobQueue
from auto_promotion.store import Store
from auto_promotion.target_checks import TargetChecks


class FaultTelegram(FakeTelegram):
    def __init__(self):
        super().__init__()
        self.banned = self.left = self.server_denies = False
        self.fault = None
        self.permission_fault = None
        self.join_calls = self.send_calls = self.accepted = 0

    async def get_permissions(self, entity, participant):
        if self.permission_fault:
            raise self.permission_fault
        return types.SimpleNamespace(is_banned=self.banned, has_left=self.left, participant=None)

    async def send_message(self, entity, text, **kwargs):
        from telethon import errors

        self.send_calls += 1
        if self.server_denies:
            raise errors.ChatWriteForbiddenError(None)
        message = await super().send_message(entity, text, **kwargs)
        self.accepted += 1
        if self.fault:
            raise self.fault
        return message

    async def __call__(self, request):
        from telethon import functions

        if isinstance(request, functions.channels.JoinChannelRequest):
            self.join_calls += 1
        return await super().__call__(request)


class ExtremeTelegramTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.environment = patch.dict(os.environ, {"AP_ACCOUNTS_DIR": self.temp.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        save_profile(
            "telegram", "synthetic", {"identity": "42", "api_id": 1, "api_hash": "synthetic-only"}
        )
        self.client = FaultTelegram()
        self.backend = TelegramBackend("synthetic", client_factory=lambda *args: self.client)
        self.item = {
            "action": "send_message",
            "target": "@synthetic_group",
            "text": "synthetic",
            "media": [],
            "reply_to": None,
        }
        self.request = {
            "platform": "telegram",
            "account": "synthetic",
            "operation": "execute",
            "payload": {"item_id": "one", "payload": self.item},
        }

    def dispatch(self):
        with (
            patch.object(native_bridge, "verify_claim"),
            patch("auto_promotion.backends.telegram.TelegramBackend", return_value=self.backend),
        ):
            return native_bridge.dispatch(self.request)

    def test_muted_or_kicked_member_never_calls_send(self):
        for mode in ("banned", "left"):
            with self.subTest(mode=mode):
                setattr(self.client, mode, True)
                result = asyncio.run(
                    self.backend.run(
                        "execute", {"payload": self.item}, lambda _: None, lambda: None
                    )
                )
                self.assertEqual(self.client.send_calls, 0)
                self.assertNotEqual(result.get("status"), "confirmed")
                setattr(self.client, mode, False)

    def test_permission_revoked_after_preflight_is_rechecked_before_send(self):
        self.assertTrue(
            asyncio.run(
                self.backend.run("preflight", {"payload": self.item}, lambda _: None, lambda: None)
            )["allowed"]
        )
        self.client.banned = True
        result = self.dispatch()
        self.assertEqual(self.client.send_calls, 0)
        self.assertEqual(self.client.accepted, 0)
        self.assertFalse(result.get("verified", False))

    def test_final_rpc_permission_race_is_decided_by_server_without_acceptance(self):
        self.client.server_denies = True
        result = self.dispatch()
        self.assertEqual(self.client.send_calls, 1)
        self.assertEqual(self.client.accepted, 0)
        self.assertNotEqual(result.get("status"), "confirmed")
        self.dispatch()
        self.assertEqual(self.client.send_calls, 1)

    def test_permission_recovery_requires_fresh_task_item_and_never_reopens_old_receipt(self):
        self.client.banned = True
        denied = self.dispatch()
        self.assertEqual(denied["status"], "failed_known")
        self.assertIs(
            Receipts("telegram", "synthetic").read("execute:one")["submission_started"], False
        )
        self.assertEqual(self.client.send_calls, 0)
        self.client.banned = False
        self.assertTrue(
            asyncio.run(
                self.backend.run("preflight", {"payload": self.item}, lambda _: None, lambda: None)
            )["allowed"]
        )
        self.assertEqual(self.dispatch()["status"], "failed_known")
        self.assertEqual(self.client.send_calls, 0)
        # verify_claim is an explicit synthetic authorization fixture here;
        # a fresh task owns a new immutable item, never clears the old receipt.
        self.request["task_id"] = "new-explicitly-started-synthetic-task"
        self.request["payload"]["item_id"] = "fresh-item"
        self.assertEqual(self.dispatch()["status"], "confirmed")
        self.assertEqual(self.client.send_calls, 1)
        self.request["payload"]["item_id"] = "one"
        self.assertEqual(self.dispatch()["status"], "failed_known")
        self.assertEqual(self.client.send_calls, 1)

    def test_final_permission_read_errors_never_cross_submission_boundary(self):
        from telethon import errors

        for index, error in enumerate(
            (
                errors.ChannelPrivateError(None),
                errors.UserNotParticipantError(None),
                errors.ChatWriteForbiddenError(None),
                errors.ChatAdminRequiredError(None),
                errors.FloodWaitError(None, 120),
                TimeoutError(),
            )
        ):
            with self.subTest(error=type(error).__name__):
                self.request["payload"]["item_id"] = "permission-error-" + str(index)
                self.client.permission_fault = error
                result = self.dispatch()
                receipt = Receipts("telegram", "synthetic").read(
                    "execute:" + self.request["payload"]["item_id"]
                )
                self.assertIs(receipt["submission_started"], False)
                self.assertEqual(self.client.send_calls, 0)
                self.assertEqual(
                    result["status"],
                    "unknown" if isinstance(error, TimeoutError) else "failed_known",
                )
                if isinstance(error, errors.FloodWaitError):
                    self.assertEqual(result["retry_after_seconds"], 120)
                self.client.permission_fault = None
                self.dispatch()
                self.assertEqual(self.client.send_calls, 0)

    def test_identity_switch_during_restart_never_sends(self):
        self.client.me.id = 43
        result = self.dispatch()
        self.assertEqual(self.client.send_calls, 0)
        self.assertNotEqual(result.get("status"), "confirmed")

    def test_pending_admin_to_joined_is_read_only_and_waits_an_hour(self):
        self.client.pending = True

        def call(op):
            return asyncio.run(
                self.backend.run(
                    op,
                    {"candidate_id": "pending", "target": "@synthetic_group"},
                    lambda _: None,
                    lambda: None,
                )
            )

        self.assertEqual(call("join")["status"], "pending_admin_approval")
        self.assertFalse(call("verify_join")["verified"])
        self.client.joined = True  # synthetic administrator approval
        before = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
        with patch("auto_promotion.backends.telegram.datetime", wraps=datetime) as clock:
            clock.now.return_value = before
            result = call("verify_join")
        self.assertTrue(result["verified"])
        from auto_promotion.models import epoch

        self.assertEqual(epoch(result["outbound_eligible_at_utc"]), before.timestamp() + 3600)
        self.assertEqual(self.client.join_calls, 1)

    def test_timeout_incomplete_read_and_process_interruption_never_replay(self):
        for index, fault in enumerate(
            (TimeoutError(), IncompleteRead(b"synthetic", 50), KeyboardInterrupt())
        ):
            with self.subTest(fault=type(fault).__name__):
                self.request["payload"]["item_id"] = "fault-" + str(index)
                self.client.fault = fault
                old = self.client.send_calls
                if isinstance(fault, KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        self.dispatch()
                else:
                    self.assertEqual(self.dispatch()["status"], "unknown")
                self.client.fault = None
                result = self.dispatch()
                self.assertEqual(result["status"], "unknown")
                self.assertTrue(result["submission_started"])
                self.assertEqual(self.client.send_calls, old + 1)

    def test_receipt_failure_before_submission_prevents_send(self):
        original = Receipts.save

        def save(receipts, key, result):
            if result.get("submission_started"):
                raise sqlite3.OperationalError("database is locked")
            return original(receipts, key, result)

        with patch.object(Receipts, "save", side_effect=None, autospec=True) as mocked:
            mocked.side_effect = save
            self.assertEqual(self.dispatch()["status"], "unknown")
        self.assertEqual(self.client.send_calls, 0)
        self.assertEqual(self.dispatch()["status"], "unknown")
        self.assertEqual(self.client.send_calls, 0)

    def test_real_child_exit_after_acceptance_preserves_durable_unknown(self):
        root = Path(__file__).resolve().parents[1]
        marker = Path(self.temp.name) / "accepted-count"
        program = """
import json, os
from pathlib import Path
from auto_promotion import native_bridge
native_bridge.verify_claim=lambda request: None
def submit(label, operation, payload, checkpoint, authorize):
 checkpoint.progress('platform_submission',True)
 with Path(os.environ['SYNTHETIC_MARKER']).open('a') as f:
  f.write('accepted\\n');f.flush();os.fsync(f.fileno())
 os._exit(17)
import auto_promotion.backends.telegram as telegram
telegram.call=submit
request={'platform':'telegram','account':'crash-only','operation':'execute','payload':{'item_id':'crash','payload':{'text':'synthetic'}}}
print(json.dumps(native_bridge.dispatch(request)))
"""
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": self.temp.name,
            "PYTHONPATH": str(root / "src"),
            "AP_ACCOUNTS_DIR": self.temp.name,
            "SYNTHETIC_MARKER": str(marker),
        }
        # Windows needs these non-secret OS paths to initialize asyncio/Winsock.
        # Keep the child otherwise isolated from real service credentials.
        env.update({key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ})
        first = subprocess.run(
            [sys.executable, "-c", program], env=env, capture_output=True, timeout=15
        )
        self.assertEqual(first.returncode, 17, first.stderr.decode(errors="replace"))
        second = subprocess.run(
            [sys.executable, "-c", program], env=env, capture_output=True, timeout=15
        )
        self.assertEqual(second.returncode, 0, second.stderr.decode())
        self.assertEqual(json.loads(second.stdout)["status"], "unknown")
        self.assertEqual(marker.read_text().splitlines(), ["accepted"])


class ExtremeDatabaseCloudTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PersistentPoolTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.engine, self.store = self.fixture.engine, self.fixture.store
        self.store.lock_timeout = 0.005
        self.store.lock_retry_delays = (0.005, 0.01, 0.02, 0.04)

    def test_twenty_short_lock_releases_and_three_timeouts_never_replay_bodies(self):
        for _ in range(20):
            entered, release = threading.Event(), threading.Event()

            def holder():
                with closing(sqlite3.connect(self.store.path)) as db:
                    db.execute("BEGIN IMMEDIATE")
                    entered.set()
                    release.wait(2)

            thread = threading.Thread(target=holder)
            thread.start()
            self.assertTrue(entered.wait(2))
            timer = threading.Timer(0.012, release.set)
            timer.start()
            try:
                bodies = []
                with self.store.connect(write=True) as db:
                    bodies.append(True)
                    db.execute("UPDATE meta SET value=value WHERE key='global_stop'")
                self.assertEqual(bodies, [True])
            finally:
                release.set()
                thread.join(2)
                timer.join(2)
        for _ in range(3):
            with closing(sqlite3.connect(self.store.path)) as db:
                db.execute("BEGIN IMMEDIATE")
                start = time.monotonic()
                with self.assertRaises(sqlite3.OperationalError):
                    with self.store.connect(write=True):
                        self.fail("locked body ran")
                self.assertLess(time.monotonic() - start, 0.8)

    def test_eight_process_claim_race_repeated_five_times_claims_once(self):
        self.fixture.ready()
        JobQueue(self.engine).sync()
        program = """
import sys
from auto_promotion.store import Store
from auto_promotion.core import Engine
from auto_promotion.adapters import AdapterRegistry
from auto_promotion.queue_store import JobQueue
store=Store.__new__(Store);store.path=sys.argv[1]
e=Engine(store,AdapterRegistry(sys.argv[2]),clock=lambda:1800000000)
print('claimed' if JobQueue(e).claim('simulation.x',sys.argv[3]) else 'empty')
"""
        for iteration in range(5):

            def claim(index):
                return subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        program,
                        self.store.path,
                        self.fixture.temp.name,
                        f"child-{iteration}-{index}",
                    ],
                    capture_output=True,
                    timeout=20,
                )

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(claim, range(8)))
            self.assertTrue(
                all(r.returncode == 0 for r in results), [r.stderr.decode() for r in results]
            )
            self.assertEqual(sum(r.stdout.strip() == b"claimed" for r in results), 1)
            with self.store.connect(write=True) as db:
                db.execute("UPDATE execution_jobs SET state='pending',owner=NULL,lease_until=0")
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM attempts")["n"], 0)

    def test_negative_cache_operation_account_target_and_restart_isolation(self):
        _, a = self.fixture.ready()
        a.update(platform="telegram", mode="live", identity_hash="synthetic")
        checks = TargetChecks(self.engine, a, "preflight", {"payload": {"target": "@group"}})
        checks.save(checks.failure("ChannelPrivateError"))
        for op in ("verify_join", "feedback"):
            self.assertIsNone(TargetChecks(self.engine, a, op, {"target": "@group"}).read())
        reopened = Engine(
            Store(self.store.path), AdapterRegistry(self.fixture.temp.name), clock=self.engine.clock
        )
        self.assertIsNotNone(
            TargetChecks(reopened, a, "preflight", {"payload": {"target": "@group"}}).read()
        )
        reopened.clock = lambda: 1800000300
        self.assertIsNone(
            TargetChecks(reopened, a, "preflight", {"payload": {"target": "@group"}}).read()
        )

    def test_floodwait_survives_worker_restart_and_exact_deadline(self):
        _, a = self.fixture.ready()
        with self.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET platform='telegram',mode='live' WHERE id=?", (a["id"],))
        self.engine.apply_cooldown(a["id"], {"rate_limited": True, "retry_after_seconds": 120})
        reopened = Engine(
            Store(self.store.path),
            AdapterRegistry(self.fixture.temp.name),
            clock=lambda: 1800000119,
        )
        with patch.object(reopened, "_call_unlocked", return_value={"allowed": True}) as call:
            self.assertFalse(
                reopened.call(
                    reopened.account(a["id"]), "preflight", {"payload": {"target": "@group"}}
                )["allowed"]
            )
            call.assert_not_called()
            reopened.clock = lambda: 1800000120
            self.assertTrue(
                reopened.call(
                    reopened.account(a["id"]), "preflight", {"payload": {"target": "@group"}}
                )["allowed"]
            )
            self.assertEqual(call.call_count, 1)

    def test_missing_snapshot_field_fails_with_safe_consistency_error(self):
        self.fixture.ready()
        with self.store.connect(write=True) as db:
            row = db.execute("SELECT policy FROM tasks").fetchone()
            policy = json.loads(row[0])
            del policy["starts_at"]
            db.execute("UPDATE tasks SET policy=?", (canonical(policy),))
        with self.assertRaisesRegex(CloudUnavailable, "CloudSnapshotInconsistent"):
            export_snapshot(self.engine)

    def test_snapshot_time_boundaries_and_compatible_missing_optional_fields(self):
        self.fixture.ready()
        original = json.loads(self.store.one("SELECT policy FROM tasks")["policy"])
        invalid = [None, [], {k: v for k, v in original.items() if k != "ends_at"}]
        invalid += [
            {**original, **changes}
            for changes in (
                {"starts_at": None},
                {"ends_at": None},
                {"starts_at": False},
                {"ends_at": 1234},
                {"starts_at": "not-a-time"},
                {"starts_at": "2027-01-15T08:00:00"},
                {"ends_at": original["starts_at"]},
                {"starts_at": original["ends_at"], "ends_at": original["starts_at"]},
                {"timezone": "Synthetic/NotAZone"},
            )
        ]
        for policy in invalid:
            with self.subTest(policy=policy):
                with self.store.connect(write=True) as db:
                    db.execute("UPDATE tasks SET policy=?", (canonical(policy),))
                with self.assertRaisesRegex(CloudUnavailable, "CloudSnapshotInconsistent"):
                    export_snapshot(self.engine)
        old_policy = {
            k: v
            for k, v in original.items()
            if k
            not in {
                "timezone",
                "outbound_after_service_recovery_seconds",
                "quota_snapshot",
                "ordering_mode",
                "pacing_scope",
            }
        }
        with self.store.connect(write=True) as db:
            db.execute("UPDATE tasks SET policy=?", (canonical(old_policy),))
        snapshot = export_snapshot(self.engine)
        self.assertEqual(len(snapshot["tasks"]), 1)
        self.assertEqual(len(snapshot["jobs"]), 1)

    def test_actual_loopback_http_500_1101_and_quota_are_typed_and_do_not_retry(self):
        cases = [
            (500, {"error": "CloudRequestRejected"}, "CloudRequestRejected"),
            (500, {"error_code": 1101}, "CloudWorkerException"),
            (
                503,
                {"error": "CloudWriteQuotaExceeded", "retry_after_seconds": 3600},
                "CloudWriteQuotaExceeded",
            ),
        ]
        received = []
        emitted = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(handler):
                handler.rfile.read(int(handler.headers.get("Content-Length", "0")))
                received.append(handler.path)
                status, payload, _ = cases[(len(received) - 1) % len(cases)]
                data = json.dumps(payload).encode()
                emitted.append((status, payload))
                handler.send_response(status)
                handler.send_header("Content-Type", "application/json")
                handler.send_header("Content-Length", str(len(data)))
                handler.send_header("Connection", "close")
                handler.end_headers()
                handler.wfile.write(data)
                handler.wfile.flush()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            cloud = CloudControl(
                f"http://127.0.0.1:{server.server_port}",
                node_id="synthetic",
                node_token="synthetic-only",
            )
            for iteration in range(10):
                for status, payload, expected in cases:
                    with self.subTest(iteration=iteration, status=status, response=payload):
                        with self.assertRaisesRegex(CloudUnavailable, expected) as raised:
                            cloud.request("/v1/heartbeat", {})
                        self.assertEqual(
                            raised.exception.retry_after_seconds,
                            3600 if expected == "CloudWriteQuotaExceeded" else None,
                        )
            self.assertEqual(received, ["/v1/heartbeat"] * 30)
            self.assertEqual(emitted, [(s, p) for s, p, _ in cases] * 10)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_500_1101_and_quota_never_silently_enable_local_takeover(self):
        self.fixture.ready()
        for code in (
            "CloudTransportUnavailable",
            "CloudWorkerException",
            "CloudWriteQuotaExceeded",
        ):
            with self.subTest(code=code):
                cloud = CloudControl(
                    "http://127.0.0.1:8791", node_id="synthetic", node_token="synthetic-only"
                )
                with (
                    patch.object(cloud, "request", side_effect=CloudUnavailable(code)),
                    patch.object(CloudControl, "configured", return_value=cloud),
                ):
                    scheduler = PoolScheduler(
                        self.engine, popen=lambda *args, **kwargs: self.fail("unauthorized process")
                    )
                    scheduler.enable()
                    scheduler.tick()
                    self.assertFalse(
                        activate_local(self.engine, cloud, {"reason": code, "until": 1800003600})
                    )
                    self.assertEqual(self.store.one("SELECT COUNT(*) n FROM attempts")["n"], 0)
                with self.store.connect(write=True) as db:
                    db.execute("DELETE FROM meta WHERE key='cloud_retry_after'")
