import asyncio
import json
import os
import tempfile
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
        self.assertEqual(len(registry.services()), 5)
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
        self.assertNotIn("secret-token", json.dumps(first))


class ClaimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(
            os.environ, {"AUTO_PROMOTION_LIVE_WRITES": "1", "AP_TELEGRAM_OWNER": "auto-promotion"}
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
        self.engine.approve(task["id"], "discovery_join", task["discovery_phrase"])
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
        self.engine.approve(task["id"], "outbound", task["outbound_phrase"])
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

    def test_current_claim_and_exact_approvals_are_required(self):
        verify_claim(self.request)
        with self.store.connect(write=True) as db:
            db.execute("DELETE FROM approvals WHERE stage='outbound'")
        with self.assertRaises(Exception):
            verify_claim(self.request)

    def test_stop_rechecked_inside_provider(self):
        self.engine.set_stop(True)
        with self.assertRaises(BackendError):
            verify_claim(self.request)

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
            checked = self.engine.check_account(self.account["id"])
        self.assertGreaterEqual(checked["cooldown_until"], time.time() + 85)
        with self.assertRaises(BackendError):
            verify_claim(self.request)

    def test_risk_warning_during_health_probe_disables_account(self):
        with patch.object(
            self.engine.adapters,
            "call",
            return_value={"risk_warning": True, "reason": "PeerFloodError"},
        ):
            checked = self.engine.check_account(self.account["id"])
        self.assertTrue(checked["disabled"])


class FakeTelegram:
    def __init__(self):
        self.messages = {}
        self.sends = []
        self.lookups = 0
        self.me = types.SimpleNamespace(id=42, bot=False)
        self.joined = False
        self.fail_lookup = False
        self.pending = False

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return self.me

    async def get_entity(self, target):
        return types.SimpleNamespace(id=10, username="public_group", megagroup=True)

    async def get_permissions(self, entity, participant):
        return types.SimpleNamespace(send_messages=True)

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

    async def get_messages(self, entity, ids=None, limit=None):
        if self.fail_lookup:
            raise ConnectionError("test transport dropped after submission")
        return [self.messages.get(i) for i in ids] if ids else []

    async def __call__(self, request):
        from telethon import errors, functions

        if isinstance(request, functions.messages.GetScheduledHistoryRequest):
            self.lookups += 1
            return types.SimpleNamespace(messages=list(self.messages.values()))
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
        self.assertEqual(self.saved[0]["message_ids"], [123])
        self.assertIsNone(self.client.sends[0][1]["parse_mode"])
        self.assertEqual(len(self.authorized), 1)

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

    def test_read_failure_preserves_accepted_id_without_resubmitting(self):
        backend = self.backend()
        self.client.fail_lookup = True
        with self.assertRaises(ConnectionError):
            self.run_action(backend)
        self.assertEqual(self.saved[0]["message_ids"], [123])
        self.assertEqual(len(self.client.sends), 1)

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
