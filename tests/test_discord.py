import asyncio
import copy
import hashlib
import io
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from test_api import request

from auto_promotion.adapters import AdapterRegistry, SimulationAdapter
from auto_promotion.api import create_app
from auto_promotion.backends.common import BackendError, read_profile, save_profile
from auto_promotion.backends.discord import (
    ADMIN,
    ATTACH,
    HISTORY,
    SEND,
    VIEW,
    DiscordBackend,
    DiscordError,
    DiscordHTTP,
    NoRedirect,
    channel_permissions,
    configure,
)
from auto_promotion.connections import ConnectionInput, ConnectionManager
from auto_promotion.core import Conflict, Engine
from auto_promotion.models import AccountInput, ItemInput, TaskInput
from auto_promotion.native_bridge import dispatch
from auto_promotion.store import Store

BOT = "123456789012345678"
GUILD = "234567890123456789"
CHANNEL = "345678901234567890"
MESSAGE = "456789012345678901"
ORIGINAL = "999999999999999999"
ROLE = "567890123456789012"


class FakeDiscord:
    def __init__(self):
        self.calls = []
        self.me = {"id": BOT, "username": "Brand Bot", "bot": True}
        self.guild = {
            "id": GUILD,
            "owner_id": "1",
            "roles": [
                {"id": GUILD, "permissions": str(VIEW)},
                {"id": ROLE, "permissions": str(VIEW | SEND | HISTORY | ATTACH)},
            ],
        }
        self.member = {"user": {"id": BOT}, "roles": [ROLE]}
        self.channels = [{"id": CHANNEL, "guild_id": GUILD, "type": 0, "name": "announcements"}]
        self.message = None
        self.fail_read = False
        self.post_error = None

    def request(self, path, *, body=None, files=None):
        self.calls.append((path, copy.deepcopy(body), files))
        if path == "users/@me":
            return copy.deepcopy(self.me)
        if path.startswith("users/@me/guilds?"):
            return [{"id": GUILD, "name": "Brand Community"}]
        if path == "guilds/" + GUILD:
            return copy.deepcopy(self.guild)
        if path == f"guilds/{GUILD}/members/{BOT}":
            return copy.deepcopy(self.member)
        if path == f"guilds/{GUILD}/channels":
            return copy.deepcopy(self.channels)
        if path == "channels/" + CHANNEL:
            return copy.deepcopy(self.channels[0])
        if path == f"channels/{CHANNEL}/messages/{ORIGINAL}":
            return {"id": ORIGINAL, "channel_id": CHANNEL}
        if path == f"channels/{CHANNEL}/messages" and body is not None:
            if self.post_error:
                raise self.post_error
            self.message = {
                "id": MESSAGE,
                "channel_id": CHANNEL,
                "author": self.me,
                "content": body["content"],
                "nonce": body["nonce"],
                "attachments": [
                    {"filename": name, "size": len(content)} for name, content in files or []
                ],
            }
            if "message_reference" in body:
                self.message["message_reference"] = body["message_reference"]
            return copy.deepcopy(self.message)
        if path == f"channels/{CHANNEL}/messages/{MESSAGE}":
            if self.fail_read:
                raise BackendError("DiscordTransportUnavailable")
            return copy.deepcopy(self.message)
        raise AssertionError(path)


class DiscordBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {"AP_ACCOUNTS_DIR": str(self.root / "profiles")})
        env.start()
        self.addCleanup(env.stop)
        save_profile("discord", "brand", {"identity": BOT, "bot_token": "private-test-token"})
        self.http = FakeDiscord()
        self.backend = DiscordBackend("brand", http_factory=lambda _: self.http)
        self.checkpoint = MagicMock()
        self.authorize = MagicMock()

    def payload(self, **changes):
        return {
            "item_id": "item-exact-123",
            "payload": {
                "action": "send_message",
                "target": CHANNEL,
                "text": "exact @everyone <@123>",
                "media": [],
                "title": "",
                "reply_to": None,
                "discord_reply_to": "",
                "recipient_id": "",
                **changes,
            },
        }

    def run_backend(self, operation, payload=None):
        return self.backend.run(
            operation, payload or self.payload(), self.checkpoint, self.authorize
        )

    def test_bot_identity_and_channel_catalog_filter_permissions_and_private_channels(self):
        self.http.channels += [
            {"id": "678901234567890123", "type": 0, "nsfw": True},
            {"id": "789012345678901234", "type": 2},
            {
                "id": "890123456789012345",
                "type": 0,
                "permission_overwrites": [
                    {"id": GUILD, "type": 0, "deny": str(VIEW), "allow": "0"}
                ],
            },
        ]
        self.assertTrue(self.run_backend("health")["identity_verified"])
        catalog = self.run_backend("targets")
        self.assertEqual([c["id"] for c in catalog["targets"]], [CHANNEL])
        self.assertTrue(catalog["targets"][0]["can_attach"])
        self.assertFalse(catalog["truncated"])
        self.assertFalse(any(body for _, body, _ in self.http.calls))

    def test_member_overwrites_take_precedence_over_roles_and_admin_bypasses_denies(self):
        self.http.channels[0]["permission_overwrites"] = [
            {"id": GUILD, "type": 0, "deny": str(SEND), "allow": "0"},
            {"id": ROLE, "type": 0, "deny": "0", "allow": str(SEND)},
            {"id": BOT, "type": 1, "deny": str(SEND), "allow": "0"},
        ]
        self.assertFalse(
            channel_permissions(self.http.guild, self.http.member, self.http.channels[0]) & SEND
        )
        self.assertFalse(self.run_backend("preflight")["allowed"])
        self.http.guild["roles"][1]["permissions"] = str(ADMIN)
        self.assertTrue(self.run_backend("preflight")["allowed"])

    def test_send_rechecks_authorization_suppresses_mentions_and_verifies_receipt(self):
        result = self.run_backend("execute")
        self.assertEqual(result["status"], "confirmed")
        self.authorize.assert_called_once()
        post = next(c for c in self.http.calls if c[1] is not None)
        self.assertEqual(post[1]["allowed_mentions"], {"parse": [], "replied_user": False})
        self.assertTrue(post[1]["enforce_nonce"])
        self.assertEqual(post[1]["nonce"], "item-exact-123")
        self.checkpoint.assert_called_once()
        self.assertEqual(self.checkpoint.call_args.args[0]["status"], "unknown")
        self.assertEqual(
            result["external_reference"],
            f"https://discord.com/channels/{GUILD}/{CHANNEL}/{MESSAGE}",
        )

    def test_reply_uses_exact_string_id_and_file_bytes(self):
        path = self.root / "image.png"
        content = b"image\x00binary"
        path.write_bytes(content)
        media = {
            "filename": "image.png",
            "path": str(path),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        result = self.run_backend(
            "execute", self.payload(action="reply", discord_reply_to=ORIGINAL, media=[media])
        )
        self.assertTrue(result["verified"])
        post = next(c for c in self.http.calls if c[1] is not None)
        self.assertEqual(post[1]["message_reference"]["message_id"], ORIGINAL)
        self.assertEqual(post[2], [("image.png", content)])

    def test_permission_loss_or_missing_reply_never_submits(self):
        self.http.member["roles"] = []
        self.assertEqual(self.run_backend("execute")["status"], "failed_known")
        self.authorize.assert_not_called()
        self.assertFalse(any(body for _, body, _ in self.http.calls))

    def test_expired_member_timeout_does_not_block_and_future_timeout_does(self):
        self.http.member["communication_disabled_until"] = "2020-01-01T00:00:00+00:00"
        self.assertTrue(self.run_backend("preflight")["allowed"])
        self.http.member["communication_disabled_until"] = "2099-01-01T00:00:00+00:00"
        self.assertFalse(self.run_backend("preflight")["allowed"])

    def test_user_token_or_different_bot_cannot_execute(self):
        self.http.me["bot"] = False
        with self.assertRaises(BackendError):
            self.run_backend("execute")
        self.http.me.update(bot=True, id="123456789012345679")
        with self.assertRaises(BackendError):
            self.run_backend("execute")
        self.authorize.assert_not_called()

    def test_read_failure_keeps_receipt_and_reconciliation_does_not_resend(self):
        self.http.fail_read = True
        with self.assertRaises(BackendError):
            self.run_backend("execute")
        receipt = self.checkpoint.call_args.args[0]
        self.assertEqual(receipt["message_ids"], [MESSAGE])
        self.http.fail_read = False
        payload = {**self.payload(), "receipt": receipt}
        self.assertEqual(self.run_backend("verify", payload)["status"], "confirmed")
        self.assertEqual(sum(body is not None for _, body, _ in self.http.calls), 1)
        self.http.message["author"] = {"id": "111111111111111111"}
        self.assertFalse(self.run_backend("verify", payload)["verified"])

    def test_known_rejection_is_failed_and_429_is_not_retried(self):
        self.http.post_error = DiscordError(403)
        self.assertEqual(self.run_backend("execute")["status"], "failed_known")
        self.http.post_error = DiscordError(429, 43)
        with self.assertRaises(DiscordError):
            self.run_backend("execute")
        self.assertEqual(sum(body is not None for _, body, _ in self.http.calls), 2)

    def test_missing_receipt_cannot_claim_success(self):
        self.assertEqual(self.run_backend("verify")["status"], "unknown")
        self.assertFalse(any(body for _, body, _ in self.http.calls))

    def test_bridge_keeps_accepted_discord_receipt_and_never_repeats_submission(self):
        self.http.fail_read = True
        request = {
            "platform": "discord",
            "account": "brand",
            "operation": "execute",
            "payload": self.payload(),
        }
        with (
            patch("auto_promotion.native_bridge.verify_claim"),
            patch("auto_promotion.backends.discord.DiscordBackend", return_value=self.backend),
        ):
            first = dispatch(request)
            self.assertEqual(first["status"], "unknown")
            self.assertEqual(first["message_ids"], [MESSAGE])
            self.assertEqual(dispatch(request), first)
            self.http.fail_read = False
            verified = dispatch({**request, "operation": "verify"})
            self.assertEqual(verified["status"], "confirmed")
        self.assertEqual(sum(body is not None for _, body, _ in self.http.calls), 1)

    def test_cli_configuration_never_returns_token_and_preserves_identity(self):
        with patch("auto_promotion.backends.discord.DiscordHTTP", return_value=self.http):
            result = configure("configured", token="private-test-token")
            self.assertNotIn("private-test-token", json.dumps(result))
            self.http.me["id"] = "123456789012345679"
            with self.assertRaises(BackendError):
                configure("configured", token="other-token")
        self.assertEqual(read_profile("discord", "configured")["identity"], BOT)


class DiscordHTTPTests(unittest.TestCase):
    def setUp(self):
        self.http = DiscordHTTP({"bot_token": "private-test-token"})
        self.response = MagicMock()
        self.response.__enter__.return_value = self.response
        self.response.read.return_value = b'{"id": "1"}'
        self.http.opener = MagicMock()
        self.http.opener.open.return_value = self.response

    def test_fixed_host_bot_authorization_and_multipart_upload(self):
        self.http.request(
            "channels/1/messages", body={"content": "中文"}, files=[("图片.png", b"\x00abc")]
        )
        req = self.http.opener.open.call_args.args[0]
        self.assertEqual(req.full_url, "https://discord.com/api/v10/channels/1/messages")
        self.assertEqual(req.get_header("Authorization"), "Bot private-test-token")
        self.assertIn(b'name="payload_json"', req.data)
        self.assertIn('filename="图片.png"'.encode(), req.data)
        self.assertIn(b"\x00abc", req.data)
        self.assertNotIn(b"private-test-token", req.data)
        with self.assertRaises(BackendError):
            self.http.request("https://evil.example")
        self.assertIsNone(
            NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example")
        )

    def test_429_fractional_delay_and_secret_transport_errors_are_sanitized(self):
        error = HTTPError(
            "secret-url", 429, "private-test-token", {}, io.BytesIO(b'{"retry_after": 2.2}')
        )
        self.http.opener.open.side_effect = error
        with self.assertRaises(DiscordError) as caught:
            self.http.request("users/@me")
        self.assertEqual(caught.exception.retry_after, 3)
        self.assertNotIn("private-test-token", str(caught.exception))
        self.http.opener.open.side_effect = URLError("private-test-token")
        with self.assertRaises(BackendError) as caught:
            self.http.request("users/@me")
        self.assertEqual(str(caught.exception), "DiscordTransportUnavailable")


class DiscordWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = time.time()
        self.engine = Engine(
            Store(self.root / "test.db"), AdapterRegistry(self.root), clock=lambda: self.now
        )
        self.account = self.engine.add_account(
            AccountInput(
                platform="discord",
                label="Bot",
                backend_label="simulation",
                mode="simulation",
                min_interval=0,
            )
        )
        self.engine.check_account(self.account["id"])

        def iso(seconds):
            return datetime.fromtimestamp(seconds, timezone.utc).isoformat()

        self.task = self.engine.create_task(
            TaskInput(
                name="Discord",
                account_ids=[self.account["id"]],
                policy={
                    "starts_at": iso(self.now - 60),
                    "ends_at": iso(self.now + 3600),
                    "min_interval": 0,
                },
            )
        )
        self.iso = iso

    def item(self, **changes):
        return ItemInput(
            account_id=self.account["id"],
            action="send_message",
            target=CHANNEL,
            text="exact",
            scheduled_at=self.iso(self.now + 10),
            **changes,
        )

    def test_future_delivery_waits_for_both_exact_approvals_and_due_time(self):
        self.engine.set_stop(False)
        with self.assertRaises(Conflict):
            self.engine.set_items(self.task["id"], [self.item()])
        self.engine.approve(self.task["id"], "discovery_join", self.task["discovery_phrase"])
        self.engine.set_items(self.task["id"], [self.item()])
        frozen = self.engine.prepare(self.task["id"])
        self.assertEqual(self.engine.run_once()["processed"], [])
        self.engine.approve(frozen["id"], "outbound", frozen["outbound_phrase"])
        self.assertEqual(self.engine.run_once()["processed"], [])
        self.now += 11
        self.assertEqual(self.engine.run_once()["processed"][0]["status"], "confirmed")
        self.assertEqual(self.engine.run_once()["processed"], [])

    def test_string_ids_are_preserved_and_platform_limits_reject_invalid_payloads(self):
        value = ItemInput(
            account_id=self.account["id"],
            action="reply",
            target=CHANNEL,
            text="exact",
            scheduled_at=self.iso(self.now),
            discord_reply_to=ORIGINAL,
        )
        self.engine.validate_item(self.task, value)
        self.assertEqual(value.model_dump()["discord_reply_to"], ORIGINAL)
        for changes in (
            {"target": "https://discord.com/channels/a/b"},
            {"text": "x" * 2001},
            {"reply_to": 1},
            {"discord_reply_to": ORIGINAL},
            {"action": "reply"},
        ):
            base = {**self.item().model_dump(), **changes}
            with self.assertRaises(Conflict):
                self.engine.validate_item(self.task, ItemInput(**base))

    def test_catalog_rate_limit_records_cooldown(self):
        with patch.object(
            self.engine.adapters,
            "call",
            return_value={"rate_limited": True, "retry_after_seconds": 90},
        ):
            with self.assertRaises(Conflict):
                self.engine.account_targets(self.account["id"])
        self.assertEqual(self.engine.account(self.account["id"])["cooldown_until"], self.now + 90)

    def test_native_bridge_refuses_unapproved_discord_write(self):
        with patch.dict(os.environ, {"AUTO_PROMOTION_LIVE_WRITES": "0"}):
            with self.assertRaises(BackendError):
                dispatch(
                    {
                        "platform": "discord",
                        "account": "brand",
                        "operation": "execute",
                        "payload": {},
                    }
                )

    def test_cooldown_does_not_poll_discord_before_retry_after(self):
        self.engine.apply_cooldown(
            self.account["id"], {"rate_limited": True, "retry_after_seconds": 90}
        )
        with patch.object(self.engine.adapters, "call") as call:
            self.engine.check_account(self.account["id"])
            with self.assertRaises(Conflict):
                self.engine.account_targets(self.account["id"])
        call.assert_not_called()

    def test_live_preflight_rate_limit_cools_account_without_claiming_item(self):
        live = self.engine.add_account(
            AccountInput(platform="discord", label="live", backend_label="brand")
        )
        task = self.engine.create_task(
            TaskInput(
                name="live",
                account_ids=[live["id"]],
                policy={"starts_at": self.iso(self.now - 60), "ends_at": self.iso(self.now + 3600)},
            )
        )
        self.engine.approve(task["id"], "discovery_join", task["discovery_phrase"])
        self.engine.set_items(
            task["id"],
            [
                ItemInput(
                    account_id=live["id"],
                    action="send_message",
                    target=CHANNEL,
                    text="exact",
                    scheduled_at=self.iso(self.now - 1),
                )
            ],
        )
        task = self.engine.prepare(task["id"])
        self.engine.approve(task["id"], "outbound", task["outbound_phrase"])
        self.engine.live_enabled = True
        self.engine.set_stop(False)

        def platform_call(account, operation, payload):
            if operation == "health":
                return {"healthy": True, "identity_verified": True}
            if operation == "preflight":
                return {"allowed": False, "rate_limited": True, "retry_after_seconds": 45}
            raise AssertionError(operation)

        with (
            patch.dict(
                os.environ,
                {"AUTO_PROMOTION_LIVE_WRITES": "1", "AP_DISCORD_OWNER": "auto-promotion"},
            ),
            patch.object(self.engine.adapters, "call", side_effect=platform_call) as call,
        ):
            self.assertEqual(self.engine.run_once(task["id"])["processed"][0]["status"], "blocked")
            count = call.call_count
            self.assertEqual(self.engine.run_once(task["id"])["processed"], [])
            self.assertEqual(call.call_count, count)
        self.assertEqual(self.engine.task(task["id"])["items"][0]["status"], "not_attempted")
        self.assertEqual(self.engine.account(live["id"])["cooldown_until"], self.now + 45)


class DiscordConnectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {"AP_ACCOUNTS_DIR": str(self.root / "profiles")})
        env.start()
        self.addCleanup(env.stop)
        self.engine = Engine(
            Store(self.root / "test.db"),
            AdapterRegistry(self.root, overrides={"discord": SimulationAdapter()}),
        )
        self.manager = ConnectionManager(self.engine)
        thread = patch("auto_promotion.connections.threading.Thread")
        thread.start()
        self.addCleanup(thread.stop)

    def test_web_connection_registers_bot_without_enabling_external_writes(self):
        value = ConnectionInput(
            platform="discord", bot_token="private-test-token", backend_label="brand"
        )
        job = self.manager.start(value)
        with patch("auto_promotion.backends.discord.DiscordHTTP", return_value=FakeDiscord()):
            self.manager.run(job["id"], value, self.manager.cancellations[job["id"]])
        job = self.manager.get(job["id"])
        self.assertEqual(job["stage"], "connected")
        self.assertEqual(job["discord_user_id"], BOT)
        self.assertFalse(self.engine.accounts()[0]["writable"])
        self.assertIsNone(value.bot_token)
        public = (
            json.dumps(self.manager.list())
            + json.dumps(self.engine.accounts())
            + json.dumps(self.engine.store.rows("SELECT * FROM audit"))
        )
        self.assertNotIn("private-test-token", public)

    def test_api_catalog_is_private_and_invalid_bot_form_never_echoes_token(self):
        app = create_app(db_path=self.root / "api.db", token="test-key")
        headers = {"authorization": "Bearer test-key"}
        status, body, _ = asyncio.run(
            request(
                app,
                "/api/connections",
                "POST",
                {
                    "platform": "discord",
                    "bot_token": "private-test-token",
                    "username": "unexpected",
                },
                headers,
            )
        )
        self.assertEqual(status, 422)
        self.assertNotIn("private-test-token", json.dumps(body))
        account = app.state.engine.add_account(
            AccountInput(
                platform="discord",
                label="test",
                backend_label="test",
                mode="simulation",
                min_interval=0,
            )
        )
        path = f"/api/accounts/{account['id']}/targets"
        self.assertEqual(asyncio.run(request(app, path))[0], 401)
        status, body, _ = asyncio.run(request(app, path, headers=headers))
        self.assertEqual(status, 200)
        self.assertEqual(body["targets"][0]["id"], "123456789012345678")
