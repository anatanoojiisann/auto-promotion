"""Durable Telegram read pages, local settings and fair collection; no platform calls."""

import unittest
from pathlib import Path
from unittest.mock import Mock

import test_statistics as fixtures

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.core import Conflict, Engine
from auto_promotion.models import TelegramReadPolicy
from auto_promotion.telegram_reading import read_policy_context


class TelegramReadConsumerTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.StatisticsTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.account_id = self.f.item(published=self.f.now - 100)
        self.store = self.f.store
        self.provider = Mock()
        self.engine = Engine(
            self.store,
            AdapterRegistry(Path(self.store.path).parent, overrides={"telegram": self.provider}),
            clock=lambda: self.f.now,
        )

    def test_policy_config_is_local_and_revision_fenced_without_task_changes(self):
        before = self.store.rows("SELECT * FROM tasks")
        initial = self.engine.telegram_read_policy(self.account_id)
        self.assertEqual(initial["revision"], 0)
        policy = TelegramReadPolicy(requests_per_window=20)
        changed = self.engine.configure_telegram_reads(self.account_id, policy, expected_revision=0)
        self.assertEqual(changed["policy"]["requests_per_window"], 20)
        with self.assertRaises(Conflict):
            self.engine.configure_telegram_reads(self.account_id, policy, expected_revision=0)
        with self.assertRaises(Conflict):
            self.engine.configure_telegram_reads(self.account_id, policy)
        self.provider.call.assert_not_called()
        self.assertEqual(self.store.rows("SELECT * FROM tasks"), before)

    def test_backend_policy_context_reads_latest_config_without_default_fallback(self):
        context = {"database": self.store.path, "account_id": self.account_id}
        self.assertEqual(read_policy_context(context)["revision"], 0)
        self.engine.configure_telegram_reads(
            self.account_id, TelegramReadPolicy(requests_per_window=2)
        )
        self.assertEqual(read_policy_context(context)["policy"]["requests_per_window"], 2)
        with self.assertRaises(ValueError):
            read_policy_context({"database": self.store.path, "account_id": "missing"})

    def test_feedback_invalid_page_rolls_back_prior_events_and_cursor(self):
        self.provider.call.return_value = {
            "verified": True,
            "feedback_status": "complete",
            "feedback_checked_through": self.f.now,
            "feedback_cursor": 20,
            "read_page_complete": True,
            "feedback_events": [
                {"event_id": "10:10", "occurred_at": self.f.now - 10},
                {"event_id": "", "occurred_at": self.f.now - 5},
            ],
        }
        with self.assertRaises(ValueError):
            self.engine.collect_feedback(self.account_id)
        self.assertEqual(self.store.rows("SELECT * FROM feedback_events"), [])
        row = self.store.one("SELECT * FROM feedback_checks WHERE item_id=?", (self.account_id,))
        self.assertTrue(not row or row["last_message_id"] == 0)

    def test_cooldown_statistics_round_is_not_prematurely_marked_complete(self):
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE accounts SET cooldown_until=? WHERE id=?",
                (self.f.now + 100, self.account_id),
            )
        self.assertEqual(self.engine.collect_statistics()["processed"], [])
        self.assertIsNone(
            self.store.one("SELECT value FROM meta WHERE key='statistics_collection_slot'")
        )
        self.f.now += 200
        self.provider.call.return_value = {
            "verified": True,
            "feedback_events": [],
            "feedback_cursor": 0,
            "feedback_status": "complete",
            "feedback_checked_through": self.f.now,
            "read_page_complete": True,
        }
        self.assertEqual(len(self.engine.collect_statistics()["processed"]), 1)

    def test_content_page_and_cursor_commit_atomically_and_resume_after_restart(self):
        from auto_promotion.campaigns import Campaigns
        from auto_promotion.models import digest

        identity = digest(["telegram", "123"])
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE accounts SET identity_hash=? WHERE id=?", (identity, self.account_id)
            )
        page = {
            "read_identity_verified": True,
            "read_identity_hash": identity,
            "read_page_complete": True,
            "read_cursor_next": {"last": 10, "head": 20},
            "read_complete": False,
            "truncated": True,
            "content_items": [
                {
                    "platform": "telegram",
                    "external_id": "1:10",
                    "target": "@public_test",
                    "action": "reply",
                    "reply_to": 10,
                    "context": "AI video help",
                    "language": "en",
                    "confidence": 0.9,
                    "is_public": True,
                    "permission": "unknown",
                }
            ],
        }
        self.provider.call.return_value = page
        result = Campaigns(self.engine).collect_telegram_content(self.account_id, "@public_test")
        self.assertFalse(result["read_complete"])
        self.assertEqual(result["imported"], 1)
        engine = Engine(self.store, self.engine.adapters, clock=lambda: self.f.now)
        self.provider.call.return_value = {
            **page,
            "content_items": [],
            "read_complete": True,
            "read_cursor_next": {"last": 20, "head": 20},
        }
        Campaigns(engine).collect_telegram_content(self.account_id, "@public_test")
        self.assertEqual(
            self.provider.call.call_args.args[2]["read_cursor"], page["read_cursor_next"]
        )
        previous = self.store.rows("SELECT * FROM telegram_read_cursors")
        self.provider.call.return_value = {
            **page,
            "content_items": [page["content_items"][0], {"platform": "invalid"}],
        }
        with self.assertRaises(ValueError):
            Campaigns(engine).collect_telegram_content(self.account_id, "@public_test")
        self.assertEqual(self.store.rows("SELECT * FROM telegram_read_cursors"), previous)
        self.assertEqual(len(self.store.rows("SELECT * FROM opportunities")), 1)

    def test_statistics_counts_verification_before_feedback_and_defers_tail(self):
        from unittest.mock import patch

        self.engine.configure_telegram_reads(self.account_id, TelegramReadPolicy(scan_rpc_limit=5))
        with self.store.connect(write=True) as db:
            db.execute("UPDATE items SET status='unknown' WHERE id=?", (self.account_id,))

        def verify(item_id, **kwargs):
            self.assertEqual(kwargs["read_scan_rpc_limit"], 5)
            with self.store.connect(write=True) as db:
                db.execute("UPDATE items SET status='confirmed' WHERE id=?", (item_id,))
            return {"read_rpc_used": 4}

        def feedback(item_id, **kwargs):
            self.assertEqual(kwargs["read_scan_rpc_limit"], 1)
            return {"status": "partial", "read_complete": False, "read_rpc_used": 1}

        with (
            patch.object(self.engine, "reconcile", side_effect=verify),
            patch.object(self.engine, "collect_feedback", side_effect=feedback),
        ):
            result = self.engine.collect_statistics()
        self.assertEqual(result["read_rpc_used_by_account"][self.account_id], 5)
        self.assertFalse(result["complete"])

    def test_discovery_persists_pages_and_reuses_cursor_instead_of_first_group(self):
        from auto_promotion.models import Policy, TaskInput

        task = self.engine.create_task(
            TaskInput(
                name="read discovery",
                account_ids=[self.account_id],
                policy=Policy(
                    starts_at="2026-10-13T00:00:00+08:00", ends_at="2026-10-14T23:00:00+08:00"
                ),
            )
        )

        def page(account, operation, payload):
            index = payload.get("read_cursor", {}).get("index", 0)
            return {
                "candidates": [
                    {
                        "target": "@public_" + str(index),
                        "title": "AI video",
                        "text": "AI video",
                        "is_public": True,
                        "language": "en",
                        "confidence": 0.95,
                        "members": 1000,
                        "activity": 100,
                    }
                ],
                "read_page_complete": True,
                "read_complete": index == 1,
                "read_cursor_next": {"index": index + 1},
                "read_identity_verified": True,
                "read_identity_hash": "identity",
            }

        self.provider.call.side_effect = page
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE accounts SET identity_hash='identity' WHERE id=?", (self.account_id,)
            )
        first = self.engine.discover(task["id"], self.account_id)
        second = self.engine.discover(task["id"], self.account_id)
        self.assertFalse(first["read_complete"])
        self.assertTrue(second["read_complete"])
        self.assertEqual(
            {row["target"] for row in self.store.rows("SELECT target FROM candidates")},
            {"@public_0", "@public_1"},
        )

    def test_real_backend_native_feedback_persists_string_peer_identity_and_cursor(self):
        import json
        import time
        import types
        from unittest.mock import patch

        import test_native_backends

        from auto_promotion import native_bridge
        from auto_promotion.backends.common import save_profile
        from auto_promotion.backends.telegram import TelegramBackend
        from auto_promotion.models import canonical, digest

        f = test_native_backends.TelegramTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        save_profile("telegram", "1", {"identity": "42", "api_id": 1, "api_hash": "synthetic"})
        client = test_native_backends.FakeTelegram()
        client.messages[123] = types.SimpleNamespace(
            id=123, out=True, message="approved", media=None
        )
        self.f.now = time.time() + 5
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE accounts SET identity_hash=? WHERE id=?",
                (digest(["telegram", "42"]), self.account_id),
            )
            db.execute(
                "UPDATE items SET payload=?,result=? WHERE id=?",
                (
                    canonical({"target": "@public_group", "text": "approved", "media": []}),
                    canonical({"message_ids": [123], "status": "confirmed", "verified": True}),
                    self.account_id,
                ),
            )

        def native(account, operation, payload):
            return native_bridge.dispatch(
                {
                    "platform": "telegram",
                    "account": "1",
                    "operation": operation,
                    "account_id": account["id"],
                    "database": self.store.path,
                    "payload": payload,
                }
            )

        def factory(label):
            return TelegramBackend(label, client_factory=lambda *args: client)

        with (
            patch.object(self.engine.adapters, "call", side_effect=native),
            patch("auto_promotion.backends.telegram.TelegramBackend", side_effect=factory),
        ):
            self.assertEqual(self.engine.collect_feedback(self.account_id)["status"], "complete")
        binding = json.loads(
            self.store.one(
                "SELECT cursor FROM telegram_read_cursors WHERE identity_key='feedback'"
            )["cursor"]
        )
        self.assertEqual(binding["peer_id"], "-1000000000010")
        self.assertEqual(binding["identity_hash"], digest(["telegram", "42"]))
        self.assertEqual(binding["cursor"], 123)
        self.assertEqual(client.sends, [])

    def test_real_content_route_consumes_all_pages_and_incremental_backlog_after_restart(self):
        from unittest.mock import patch

        import test_telegram_incremental_reads as backend_fixtures

        from auto_promotion import native_bridge
        from auto_promotion.backends.telegram import TelegramBackend
        from auto_promotion.campaigns import Campaigns

        f = backend_fixtures.TelegramIncrementalReadTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        with self.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET backend_label='test' WHERE id=?", (self.account_id,))
        self.engine.configure_telegram_reads(
            self.account_id, TelegramReadPolicy(messages_per_page=5)
        )

        def native(account, operation, payload):
            return native_bridge.dispatch(
                {
                    "platform": "telegram",
                    "account": "test",
                    "operation": operation,
                    "account_id": account["id"],
                    "database": self.store.path,
                    "payload": payload,
                }
            )

        def factory(label):
            return TelegramBackend(label, client_factory=lambda *args: f.client)

        with (
            patch.object(self.engine.adapters, "call", side_effect=native),
            patch("auto_promotion.backends.telegram.TelegramBackend", side_effect=factory),
        ):
            first = Campaigns(self.engine).collect_telegram_content(
                self.account_id, "@public_group", 12
            )
            self.assertFalse(first["read_complete"])
            # New Engine after every page uses only durable consumer state.
            for _ in range(2):
                engine = Engine(self.store, self.engine.adapters, clock=lambda: self.f.now)
                final = Campaigns(engine).collect_telegram_content(
                    self.account_id, "@public_group", 12
                )
            self.assertTrue(final["read_complete"])
            f.client.history = backend_fixtures.messages(50)
            for _ in range(4):
                Campaigns(self.engine).collect_telegram_content(
                    self.account_id, "@public_group", 12
                )
        ids = {
            int(__import__("json").loads(row["payload"])["external_id"])
            for row in self.store.rows("SELECT payload FROM opportunities")
        }
        self.assertEqual(ids, set(range(19, 51)))
        self.assertEqual(f.client.sends, [])

    def test_expired_lane_cannot_commit_content_page_or_overwrite_new_worker_cursor(self):
        from auto_promotion.campaigns import Campaigns
        from auto_promotion.health_checks import probe_revision
        from auto_promotion.models import canonical
        from auto_promotion.telegram_reading import content_scope

        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE accounts SET identity_hash='identity' WHERE id=?", (self.account_id,)
            )
        account = self.engine.account(self.account_id)
        key, scope = content_scope(
            account, probe_revision(account, self.store.path), "@public_test", 200
        )

        def late_page(*args):
            with self.store.connect(write=True) as db:
                db.execute(
                    "UPDATE account_lanes SET owner='new-worker' WHERE account_id=?",
                    (self.account_id,),
                )
                db.execute(
                    "INSERT INTO telegram_read_cursors VALUES(?,?,?,?,1,?)",
                    (
                        self.account_id,
                        key,
                        scope,
                        canonical({"identity_hash": "identity", "cursor": {"last": 50}}),
                        self.f.now,
                    ),
                )
            return {
                "content_items": [],
                "read_identity_verified": True,
                "read_identity_hash": "identity",
                "read_page_complete": True,
                "read_cursor_next": {"last": 10},
                "read_complete": True,
            }

        self.provider.call.side_effect = late_page
        with self.assertRaises(Conflict):
            Campaigns(self.engine).collect_telegram_content(self.account_id, "@public_test")
        self.assertEqual(
            __import__("json").loads(
                self.store.one("SELECT cursor FROM telegram_read_cursors")["cursor"]
            )["cursor"],
            {"last": 50},
        )

    def test_error_read_counts_survive_native_and_account_diagnostics_export(self):
        import json
        from unittest.mock import patch

        from auto_promotion import native_bridge
        from auto_promotion.backends.common import BackendError
        from auto_promotion.task_tools import TaskTools

        error = BackendError("SyntheticReadFailure")
        error.rpc_counts = {"history": 2, "identity": 2}
        error.rpc_count_source = "telethon_logical_call_attempt"
        error.read_diagnostics = {
            "read_rpc_used": 4,
            "read_rpc_wire_attempts": None,
            "read_state": "incomplete",
        }

        def native(account, operation, payload):
            return native_bridge.dispatch(
                {
                    "platform": "telegram",
                    "account": account["backend_label"],
                    "operation": operation,
                    "account_id": account["id"],
                    "database": self.store.path,
                    "payload": payload,
                }
            )

        with (
            patch.object(self.engine.adapters, "call", side_effect=native),
            patch("auto_promotion.backends.telegram.call", side_effect=error),
        ):
            with self.assertRaises(Conflict):
                self.engine.collect_feedback(self.account_id)
        observation = json.loads(
            self.store.one("SELECT result FROM telegram_read_observations")["result"]
        )
        self.assertEqual(observation["read_rpc_used"], 4)
        self.assertIsNone(observation["read_rpc_wire_attempts"])
        self.assertEqual(observation["rpc_counts"]["history"], 2)
        evidence = TaskTools(self.engine).evidence(self.account_id)
        self.assertEqual(evidence["telegram_read_observations"][0]["result"]["read_rpc_used"], 4)

    def test_statistics_round_is_leased_and_tail_resumes_in_new_engine(self):
        from threading import Event, Thread
        from unittest.mock import patch

        second = self.f.item(published=self.f.now - 100)
        entered, release = Event(), Event()

        def slow_feedback(item_id, **kwargs):
            entered.set()
            release.wait(5)
            return {"status": "complete", "read_complete": True, "read_rpc_used": 1}

        results = []
        with patch.object(self.engine, "collect_feedback", side_effect=slow_feedback):
            worker = Thread(
                target=lambda: results.append(self.engine.collect_statistics(max_items=1))
            )
            worker.start()
            self.assertTrue(entered.wait(5))
            other = Engine(self.store, self.engine.adapters, clock=lambda: self.f.now)
            self.assertEqual(other.collect_statistics()["reason"], "statistics_collection_busy")
            release.set()
            worker.join(5)
        self.assertFalse(results[0]["complete"])
        with patch.object(
            other,
            "collect_feedback",
            return_value={"status": "complete", "read_complete": True, "read_rpc_used": 1},
        ) as feedback:
            last = other.collect_statistics(max_items=1)
        self.assertTrue(last["complete"])
        self.assertEqual(feedback.call_args.args[0], second)

    def test_growth_feed_round_shares_cap_and_rotates_untouched_tail(self):
        from unittest.mock import patch

        from auto_promotion.growth import Growth
        from auto_promotion.growth_models import FeedInput

        self.engine.configure_telegram_reads(self.account_id, TelegramReadPolicy(scan_rpc_limit=5))
        growth = Growth(self.engine)
        for n in range(3):
            growth.save_feed(
                FeedInput(account_id=self.account_id, target="@public_" + str(n), enabled=True)
            )

        def read(account_id, target, limit, *, read_scan_rpc_limit):
            return {"read_complete": True, "read_rpc_used": min(4, read_scan_rpc_limit)}

        with patch(
            "auto_promotion.campaigns.Campaigns.collect_telegram_content", side_effect=read
        ) as provider:
            first = growth.poll_feeds()
            self.assertEqual(provider.call_count, 2)
            self.assertEqual(
                [call.kwargs["read_scan_rpc_limit"] for call in provider.call_args_list], [5, 1]
            )
            self.assertEqual(first["read_rpc_used_by_account"][self.account_id], 5)
            self.assertEqual(growth.poll_feeds()["completed"], 1)
        self.assertTrue(all(feed["status"] == "complete" for feed in growth.feeds()))


class TelegramReadReceiptTests(unittest.TestCase):
    def test_transport_read_settings_do_not_change_immutable_execution_claim(self):
        import sqlite3
        from unittest.mock import patch

        import test_private_receipt_recovery as receipts

        from auto_promotion import final_receipts, native_bridge

        f = receipts.PrivateReceiptRecoveryTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        claims = []

        def adapter(account, operation, payload):
            claims.append(dict(payload))
            return native_bridge.dispatch({**f.request, "payload": payload})

        with (
            patch("auto_promotion.backends.telegram.call", f.submit()),
            patch.object(f.engine.adapters, "call", side_effect=adapter),
            f.engine.adapters.execution_job(f.context),
        ):
            result = f.engine.call(f.account, "execute", f.claim, f.task_id)
        self.assertIsNotNone(final_receipts.proof(f.store, f.store.one("SELECT * FROM items")))
        self.assertNotIn("read_policy", claims[0])
        with patch.object(
            f.engine, "finish", side_effect=sqlite3.OperationalError("synthetic lock")
        ):
            with self.assertRaises(sqlite3.OperationalError):
                f.engine.finish(f.claim, result)
        f.expire(paused=True)
        f.engine.recover()
        f.assert_once("confirmed")
