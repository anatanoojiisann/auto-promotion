"""HTTP start retries must remain local, honest and once-only for a synthetic task."""

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from test_api import request

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.api import create_app
from auto_promotion.models import AccountInput, ItemInput, TaskInput


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class StartAPIIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1800000000.0
        self.app = create_app(
            db_path=Path(self.tmp.name) / "fixture.db",
            registry=AdapterRegistry(Path(self.tmp.name)),
            clock=lambda: self.now,
            token="fixture-token",
        )
        self.engine = self.app.state.engine
        self.account = self.engine.add_account(
            AccountInput(
                platform="telegram",
                label="Synthetic",
                backend_label="fixture-only",
                mode="simulation",
                min_interval=0,
            )
        )
        self.engine.check_account(self.account["id"])
        self.engine.set_stop(False)
        self.task = self.engine.create_task(
            TaskInput(
                name="HTTP fixture",
                account_ids=[self.account["id"]],
                policy={
                    "starts_at": iso(self.now - 10),
                    "ends_at": iso(self.now + 7200),
                    "min_interval": 0,
                },
            )
        )
        self.engine.set_items(
            self.task["id"],
            [
                ItemInput(
                    account_id=self.account["id"],
                    action="send_message",
                    target="@fixture",
                    text="Synthetic only",
                    scheduled_at=iso(self.now + 600),
                )
            ],
        )

    def start(self, auth=True):
        return asyncio.run(
            request(
                self.app,
                f"/api/tasks/{self.task['id']}/start",
                "POST",
                headers={"authorization": "Bearer fixture-token"} if auth else {},
            )
        )

    def test_repeated_http_start_keeps_schedule_and_does_not_probe_or_dispatch_again(self):
        self.assertEqual(self.start()[0], 200)
        before = self.engine.task(self.task["id"])
        with (
            patch.object(
                self.engine, "check_account", side_effect=AssertionError("no redundant health")
            ),
            patch.object(self.engine, "run_once", side_effect=AssertionError("no repeat dispatch")),
        ):
            status, result, _ = self.start()
        self.assertEqual(status, 200)
        self.assertTrue(result["execution_result"]["already_started"])
        self.assertEqual(result["readiness"]["workflow"]["status"], "waiting_schedule")
        self.assertEqual(result["manifest_hash"], before["manifest_hash"])
        self.assertEqual(result["items"], before["items"])
        self.assertEqual(self.engine.store.rows("SELECT * FROM attempts"), [])

    def test_repeated_http_start_reports_current_stop_without_resetting_it(self):
        self.start()
        self.engine.set_stop(True)
        status, result, _ = self.start()
        self.assertEqual(status, 200)
        self.assertEqual(result["readiness"]["workflow"]["status"], "global_stop")
        self.assertTrue(result["execution_result"]["already_started"])
        self.assertEqual(
            self.engine.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"], "1"
        )
        self.assertFalse(self.engine.store.rows("SELECT * FROM attempts"))

    def test_changed_account_configuration_is_not_hidden_by_already_started(self):
        self.start()
        with self.engine.store.connect(write=True) as db:
            db.execute(
                "UPDATE accounts SET daily_limit=daily_limit+1 WHERE id=?", (self.account["id"],)
            )
        self.assertEqual(self.start()[0], 409)
        self.assertFalse(self.engine.store.rows("SELECT * FROM attempts"))

    def test_authentication_and_terminal_state_are_not_hidden(self):
        self.assertEqual(self.start(auth=False)[0], 401)
        self.start()
        self.engine.change_state(self.task["id"], "cancelled")
        self.assertEqual(self.start()[0], 409)
        self.assertFalse(self.engine.store.rows("SELECT * FROM attempts"))
