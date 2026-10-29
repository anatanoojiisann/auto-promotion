import asyncio
import json
import os
import sqlite3
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime, timezone
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

from auto_promotion import native_bridge
from auto_promotion.adapters import AdapterRegistry, SimulationAdapter
from auto_promotion.backends.common import (
    BackendError,
    Receipts,
    account_lock,
    profile_path,
    read_profile,
    save_profile,
    verify_claim,
)
from auto_promotion.core import Engine
from auto_promotion.models import AccountInput, ItemInput, TaskInput
from auto_promotion.store import Store


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class PrivateBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.environment = patch.dict(os.environ, {"AP_ACCOUNTS_DIR": self.tmp.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_labels_cannot_escape_profile_directory(self):
        path = profile_path("telegram", "../../private/账户")
        self.assertTrue(path.is_relative_to(Path(self.tmp.name)))
        self.assertEqual(path.parent.name, "telegram")

    def test_changed_login_identity_requires_new_label(self):
        save_profile("telegram", "default", {"identity": "42"})
        with self.assertRaises(BackendError):
            save_profile("telegram", "default", {"identity": "43"})
        self.assertEqual(read_profile("telegram", "default")["identity"], "42")

    def test_process_lock_releases_and_prevents_concurrent_session(self):
        with account_lock("telegram", "default"):
            with self.assertRaises(BackendError):
                with account_lock("telegram", "default"):
                    self.fail("Second process lock was acquired")
        with account_lock("telegram", "default"):
            pass

    def test_receipt_survives_restart_and_rejects_payload_change(self):
        receipts = Receipts("telegram", "default")
        self.assertIsNone(receipts.begin("execute:one", {"text": "exact"}))
        receipts.save("execute:one", {"status": "unknown", "message_ids": [123]})
        restarted = Receipts("telegram", "default")
        self.assertEqual(restarted.begin("execute:one", {"text": "exact"})["message_ids"], [123])
        with self.assertRaises(BackendError):
            restarted.begin("execute:one", {"text": "changed"})

    def test_builtin_catalog_does_not_require_sibling_checkouts(self):
        registry = AdapterRegistry(Path(self.tmp.name) / "missing-workspace")
        self.assertEqual(len(registry.services()), 7)
        self.assertTrue(all(service["installed"] for service in registry.services()))

    def test_native_health_without_configuration_is_explicitly_unavailable(self):
        result = native_bridge.dispatch(
            {"platform": "telegram", "account": "not-configured", "operation": "health"}
        )
        self.assertFalse(result["healthy"])
        self.assertEqual(result["reason"], "AccountNotConfigured")

    def test_no_write_without_commissioning(self):
        with patch.dict(os.environ, {"AUTO_PROMOTION_LIVE_WRITES": "0"}):
            with self.assertRaises(BackendError):
                native_bridge.dispatch(
                    {"platform": "telegram", "account": "default", "operation": "execute"}
                )

    def test_accepted_receipt_survives_verification_failure_and_does_not_resend(self):
        calls = []

        def submit(label, operation, payload, checkpoint, authorize):
            calls.append(operation)
            checkpoint(
                {
                    "status": "unknown",
                    "message_ids": [123],
                    "external_reference": "telegram:@group:123",
                }
            )
            checkpoint.progress("receipt_lookup", True)
            raise RuntimeError("secret-token-must-not-leak")

        request = {
            "platform": "telegram",
            "account": "default",
            "operation": "execute",
            "payload": {"item_id": "one", "payload": {"text": "exact"}},
        }
        with (
            patch.object(native_bridge, "verify_claim"),
            patch("auto_promotion.backends.telegram.call", submit),
        ):
            first = native_bridge.dispatch(request)
            second = native_bridge.dispatch(request)
        self.assertEqual(calls, ["execute"])
        self.assertEqual(first, second)
        self.assertEqual(first["message_ids"], [123])
        self.assertEqual(first["execution_step"], "receipt_lookup")
        self.assertTrue(first["submission_started"])
        self.assertTrue(first["diagnostic_traceback"])
        self.assertNotIn("secret-token", json.dumps(first))

    def test_timeout_without_receipt_records_step_and_never_resends(self):
        calls = []

        def submit(platform, label, operation, payload, checkpoint, authorize):
            calls.append(operation)
            checkpoint.progress("submission_response", True)
            raise TimeoutError("cookie=secret-token-must-not-leak")

        request = {
            "platform": "x",
            "account": "local-x",
            "operation": "execute",
            "payload": {"item_id": "unknown", "payload": {"text": "exact"}},
        }
        with (
            patch.object(native_bridge, "verify_claim"),
            patch("auto_promotion.backends.browser.call", submit),
        ):
            first = native_bridge.dispatch(request)
            second = native_bridge.dispatch(request)
        self.assertEqual(calls, ["execute"])
        self.assertEqual(first, second)
        self.assertEqual(
            (first["status"], first["reason"], first["execution_step"]),
            ("unknown", "TimeoutError", "submission_response"),
        )
        self.assertTrue(first["submission_started"])
        self.assertNotIn("external_reference", first)
        self.assertNotIn("secret-token", json.dumps(first))


class ClaimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(
            os.environ,
            {
                "AUTO_PROMOTION_LIVE_WRITES": "1",
                "AP_TELEGRAM_OWNER": "auto-promotion",
                "AP_ACCOUNTS_DIR": str(Path(self.tmp.name) / "accounts"),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = Store(Path(self.tmp.name) / "db.sqlite")
        registry = AdapterRegistry(Path(self.tmp.name), overrides={"telegram": SimulationAdapter()})
        self.engine = Engine(self.store, registry, live_enabled=True)
        self.account = self.engine.add_account(
            AccountInput(platform="telegram", label="test", backend_label="default")
        )
        self.engine.check_account(self.account["id"])
        now = time.time()
        task = self.engine.create_task(
            TaskInput(
                name="Test",
                account_ids=[self.account["id"]],
                policy={"starts_at": iso(now - 60), "ends_at": iso(now + 3600)},
            )
        )
        self.engine.start_task(task["id"], sending=False)
        self.engine.set_items(
            task["id"],
            [
                ItemInput(
                    account_id=self.account["id"],
                    action="send_message",
                    target="@public_group",
                    text="exact",
                    scheduled_at=iso(now - 1),
                )
            ],
        )
        task = self.engine.prepare(task["id"])
        self.engine.start_task(task["id"])
        self.engine.set_stop(False)
        item = self.engine.task(task["id"])["items"][0]
        claim = self.engine._claim(item["id"], {"allowed": True})
        self.assertIsNotNone(claim)
        self.request = {
            "platform": "telegram",
            "account": "default",
            "account_id": self.account["id"],
            "database": self.store.path,
            "operation": "execute",
            "payload": claim,
        }

    def test_current_claim_and_explicit_start_are_required(self):
        verify_claim(self.request)
        with self.store.connect(write=True) as db:
            db.execute("UPDATE tasks SET phase='review'")
        with self.assertRaises(BackendError):
            verify_claim(self.request)

    def test_existing_sender_can_verify_before_campaign_schema_migration(self):
        with self.store.connect(write=True) as db:
            db.execute("DROP INDEX tasks_campaign")
            db.execute("ALTER TABLE tasks DROP COLUMN campaign_id")
        verify_claim(self.request)

    def test_stop_rechecked_inside_provider(self):
        self.engine.set_stop(True)
        with self.assertRaises(BackendError):
            verify_claim(self.request)

    def test_local_submit_check_survives_an_unrelated_database_writer(self):
        from auto_promotion.execution_channels import binding

        now = time.time()
        context = {
            "id": "send:" + self.request["payload"]["item_id"],
            "owner": "local-test",
            "channel": "local",
            "generation": "test-generation",
            "store_id": binding(self.engine),
            "node_id": "test-node",
        }
        state = {
            **context,
            "active": "local",
            "authorization": "local_queue",
            "reason": "explicit_user_local",
            "until": now + 3600,
            "account_ids": [self.account["id"]],
        }
        task_id = self.request["payload"]["task_id"]
        with self.store.connect(write=True) as db:
            for key, value in (
                ("execution_channel_policy", "cloud_then_local"),
                ("local_channel_authority", json.dumps({"account_ids": state["account_ids"]})),
                ("execution_channel", json.dumps(state)),
            ):
                db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, value))
            db.execute(
                "INSERT INTO execution_jobs(id,kind,reference_id,task_id,account_id,pool,route,"
                "version,available_at,expires_at,state,owner,lease_until,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    context["id"],
                    "send",
                    self.request["payload"]["item_id"],
                    task_id,
                    self.account["id"],
                    "telegram.mtproto",
                    "{}",
                    "test",
                    now - 1,
                    now + 900,
                    "leased",
                    context["owner"],
                    now + 90,
                    now,
                    now,
                ),
            )
        self.request["execution_job"] = context
        # A WAL reader must not need the schema writer lock just to reauthorize.
        with patch.dict(os.environ, {"AP_NODE_ID": "test-node"}):
            writer = sqlite3.connect(self.store.path)
            try:
                writer.execute("BEGIN IMMEDIATE")
                verify_claim(self.request)
            finally:
                writer.close()
            self.engine.set_stop(True)
            with self.assertRaisesRegex(BackendError, "ExecutionStopped"):
                verify_claim(self.request)

    def test_existing_store_check_never_creates_a_missing_database(self):
        missing = Path(self.tmp.name) / "missing.sqlite"
        with self.assertRaises(FileNotFoundError):
            Store(missing, initialize=False)
        self.assertFalse(missing.exists())

    def test_progress_waits_for_writer_and_never_replays_submission(self):
        locked, release = threading.Event(), threading.Event()
        submissions = []

        def hold_writer():
            writer = sqlite3.connect(self.store.path)
            try:
                writer.execute("BEGIN IMMEDIATE")
                locked.set()
                release.wait(3)
            finally:
                writer.close()

        def submit(label, operation, payload, checkpoint, authorize):
            thread = threading.Thread(target=hold_writer)
            thread.start()
            self.addCleanup(thread.join)
            self.addCleanup(release.set)
            self.assertTrue(locked.wait(2))
            timer = threading.Timer(0.1, release.set)
            timer.start()
            self.addCleanup(timer.join)
            checkpoint.progress("submit_click", True)
            observed = self.store.one("SELECT result FROM items WHERE id=?", (payload["item_id"],))
            self.assertTrue(json.loads(observed["result"])["submission_started"])
            submissions.append(True)
            result = {
                "status": "confirmed",
                "verified": True,
                "external_reference": "telegram:@public_group:123",
            }
            checkpoint(result)
            return result

        with patch("auto_promotion.backends.telegram.call", submit):
            first = native_bridge.dispatch(self.request)
            second = native_bridge.dispatch(self.request)
        self.assertEqual(submissions, [True])
        self.assertEqual(first, second)
        self.assertTrue(first["verified"])

    def test_busy_progress_mirror_keeps_durable_marker_and_submits_once(self):
        locked, release = threading.Event(), threading.Event()
        submissions = []

        def holder():
            writer = sqlite3.connect(self.store.path)
            try:
                writer.execute("BEGIN IMMEDIATE")
                locked.set()
                release.wait(10)
            finally:
                writer.close()

        def submit(label, operation, payload, checkpoint, authorize):
            thread = threading.Thread(target=holder)
            thread.start()
            self.addCleanup(thread.join)
            self.addCleanup(release.set)
            self.assertTrue(locked.wait(2))
            started = time.monotonic()
            checkpoint.progress("submit_click", True)
            self.assertLess(time.monotonic() - started, 2)
            saved = Receipts("telegram", "default").read("execute:" + payload["item_id"])
            self.assertTrue(saved["submission_started"])
            self.assertTrue(saved["progress_mirror_deferred"])
            authorize()
            submissions.append(True)
            release.set()
            thread.join(2)
            result = {
                "status": "confirmed",
                "verified": True,
                "external_reference": "telegram:@public_group:123",
            }
            checkpoint(result)
            return result

        with patch("auto_promotion.backends.telegram.call", submit):
            first = native_bridge.dispatch(self.request)
            second = native_bridge.dispatch(self.request)
        self.assertEqual(submissions, [True])
        self.assertEqual(first, second)

    def test_private_marker_failure_still_prevents_submission(self):
        submissions = []
        original = Receipts.save
        failed = []

        def save(receipts, key, result):
            if result.get("execution_step") == "submit_click" and not failed:
                failed.append(True)
                raise sqlite3.OperationalError("database is locked")
            return original(receipts, key, result)

        def submit(label, operation, payload, checkpoint, authorize):
            checkpoint.progress("submit_click", True)
            submissions.append(True)
            return {"status": "confirmed", "verified": True}

        with (
            patch.object(Receipts, "save", save),
            patch("auto_promotion.backends.telegram.call", submit),
        ):
            result = native_bridge.dispatch(self.request)
        self.assertEqual(submissions, [])
        self.assertEqual(result["status"], "unknown")

    def test_accepted_and_final_mirrors_locked_recover_from_private_receipt_once(self):
        locked, release = threading.Event(), threading.Event()
        submissions = []

        def holder():
            writer = sqlite3.connect(self.store.path)
            try:
                writer.execute("BEGIN IMMEDIATE")
                locked.set()
                release.wait(10)
            finally:
                writer.close()

        def submit(label, operation, payload, checkpoint, authorize):
            checkpoint.progress("submit_click", True)
            authorize()
            submissions.append(True)
            thread.start()
            self.assertTrue(locked.wait(2))
            result = {
                "status": "confirmed",
                "verified": True,
                "message_ids": [123],
                "external_reference": "telegram:@public_group:123",
            }
            checkpoint(result)
            return result

        thread = threading.Thread(target=holder)
        self.addCleanup(lambda: thread.join(2) if thread.ident else None)
        self.addCleanup(release.set)
        with patch("auto_promotion.backends.telegram.call", submit):
            first = native_bridge.dispatch(self.request)
            saved = Receipts("telegram", "default").read(
                "execute:" + self.request["payload"]["item_id"]
            )
            self.assertTrue(saved["progress_mirror_deferred"])
            self.assertTrue(saved["verified"])
            release.set()
            thread.join(2)
            second = native_bridge.dispatch(self.request)
        self.assertEqual(submissions, [True])
        self.assertEqual(first, second)
        self.assertEqual(second["message_ids"], [123])

    def test_changed_payload_cannot_use_claim(self):
        self.request["payload"]["payload"]["text"] = "changed"
        with self.assertRaises(BackendError):
            verify_claim(self.request)

    def test_unclaimed_item_cannot_be_sent_directly(self):
        with self.store.connect(write=True) as db:
            db.execute("UPDATE items SET status='not_attempted'")
        with self.assertRaises(BackendError):
            verify_claim(self.request)

    def test_rate_limit_during_health_probe_persists_cooldown(self):
        with patch.object(
            self.engine.adapters,
            "call",
            return_value={
                "rate_limited": True,
                "retry_after_seconds": 90,
                "reason": "FloodWaitError",
            },
        ):
            checked = self.engine.check_account(self.account["id"], force=True)
        self.assertGreaterEqual(checked["cooldown_until"], time.time() + 85)
        with self.assertRaises(BackendError):
            verify_claim(self.request)

    def test_risk_warning_during_health_probe_disables_account(self):
        with patch.object(
            self.engine.adapters,
            "call",
            return_value={"risk_warning": True, "reason": "PeerFloodError"},
        ):
            checked = self.engine.check_account(self.account["id"], force=True)
        self.assertTrue(checked["disabled"])

    def test_interrupted_provider_progress_and_reference_survive_lease_recovery(self):
        calls = []

        def interrupted(label, operation, payload, checkpoint, authorize):
            calls.append(operation)
            checkpoint.progress("submission_response", True)
            checkpoint({"external_reference": "telegram:@public_group:123"})
            checkpoint.progress("receipt_lookup", True)
            raise KeyboardInterrupt()

        with patch("auto_promotion.backends.telegram.call", interrupted):
            with self.assertRaises(KeyboardInterrupt):
                native_bridge.dispatch(self.request)
        self.engine.clock = lambda: time.time() + 301
        self.engine.recover()
        item = self.engine.task(self.request["payload"]["task_id"])["items"][0]
        self.assertEqual(
            (item["status"], item["result"]["execution_step"]), ("unknown", "receipt_lookup")
        )
        self.assertTrue(item["result"]["submission_started"])
        self.assertEqual(item["external_reference"], "telegram:@public_group:123")
        self.assertEqual(self.engine.run_once(item["task_id"])["processed"], [])
        self.assertEqual(calls, ["execute"])


class FakeTelegram:
    def __init__(self):
        self.messages = {}
        self.sends = []
        self.lookups = 0
        self.me = types.SimpleNamespace(id=42, bot=False)
        self.joined = False
        self.fail_lookup = False
        self.pending = False
        self.history = []
        self.scheduled_messages = None

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return self.me

    async def get_entity(self, target):
        from telethon import types as telegram_types

        return telegram_types.Channel(
            id=10,
            title="Public",
            photo=telegram_types.ChatPhotoEmpty(),
            date=datetime.now(timezone.utc),
            username="public_group",
            megagroup=True,
        )

    async def get_permissions(self, entity, participant):
        return types.SimpleNamespace(
            send_messages=True, is_banned=False, has_left=False, participant=None
        )

    async def send_message(self, entity, text, **kwargs):
        self.sends.append((text, kwargs))
        message = types.SimpleNamespace(
            id=123,
            out=True,
            message=text,
            reply_to_msg_id=kwargs.get("reply_to"),
            media=None,
            date=kwargs.get("schedule"),
        )
        self.messages[123] = message
        return message

    async def send_file(self, entity, path, **kwargs):
        sent = await self.send_message(entity, kwargs["caption"], schedule=kwargs.get("schedule"))
        sent.media = object()
        return sent

    async def get_messages(self, entity, ids=None, limit=None, offset_date=None):
        if self.fail_lookup:
            raise ConnectionError("test transport dropped after submission")
        return [self.messages.get(i) for i in ids] if ids else self.history[:limit]

    async def iter_messages(self, entity, *, min_id, reverse, limit):
        messages = sorted((m for m in self.history if m.id > min_id), key=lambda m: m.id)
        for message in messages[:limit]:
            yield message

    async def __call__(self, request):
        from telethon import errors, functions

        if isinstance(request, functions.messages.GetScheduledHistoryRequest):
            self.lookups += 1
            return types.SimpleNamespace(
                messages=list(self.messages.values())
                if self.scheduled_messages is None
                else self.scheduled_messages
            )
        if isinstance(request, functions.channels.GetParticipantRequest):
            if not self.joined:
                raise errors.UserNotParticipantError(request)
            return types.SimpleNamespace(participant=type("ChannelParticipantSelf", (), {})())
        if isinstance(request, functions.channels.JoinChannelRequest):
            if self.pending:
                raise errors.InviteRequestSentError(request)
            self.joined = True
            return object()
        raise AssertionError(type(request))


@unittest.skipUnless(find_spec("telethon"), "Install .[telegram] to test Telegram")
class TelegramTests(unittest.TestCase):
    setUp = PrivateBackendTests.setUp

    def backend(self):
        from auto_promotion.backends.telegram import TelegramBackend

        save_profile("telegram", "default", {"identity": "42", "api_id": 1, "api_hash": "test"})
        self.client = FakeTelegram()
        self.saved = []
        self.authorized = []
        return TelegramBackend("default", client_factory=lambda *args: self.client)

    def run_action(self, backend, action="send_message", operation="execute", **changes):
        item = {
            "action": action,
            "target": "@public_group",
            "text": "exact **plain**",
            "media": [],
            "reply_to": None,
            "scheduled_at": iso(time.time() + 3600),
        }
        item.update(changes)
        return asyncio.run(
            backend.run(
                operation,
                {"item_id": "one", "payload": item},
                self.saved.append,
                lambda: self.authorized.append(True),
            )
        )

    def test_plain_text_send_verifies_independent_read_and_checkpoints_first(self):
        result = self.run_action(self.backend())
        self.assertEqual(result["status"], "confirmed")
        self.assertTrue(self.saved[0]["submission_started"])
        self.assertEqual(self.saved[1]["message_ids"], [123])
        self.assertIsNone(self.client.sends[0][1]["parse_mode"])
        self.assertEqual(len(self.authorized), 1)

    def test_exact_spambot_start_keeps_authorization_and_independent_receipt(self):
        from unittest.mock import AsyncMock

        from telethon import types as telegram_types

        backend = self.backend()
        self.client.get_entity = AsyncMock(
            return_value=telegram_types.User(id=10, username="SpamBot", bot=True)
        )
        self.client.get_permissions = AsyncMock(
            side_effect=AssertionError("Bots have no group membership")
        )
        result = self.run_action(backend, target="@SpamBot", text="/start", recipient_id="10")
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(len(self.client.sends), 1)
        self.assertEqual(self.client.sends[0][0], "/start")
        self.assertEqual(len(self.authorized), 1)
        self.assertEqual(self.saved[1]["message_ids"], [123])
        self.client.get_permissions.assert_not_called()

    def test_spambot_preflight_is_readonly_and_requires_the_pinned_identity(self):
        from unittest.mock import AsyncMock

        from telethon import types as telegram_types

        backend = self.backend()
        self.client.get_entity = AsyncMock(
            return_value=telegram_types.User(id=10, username="SpamBot", bot=True)
        )
        for recipient, allowed in [("10", True), ("11", False)]:
            result = asyncio.run(
                backend.run(
                    "preflight",
                    {
                        "payload": dict(
                            action="send_message",
                            target="@SpamBot",
                            text="/start",
                            recipient_id=recipient,
                        )
                    },
                    self.saved.append,
                    lambda: self.authorized.append(True),
                )
            )
            self.assertEqual(result["allowed"], allowed)
        self.assertEqual(self.client.sends, [])
        self.assertEqual(self.authorized, [])

    def test_bot_exception_does_not_allow_other_commands_or_recipients(self):
        from unittest.mock import AsyncMock

        from telethon import types as telegram_types

        for changes in [
            dict(text="/help"),
            dict(recipient_id="11"),
            dict(target="@OtherBot"),
            dict(action="reply", reply_to=1),
            dict(action="schedule_message"),
        ]:
            with self.subTest(changes=changes):
                backend = self.backend()
                self.client.get_entity = AsyncMock(
                    return_value=telegram_types.User(id=10, username="SpamBot", bot=True)
                )
                fields = dict(target="@SpamBot", text="/start", recipient_id="10")
                fields.update(changes)
                action = fields.pop("action", "send_message")
                result = self.run_action(backend, action=action, **fields)
                self.assertEqual(result["status"], "failed_known")
                self.assertEqual(self.client.sends, [])
                self.assertEqual(self.authorized, [])

    def test_submission_marker_failure_prevents_platform_send(self):
        import sqlite3

        backend = self.backend()

        def fail_checkpoint(result):
            self.assertTrue(result["submission_started"])
            self.assertEqual(self.client.sends, [])
            raise sqlite3.OperationalError("database is locked")

        item = {"action": "send_message", "target": "@public_group", "text": "exact", "media": []}
        with self.assertRaises(sqlite3.OperationalError):
            asyncio.run(backend.run("execute", {"payload": item}, fail_checkpoint, lambda: None))
        self.assertEqual(self.client.sends, [])

    def test_accepted_send_receipt_failure_and_restart_never_resends(self):
        import sqlite3

        from auto_promotion.backends.telegram import TelegramBackend

        self.real_backend_class = TelegramBackend
        self.backend()
        request = {
            "platform": "telegram",
            "account": "default",
            "operation": "execute",
            "payload": {
                "item_id": "receipt-failure",
                "payload": {
                    "action": "send_message",
                    "target": "@public_group",
                    "text": "exact",
                    "media": [],
                    "reply_to": None,
                    "scheduled_at": iso(time.time()),
                },
            },
        }
        original_save = Receipts.save

        def fail_after_send(receipts, key, result):
            if result.get("message_ids"):
                raise sqlite3.OperationalError("database is locked")
            return original_save(receipts, key, result)

        with (
            patch.object(native_bridge, "verify_claim"),
            patch("auto_promotion.backends.telegram.make_client", return_value=self.client),
            patch(
                "auto_promotion.backends.telegram.TelegramBackend",
                side_effect=lambda label: self._backend_for_test(label),
            ),
            patch.object(Receipts, "save", fail_after_send),
        ):
            first = native_bridge.dispatch(request)
        with patch.object(native_bridge, "verify_claim"):
            second = native_bridge.dispatch(request)
        self.assertEqual(len(self.client.sends), 1)
        self.assertEqual(first["status"], "unknown")
        self.assertTrue(first["submission_started"])
        self.assertEqual(first, second)

    def _backend_for_test(self, label):
        return self.real_backend_class(label, client_factory=lambda *args: self.client)

    def test_identity_mismatch_never_sends(self):
        backend = self.backend()
        self.client.me.id = 43
        self.assertFalse(self.run_action(backend)["healthy"])
        self.assertEqual(self.client.sends, [])

    def test_native_schedule_requires_two_independent_lookups(self):
        result = self.run_action(self.backend(), "schedule_message")
        self.assertEqual(self.client.lookups, 2)
        self.assertEqual(result["status"], "scheduled")
        self.assertEqual(result["message_ids"], result["lookup_message_ids"])

    def test_scheduled_delivery_resolves_real_id_without_sending_and_rejects_ambiguity(self):
        backend = self.backend()
        expected = datetime.now(timezone.utc).replace(microsecond=0)
        from datetime import timedelta

        expected -= timedelta(seconds=30)
        item = {
            "action": "schedule_message",
            "target": "@public_group",
            "text": "approved",
            "media": [],
            "reply_to": None,
            "scheduled_at": expected.isoformat(),
        }
        message = types.SimpleNamespace(
            id=900,
            out=True,
            message="approved",
            media=None,
            date=expected,
            from_scheduled=True,
            reply_to_msg_id=None,
        )
        self.client.messages[900] = message
        self.client.history = [message]
        self.client.scheduled_messages = []
        payload = {"payload": item, "result": {"message_ids": [2], "status": "scheduled"}}
        result = asyncio.run(
            backend.run("verify", payload, self.saved.append, lambda: self.fail("write"))
        )
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["message_ids"], [900])
        self.assertEqual(result["scheduled_message_ids"], [2])
        self.assertEqual(result["published_at"], expected.timestamp())
        self.assertEqual(self.client.sends, [])
        self.client.history.append(types.SimpleNamespace(**{**vars(message), "id": 901}))
        result = asyncio.run(
            backend.run("verify", payload, self.saved.append, lambda: self.fail("write"))
        )
        self.assertEqual(result["status"], "unknown")

    def test_feedback_collects_only_direct_other_user_replies_and_does_not_skip_future_messages(
        self,
    ):
        backend = self.backend()
        now = time.time()
        parent = types.SimpleNamespace(id=123, out=True, message="approved", media=None)
        self.client.messages[123] = parent
        self.client.history = [
            types.SimpleNamespace(
                id=i,
                out=False,
                sender_id=sender,
                reply_to_msg_id=reply_to,
                date=datetime.fromtimestamp(when, timezone.utc),
            )
            for i, sender, reply_to, when in [
                (124, 99, 123, now - 20),
                (125, 42, 123, now - 10),
                (126, 99, 555, now - 5),
                (127, 99, 123, now + 60),
            ]
        ]
        payload = {
            "payload": {"target": "@public_group", "text": "approved", "media": []},
            "result": {"message_ids": [123]},
        }
        result = asyncio.run(
            backend.run("feedback", payload, self.saved.append, lambda: self.fail("write"))
        )
        self.assertEqual(len(result["feedback_events"]), 1)
        self.assertEqual(result["feedback_events"][0]["event_id"], "10:124")
        self.assertEqual(result["feedback_cursor"], 126)
        self.assertEqual(result["feedback_status"], "complete")
        self.assertEqual(self.client.sends, [])

    def test_read_failure_preserves_accepted_id_without_resubmitting(self):
        backend = self.backend()
        self.client.fail_lookup = True
        with self.assertRaises(ConnectionError):
            self.run_action(backend)
        self.assertTrue(self.saved[0]["submission_started"])
        self.assertEqual(self.saved[1]["message_ids"], [123])
        self.assertEqual(len(self.client.sends), 1)

    def test_missing_parent_keeps_tracking_verified_receipt_but_coverage_stays_partial(self):
        backend = self.backend()
        now = time.time()
        self.client.history = [
            types.SimpleNamespace(
                id=i,
                out=False,
                sender_id=sender,
                reply_to_msg_id=reply_to,
                date=datetime.fromtimestamp(when, timezone.utc),
            )
            for i, sender, reply_to, when in (
                (575, 99, 500, now - 10),
                (576, 42, 500, now - 8),
                (577, 99, 501, now - 5),
            )
        ]
        result = asyncio.run(
            backend.run(
                "feedback",
                {
                    "payload": {"target": "@public_group", "text": "approved", "media": []},
                    "result": {
                        "message_ids": [500],
                        "message_chat_id": "-1000000000010",
                        "verified": True,
                        "status": "confirmed",
                        "published_at": now - 100,
                        "external_reference": "telegram:@public_group:500",
                    },
                    "feedback_cursor": 574,
                },
                self.saved.append,
                lambda: self.fail("write"),
            )
        )
        self.assertEqual(result["feedback_status"], "partial")
        self.assertEqual(result["reason"], "feedback_parent_unavailable")
        self.assertEqual([e["event_id"] for e in result["feedback_events"]], ["10:575"])
        self.assertEqual(result["feedback_cursor"], 577)
        self.assertFalse(self.client.sends)
        self.assertFalse(self.client.joined)

    def test_missing_parent_without_exact_verified_receipt_never_scans(self):
        from unittest.mock import Mock

        backend = self.backend()
        self.client.iter_messages = Mock(side_effect=AssertionError("unverified history scan"))
        receipt = {
            "message_ids": [500],
            "verified": True,
            "status": "confirmed",
            "published_at": time.time() - 100,
            "external_reference": "telegram:@public_group:500",
        }
        for changes in (
            {"verified": False},
            {"status": "unknown"},
            {"external_reference": "telegram:@other:500"},
            {"published_at": float("nan")},
            {"published_at": time.time() + 100},
        ):
            with self.subTest(changes=changes):
                result = asyncio.run(
                    backend.run(
                        "feedback",
                        {
                            "payload": {"target": "@public_group", "text": "approved", "media": []},
                            "result": {**receipt, **changes},
                        },
                        self.saved.append,
                        lambda: self.fail("write"),
                    )
                )
                self.assertFalse(result["verified"])
        self.client.iter_messages.assert_not_called()

    def test_present_mismatched_parent_is_not_replaced_by_receipt_fallback(self):
        backend = self.backend()
        self.client.messages[500] = types.SimpleNamespace(id=500, out=False, message="approved")
        result = asyncio.run(
            backend.run(
                "feedback",
                {
                    "payload": {"target": "@public_group", "text": "approved", "media": []},
                    "result": {
                        "message_ids": [500],
                        "verified": True,
                        "status": "confirmed",
                        "published_at": time.time() - 100,
                        "external_reference": "telegram:@public_group:500",
                    },
                },
                self.saved.append,
                lambda: self.fail("write"),
            )
        )
        self.assertFalse(result["verified"])
        self.assertEqual(result["reason"], "feedback_parent_not_verified")

    def test_pending_join_is_distinct_and_not_counted_as_joined(self):
        backend = self.backend()
        self.client.pending = True
        result = asyncio.run(
            backend.run("join", {"target": "@public_group"}, self.saved.append, lambda: None)
        )
        self.assertEqual(result["status"], "pending_admin_approval")
        self.assertFalse(result["verified"])

    def test_verify_membership_never_joins(self):
        backend = self.backend()
        result = asyncio.run(
            backend.run(
                "verify_join",
                {"target": "@public_group"},
                self.saved.append,
                lambda: self.fail("Unexpected write"),
            )
        )
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(self.client.joined)

    def test_reply_receipt_binds_parent_message(self):
        result = self.run_action(self.backend(), "reply", reply_to=12)
        self.assertTrue(result["verified"])
        self.assertEqual(self.client.sends[0][1]["reply_to"], 12)

    def test_file_and_caption_are_read_back(self):
        result = self.run_action(self.backend(), "send_file", media=[{"path": "approved.png"}])
        self.assertTrue(result["verified"])
        self.assertIsNotNone(self.client.messages[123].media)

    def test_schedule_with_wrong_time_is_not_verified(self):
        from auto_promotion.backends.telegram import message_matches

        item = {"text": "exact", "media": [], "scheduled_at": iso(time.time() + 3600)}
        observed = types.SimpleNamespace(out=True, message="exact", date=datetime.now(timezone.utc))
        self.assertFalse(message_matches(observed, item, scheduled=True))
