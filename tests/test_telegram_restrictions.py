"""Telegram final-RPC rejection and transport uncertainty (synthetic clients only)."""

import unittest
from unittest.mock import AsyncMock, patch

import test_native_backends as fixtures

from auto_promotion import native_bridge


class TelegramRestrictionTests(unittest.TestCase):
    setUp = fixtures.PrivateBackendTests.setUp
    backend = fixtures.TelegramTests.backend
    run_action = fixtures.TelegramTests.run_action

    def test_final_send_user_banned_is_known_rejection_not_unknown(self):
        from telethon import errors

        backend = self.backend()
        self.client.send_message = AsyncMock(side_effect=errors.UserBannedInChannelError(None))
        result = self.run_action(backend)
        self.assertEqual(result["status"], "failed_known")
        self.assertEqual(result["account_restriction"], "telegram_speaking_restricted")
        self.assertEqual(result["failure_class"], "restricted")
        self.assertEqual(self.client.send_message.await_count, 1)

    def test_native_final_send_rejection_is_not_transformed_into_unknown(self):
        from telethon import errors

        backend = self.backend()
        self.client.send_message = AsyncMock(side_effect=errors.UserBannedInChannelError(None))
        request = {
            "platform": "telegram",
            "account": "default",
            "operation": "execute",
            "payload": {
                "item_id": "restriction",
                "payload": {
                    "action": "send_message",
                    "target": "@public_group",
                    "text": "exact",
                    "media": [],
                },
            },
        }
        with (
            patch.object(native_bridge, "verify_claim"),
            patch("auto_promotion.backends.telegram.TelegramBackend", return_value=backend),
        ):
            result = native_bridge.dispatch(request)
            again = native_bridge.dispatch(request)
        self.assertEqual(result["status"], "failed_known")
        self.assertEqual(result, again)
        self.assertEqual(self.client.send_message.await_count, 1)

    def test_accepted_id_then_restriction_during_lookup_keeps_unknown(self):
        from telethon import errors

        backend = self.backend()
        self.client.get_messages = AsyncMock(side_effect=errors.UserBannedInChannelError(None))
        result = self.run_action(backend)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["message_ids"], [123])
        self.assertEqual(len(self.client.sends), 1)

    def test_no_id_verify_never_constructs_or_connects_client(self):
        backend = self.backend()
        backend.client_factory = lambda *a: self.fail(
            "verify without IDs must do zero network work"
        )
        result = self.run_action(backend, operation="verify")
        self.assertEqual(result["reason"], "no_durable_receipt")

    def test_transport_timeout_after_send_stays_uncertain(self):
        backend = self.backend()
        self.client.send_message = AsyncMock(side_effect=TimeoutError("synthetic"))
        with self.assertRaises(TimeoutError):
            self.run_action(backend)
        self.assertTrue(self.saved[0]["submission_started"])
        self.assertEqual(self.client.send_message.await_count, 1)

    def test_disconnect_failure_cannot_overwrite_explicit_rpc_rejection(self):
        from telethon import errors

        backend = self.backend()
        self.client.send_message = AsyncMock(side_effect=errors.UserBannedInChannelError(None))
        self.client.disconnect = AsyncMock(side_effect=ConnectionError("synthetic teardown"))
        result = self.run_action(backend)
        self.assertEqual(result["status"], "failed_known")
        self.assertEqual(result["reason"], "UserBannedInChannelError")
        self.assertEqual(result["disconnect_error"], "ConnectionError")

    def test_rpc_diagnostics_count_transport_request_categories_without_payloads(self):
        backend = self.backend()
        self.client._call = AsyncMock(return_value=None)
        original = self.client.get_entity

        async def entity(target):
            await self.client._call(None, type("ResolveUsernameRequest", (), {})())
            return await original(target)

        self.client.get_entity = entity
        result = self.run_action(backend)
        self.assertEqual(result["rpc_counts"], {"entity": 1})
        self.assertEqual(result["rpc_count_source"], "telethon_logical_call_attempt")
        self.assertNotIn("target", str(result["rpc_counts"]))
