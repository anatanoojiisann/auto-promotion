import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from auto_promotion.service_host import ServiceHost, imminent_work
from auto_promotion.store import Store
from auto_promotion.workers import Workers


class ServiceHostTests(unittest.TestCase):
    def setUp(self):
        test_root = Path(__file__).resolve().parents[1] / "var" / "service-tests"
        test_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=test_root)
        assert Path(self.temp.name).resolve().is_relative_to(test_root.resolve())
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "test.db")
        self.probe, self.occupied, self.launch = (
            Mock(return_value=None),
            Mock(return_value=False),
            Mock(),
        )
        self.child = Mock(pid=4321, returncode=None)
        self.child.poll.return_value = None
        self.launch.return_value = self.child
        self.process = Mock()
        self.sender = Mock(pid=1234)
        self.sender.poll.return_value = None
        self.process.return_value = self.sender
        self.host = ServiceHost(
            self.store,
            8780,
            logging.getLogger("test-service"),
            probe=self.probe,
            occupied=self.occupied,
            launch=self.launch,
            process=self.process,
        )

    def test_release_launch_uses_the_existing_business_working_directory(self):
        runtime = Path(self.temp.name)
        with patch.dict("os.environ", {"AP_SERVICE_PROJECT_DIR": str(runtime)}):
            with patch("auto_promotion.service_host.subprocess.Popen") as launch:
                self.host._launch()
                self.assertEqual(launch.call_args.kwargs["cwd"], runtime)
        for handle in self.host.log_handles:
            handle.close()

    def test_relative_service_working_directory_is_rejected(self):
        from auto_promotion.config import service_project_directory

        with patch.dict("os.environ", {"AP_SERVICE_PROJECT_DIR": "relative"}):
            with self.assertRaises(ValueError):
                service_project_directory()

    def test_starts_sender_once_when_absent(self):
        self.assertEqual(self.host.step()["state"], "starting")
        self.assertEqual(self.host.step()["state"], "starting_or_needs_attention")
        self.launch.assert_called_once()

    def test_existing_sender_is_adopted_without_duplicate_launch(self):
        Workers(self.store).start(pid=1234)
        self.probe.return_value = {"worker": True, "live_writes": False}
        self.assertEqual(self.host.step()["state"], "running")
        self.assertEqual(self.host.step()["state"], "running")
        self.launch.assert_not_called()
        self.process.assert_called_once_with(1234)

    def test_occupied_unhealthy_port_is_never_killed_or_replaced(self):
        self.occupied.return_value = True
        self.assertEqual(self.host.step()["state"], "listener_needs_attention")
        self.launch.assert_not_called()

    def test_observed_sender_exit_is_recorded_and_restarted_without_changing_stops(self):
        run = Workers(self.store).start(pid=1234, live_enabled=True)
        self.probe.return_value = {"worker": True, "live_writes": True}
        self.host.step()
        self.sender.poll.return_value = 17
        self.probe.return_value = None
        self.host.step()
        row = self.store.one("SELECT * FROM worker_runs WHERE id=?", (run,))
        self.assertEqual((row["state"], row["exit_code"]), ("failed", 17))
        self.launch.assert_called_once()
        self.assertEqual(
            self.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"], "1"
        )
        self.assertFalse(self.store.rows("SELECT * FROM attempts"))
        self.assertFalse(self.store.rows("SELECT * FROM calls"))

    def test_child_crash_is_logged_and_relaunched(self):
        self.host.step()
        self.child.returncode = 42
        self.child.poll.return_value = 42
        second = Mock(pid=4567, returncode=None)
        second.poll.return_value = None
        self.launch.return_value = second
        self.host.step()
        self.assertEqual(self.launch.call_count, 2)
        events = self.store.rows("SELECT details FROM audit WHERE kind='service.child_exited'")
        self.assertEqual(json.loads(events[0]["details"])["exit_code"], 42)

    def test_hung_sender_is_not_terminated_across_uncertain_submission(self):
        self.host.step()
        self.probe.return_value = None
        self.occupied.return_value = True
        self.host.step()
        self.child.terminate.assert_not_called()
        self.child.kill.assert_not_called()
        self.assertEqual(self.launch.call_count, 1)

    def test_service_status_contains_no_environment_or_item_text(self):
        self.host.step()
        status = json.loads(self.host.status_path.read_text())
        self.assertEqual(status["state"], "starting")
        self.assertFalse({"environment", "token", "cookie", "text"} & status.keys())

    def test_no_idle_sleep_request_when_no_pending_items(self):
        self.assertFalse(imminent_work(self.store, 1800000000))

    def test_sender_supervisor_preserves_explicit_phone_config_on_every_restart(self):
        config = Path(self.temp.name) / "private-phone-config.json"
        reddit_config = Path(self.temp.name) / "private-reddit-config.json"
        host = ServiceHost(
            self.store,
            8780,
            logging.getLogger("test-service-phone"),
            phone_social_config=config,
            phone_reddit_config=reddit_config,
            phone_rounds=True,
        )
        try:
            with patch("auto_promotion.service_host.subprocess.Popen") as popen:
                host._launch()
                argv = popen.call_args.args[0]
                self.assertEqual(argv[argv.index("--phone-social-config") + 1], str(config))
                self.assertEqual(argv[argv.index("--phone-reddit-config") + 1], str(reddit_config))
                self.assertIn("--phone-rounds", argv)
                self.assertIsNone(popen.call_args.kwargs["env"])
        finally:
            for handle in host.log_handles:
                handle.close()

    def test_reporting_supervisor_refuses_phone_execution(self):
        with self.assertRaises(ValueError):
            ServiceHost(
                self.store,
                8782,
                logging.getLogger("test-report"),
                reports_only=True,
                phone_social_config="private.json",
            )
        with self.assertRaises(ValueError):
            ServiceHost(
                self.store,
                8782,
                logging.getLogger("test-report"),
                reports_only=True,
                phone_reddit_config="private-reddit.json",
            )
        with self.assertRaises(ValueError):
            ServiceHost(
                self.store,
                8782,
                logging.getLogger("test-report"),
                reports_only=True,
                phone_rounds=True,
            )


if __name__ == "__main__":
    unittest.main()
