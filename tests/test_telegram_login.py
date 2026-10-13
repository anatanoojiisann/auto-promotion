"""Exercise real Telethon request handling with an offline sender."""

import asyncio
import os
import tempfile
import threading
import types
import unittest
from importlib.util import find_spec
from unittest.mock import AsyncMock, Mock, patch


@unittest.skipUnless(find_spec("telethon"), "Install .[telegram] to test login")
class TelegramLoginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"AP_ACCOUNTS_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def client(self, *, for_login):
        from auto_promotion.backends.common import profile_path
        from auto_promotion.backends.telegram import make_client

        profile_path("telegram", "test-login").mkdir(parents=True, exist_ok=True)
        client = make_client(
            "test-login", {"api_id": 1, "api_hash": "synthetic"}, for_login=for_login
        )
        self.addCleanup(client.session.close)
        client.is_user_authorized = AsyncMock(return_value=False)
        client._switch_dc = AsyncMock()
        return client

    def test_login_follows_phone_migration_and_returns_code_challenge(self):
        from telethon import errors, functions
        from telethon import types as tl

        async def scenario():
            client = self.client(for_login=True)
            calls = []
            accepted = tl.auth.SentCode(tl.auth.SentCodeTypeApp(5), "synthetic-challenge")

            def send(request, **kwargs):
                calls.append(request)
                future = asyncio.get_running_loop().create_future()
                if len(calls) == 1:
                    future.set_exception(errors.PhoneMigrateError(request, capture=4))
                else:
                    future.set_result(accepted)
                return future

            client._sender = types.SimpleNamespace(send=send)
            result = await client.send_code_request("00000")
            self.assertIs(result, accepted)
            self.assertEqual(len(calls), 2)
            client._switch_dc.assert_awaited_once_with(4)
            self.assertTrue(
                all(isinstance(r, functions.InvokeWithoutUpdatesRequest) for r in calls)
            )

        asyncio.run(scenario())

    def test_repeated_migration_is_bounded(self):
        from telethon import errors

        async def scenario():
            client = self.client(for_login=True)
            calls = []

            def send(request, **kwargs):
                calls.append(request)
                future = asyncio.get_running_loop().create_future()
                future.set_exception(errors.PhoneMigrateError(request, capture=4))
                return future

            client._sender = types.SimpleNamespace(send=send)
            with self.assertRaises(errors.PhoneMigrateError):
                await client.send_code_request("00000")
            self.assertEqual(len(calls), 3)

        asyncio.run(scenario())

    def test_outbound_server_failure_is_not_retried(self):
        from telethon import errors, functions
        from telethon import types as tl

        async def scenario():
            client = self.client(for_login=False)
            calls = []

            def send(request, **kwargs):
                calls.append(request)
                future = asyncio.get_running_loop().create_future()
                future.set_exception(errors.RpcCallFailError(request))
                return future

            client._sender = types.SimpleNamespace(send=send)
            request = functions.messages.SendMessageRequest(
                tl.InputPeerSelf(), "approved", random_id=1
            )
            with patch("asyncio.sleep", new=AsyncMock()):
                with self.assertRaises(errors.RpcCallFailError):
                    await client(request)
            self.assertEqual(len(calls), 1)

        asyncio.run(scenario())

    def test_idle_disconnect_reconnects_only_during_login(self):
        async def scenario():
            for for_login in (True, False):
                with self.subTest(for_login=for_login):
                    client = self.client(for_login=for_login)
                    sender = client._sender
                    sender._user_connected = True
                    sender._connection = types.SimpleNamespace(disconnect=AsyncMock())
                    sender._connect = AsyncMock()
                    sender._disconnect = AsyncMock()
                    sender._auto_reconnect_callback = None
                    await sender._reconnect(ConnectionError())
                    self.assertEqual(sender._connect.await_count, 1 if for_login else 0)
                    self.assertEqual(sender._disconnect.await_count, 0 if for_login else 1)

        asyncio.run(scenario())

    def test_login_transport_recovery_is_bounded(self):
        async def scenario():
            client = self.client(for_login=True)
            sender = client._sender
            sender._user_connected = True
            sender._connection = types.SimpleNamespace(disconnect=AsyncMock())
            sender._connect = AsyncMock(side_effect=ConnectionError())
            sender._disconnect = AsyncMock()
            sender._auto_reconnect_callback = None
            with patch("asyncio.sleep", new=AsyncMock()):
                await sender._reconnect(ConnectionError())
            self.assertEqual(sender._connect.await_count, 2)
            sender._disconnect.assert_awaited_once()

        asyncio.run(scenario())

    def test_human_code_prompt_does_not_block_network_loop(self):
        from auto_promotion.backends.telegram import login

        entered, released = threading.Event(), threading.Event()

        def human_input(text):
            entered.set()
            released.wait(timeout=2)
            return "synthetic-input"

        async def start(**callbacks):
            pending = asyncio.create_task(callbacks["code_callback"]())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                self.assertFalse(pending.done())
            finally:
                released.set()
            self.assertEqual(await pending, "synthetic-input")

        client = types.SimpleNamespace(
            start=AsyncMock(side_effect=start),
            get_me=AsyncMock(return_value=types.SimpleNamespace(id=1, bot=False)),
            disconnect=AsyncMock(),
        )
        with (
            patch("auto_promotion.backends.telegram.read_profile", return_value={"identity": "1"}),
            patch("auto_promotion.backends.telegram.make_client", return_value=client),
            patch("auto_promotion.backends.telegram.save_profile", new=Mock()),
            patch("auto_promotion.backends.telegram.getpass.getpass", side_effect=human_input),
        ):
            self.assertTrue(asyncio.run(login("test-login"))["authenticated"])
        client.disconnect.assert_awaited_once()
