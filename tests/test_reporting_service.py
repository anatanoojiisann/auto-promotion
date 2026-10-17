import asyncio
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import URLError

from test_api import request

from auto_promotion.api import create_app
from auto_promotion.service_host import ServiceHost, read_health
from auto_promotion.store import Store


class ReportingServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "report.db"

    def test_reports_only_blocks_mutations_even_with_sender_environment_enabled(self):
        with patch.dict(os.environ, {"AUTO_PROMOTION_LIVE_WRITES": "1"}):
            app = create_app(db_path=self.db, worker=True, statistics_only=True, token="test")
        status, health, _ = asyncio.run(request(app, "/health"))
        self.assertEqual(status, 200)
        self.assertEqual(
            (health["worker"], health["live_writes"], health["report_worker"]), (False, False, True)
        )
        self.assertTrue(health["statistics_only"])
        for path in ("/api/stop", "/api/statistics/run", "/api/tasks", "/api/items/one/feedback"):
            with self.subTest(path=path):
                status, body, _ = asyncio.run(
                    request(app, path, "POST", {}, {"authorization": "Bearer test"})
                )
                self.assertEqual((status, body["error"]), (403, "statistics_service_read_only"))
        status, body, _ = asyncio.run(
            request(
                app, "/api/statistics/feedback-issues", headers={"authorization": "Bearer test"}
            )
        )
        self.assertEqual((status, body), (200, {"issues": []}))

    def test_reports_only_starts_only_statistics_and_reports(self):
        app = create_app(db_path=self.db, statistics_only=True, token="test")
        with (
            patch.object(app.state.engine, "collect_statistics", return_value={}) as collect,
            patch.object(app.state.statistics, "run_due", return_value={}) as reports,
            patch("auto_promotion.api.Growth.sync_inbox") as inbox,
            patch("auto_promotion.api.Growth.poll_feeds") as feeds,
            patch("auto_promotion.api.TikTokManager.reconcile_due") as tiktok,
            patch("auto_promotion.api.WorkerRun") as sender,
        ):

            async def run():
                async with app.router.lifespan_context(app):
                    await asyncio.sleep(0.05)

            asyncio.run(run())
        collect.assert_called_once()
        reports.assert_called_once()
        for blocked in (inbox, feeds, tiktok, sender):
            blocked.assert_not_called()
        self.assertEqual(
            app.state.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"], "1"
        )

    def test_access_denied_is_distinct_from_absent_listener(self):
        with patch(
            "auto_promotion.service_host.urllib.request.urlopen",
            side_effect=URLError(PermissionError(13, "private detail")),
        ):
            self.assertEqual(read_health(8782)["probe_error"], "local_access_denied")
        with patch(
            "auto_promotion.service_host.urllib.request.urlopen",
            side_effect=URLError(ConnectionRefusedError()),
        ):
            self.assertIsNone(read_health(8782))

    def host(self, health=None, occupied=False):
        launch = Mock()
        host = ServiceHost(
            Store(self.db),
            8782,
            logging.getLogger("test-reports"),
            reports_only=True,
            probe=Mock(return_value=health),
            occupied=Mock(return_value=occupied),
            launch=launch,
        )
        return host, launch

    def test_reports_host_adopts_only_safe_statistics_service(self):
        host, launch = self.host(
            {"worker": False, "live_writes": False, "report_worker": True, "statistics_only": True}
        )
        self.assertEqual(host.step()["state"], "running")
        launch.assert_not_called()
        self.assertFalse(host.store.rows("SELECT * FROM worker_runs"))

    def test_reports_host_does_not_replace_denied_or_occupied_port(self):
        for health, occupied, state in (
            ({"probe_error": "local_access_denied"}, False, "probe_needs_attention"),
            ({"worker": True, "live_writes": True}, True, "listener_needs_attention"),
        ):
            host, launch = self.host(health, occupied)
            self.assertEqual(host.step()["state"], state)
            launch.assert_not_called()

    def test_reporting_child_forces_writes_off_and_has_no_sender_flags(self):
        host, _ = self.host()
        with (
            patch.dict(os.environ, {"AUTO_PROMOTION_LIVE_WRITES": "1"}),
            patch("auto_promotion.service_host.subprocess.Popen") as popen,
        ):
            try:
                host._launch()
                argv = popen.call_args.args[0]
                self.assertIn("--reports-only", argv)
                self.assertEqual(popen.call_args.kwargs["env"]["AUTO_PROMOTION_LIVE_WRITES"], "0")
                self.assertEqual(os.environ["AUTO_PROMOTION_LIVE_WRITES"], "1")
            finally:
                for handle in host.log_handles:
                    handle.close()


if __name__ == "__main__":
    unittest.main()
