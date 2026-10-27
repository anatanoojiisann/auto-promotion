import asyncio
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.api import create_app
from auto_promotion.backends.browser import BrowserBackend
from auto_promotion.core import Engine
from auto_promotion.models import AccountInput
from auto_promotion.store import Store
from auto_promotion.workers import WorkerRun, Workers, supervise


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "tasks.db")
        self.now = 1800000000.0
        self.workers = Workers(self.store, lambda: self.now)

    def test_stale_heartbeat_does_not_change_task_or_enable_writes(self):
        run_id = self.workers.start(live_enabled=True)
        self.assertEqual(self.workers.snapshot(live=True)["status"], "running")
        self.now += 61
        snapshot = self.workers.snapshot(live=True)
        self.assertEqual((snapshot["status"], snapshot["reason"]), ("stale", "heartbeat_expired"))
        self.assertEqual(
            self.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"], "1"
        )
        self.workers.finish(run_id, "process_exit", 17)
        self.assertEqual(self.workers.snapshot(live=True)["status"], "stopped")

    def test_simulation_sender_cannot_claim_live_execution_is_running(self):
        self.workers.start(live_enabled=False)
        self.assertEqual(self.workers.snapshot()["status"], "running")
        self.assertEqual(self.workers.snapshot(live=True)["status"], "unmonitored")

    def test_independent_heartbeat_keeps_up_during_blocking_execution(self):
        updated = threading.Event()
        original = Workers.heartbeat

        def heartbeat(workers, *args, **kwargs):
            original(workers, *args, **kwargs)
            if threading.current_thread().name == "sender-heartbeat":
                updated.set()

        with (
            patch("auto_promotion.workers.HEARTBEAT_INTERVAL", 0.02),
            patch.object(Workers, "heartbeat", heartbeat),
        ):
            with WorkerRun(self.store) as run:
                run.phase("executing")
                self.assertTrue(updated.wait(timeout=2))
                self.assertEqual(Workers(self.store).snapshot()["phase"], "executing")
        self.assertEqual(Workers(self.store).snapshot()["status"], "stopped")

    def test_supervisor_records_abrupt_exit_that_bypasses_finally(self):
        code = supervise(
            self.store, None, [sys.executable, "-c", "import os; os._exit(17)"], live_enabled=True
        )
        snapshot = Workers(self.store).snapshot(live=True)
        self.assertEqual(
            (code, snapshot["status"], snapshot["exit_code"], snapshot["reason"]),
            (17, "stopped", 17, "process_exit"),
        )
        self.assertFalse(self.store.rows("SELECT * FROM calls"))
        self.assertEqual(
            self.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"], "1"
        )

    def test_graceful_child_reason_is_preserved_with_actual_exit_code(self):
        source = str(Path(__file__).resolve().parents[1] / "src")
        program = (
            "import os; from auto_promotion.store import Store; from auto_promotion.workers import WorkerRun; "
            f"run=WorkerRun(Store({self.store.path!r}), run_id=os.environ['AP_WORKER_RUN_ID']); "
            "run.__enter__(); run.reason='all_items_terminal'; run.__exit__(None,None,None)"
        )
        with patch.dict(os.environ, {"PYTHONPATH": source}):
            self.assertEqual(supervise(self.store, None, [sys.executable, "-c", program]), 0)
        self.assertEqual(Workers(self.store).snapshot()["reason"], "all_items_terminal")

    def test_backend_returning_unknown_is_logged_with_outcome_and_step(self):
        registry = AdapterRegistry(Path(self.tmp.name))
        engine = Engine(self.store, registry)
        account = engine.add_account(
            AccountInput(platform="x", label="local", backend_label="local", mode="simulation")
        )
        with patch.object(
            registry,
            "call",
            return_value={
                "status": "unknown",
                "reason": "TimeoutError",
                "execution_step": "submission_response",
            },
        ):
            engine.call(account, "execute")
        row = self.store.rows("SELECT * FROM calls")[-1]
        self.assertEqual(
            (row["status"], row["error_code"], row["execution_step"]),
            ("unknown", "TimeoutError", "submission_response"),
        )

    def test_browser_failure_before_submission_records_exact_step(self):
        page = Mock()
        page.url = "https://x.com/home"
        page.locator.return_value.count.return_value = 0
        page.goto.side_effect = [None, TimeoutError("must-not-leak-cookie")]
        backend = BrowserBackend(
            "x", "local", SimpleNamespace(pages=[page]), page, {"identity": "brand"}
        )
        with patch.object(backend, "check_identity"):
            result = backend.run(
                "execute",
                {"payload": {"target": "https://x.com/a/status/1", "action": "reply"}},
                Mock(),
                Mock(),
            )
        self.assertEqual(result["execution_step"], "target_navigation")
        self.assertFalse(result["submission_started"])
        self.assertEqual(result["status"], "failed_known")
        self.assertTrue(result["confirmed_not_submitted"])
        self.assertEqual(result["failure_class"], "page_anomaly")
        self.assertNotIn("must-not-leak-cookie", str(result))

    def test_api_sender_lifecycle_waits_for_inflight_operation(self):
        app = create_app(
            db_path=self.store.path,
            registry=AdapterRegistry(Path(self.tmp.name)),
            token="test",
            worker=True,
            report_worker=False,
        )
        started, release = threading.Event(), threading.Event()

        def cycle():
            started.set()
            release.wait(timeout=3)

        async def exercise():
            with patch.object(app.state.engine, "run_cycle", cycle):
                async with app.router.lifespan_context(app):
                    self.assertTrue(await asyncio.to_thread(started.wait, 2))
                    self.assertEqual(Workers(self.store).snapshot()["status"], "running")
                    release.set()
            self.assertEqual(Workers(self.store).snapshot()["reason"], "service_shutdown")

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
