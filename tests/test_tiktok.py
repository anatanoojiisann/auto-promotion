"""Offline contract and workflow tests. No real TikTok authorization or publication."""

import asyncio
import hashlib
import hmac
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse

from test_api import request
from test_engine import iso

from auto_promotion.adapters import AdapterRegistry, SimulationAdapter
from auto_promotion.api import create_app
from auto_promotion.backends.common import BackendError, read_profile, save_profile
from auto_promotion.backends.tiktok import (
    TikTokBackend,
    TikTokError,
    TikTokHTTP,
    media_url,
    validate_item,
)
from auto_promotion.campaigns import Campaigns
from auto_promotion.core import Conflict, Engine
from auto_promotion.models import (
    AccountInput,
    AccountProfile,
    CampaignInput,
    ItemInput,
    OpportunityInput,
    PoolInput,
    TaskInput,
    TikTokPostOptions,
)
from auto_promotion.native_bridge import dispatch
from auto_promotion.planner import content_key
from auto_promotion.pools import Pools
from auto_promotion.store import Store
from auto_promotion.task_tools import TaskTools
from auto_promotion.tiktok import (
    TikTokAppInput,
    TikTokAuthorizeInput,
    TikTokManager,
    verify_signature,
)
from auto_promotion.tiktok_gateway import create_gateway

NOW = 1800000000
SCOPES = (
    "video.publish,video.list,user.info.basic,user.info.username,comment.list,comment.list.manage"
)


class OfficialAPI:
    """Small stateful API fixture with the documented wire format."""

    def __init__(self):
        self.calls = []
        self.writes = []
        self.grants = SCOPES
        self.subject = "owned-account"
        self.username = "brand"
        self.owned = True
        self.publish_status = "PROCESSING_DOWNLOAD"
        self.bad_read_after_write = False
        self.comments = {
            "456": {
                "comment_id": "456",
                "video_id": "123",
                "text": "question",
                "owner": False,
                "status": "PUBLIC",
                "liked": False,
                "create_time": str(NOW),
            }
        }

    def __call__(self, token=""):
        return self

    def request(self, path, *, params=None, body=None):
        self.calls.append((path, params, body))
        if path.startswith("tt_user/oauth2/"):
            return {
                "access_token": "secret-access",
                "refresh_token": "secret-refresh-rotated",
                "open_id": self.subject,
                "expires_in": 86400,
                "refresh_token_expires_in": 30000000,
                "scope": self.grants,
            }
        if path == "tt_user/token_info/get/":
            return {"app_id": "12345", "creator_id": self.subject, "scope": self.grants}
        if path == "business/get/":
            return {"username": self.username, "display_name": "Brand", "followers_count": 12}
        if path == "business/video/settings/":
            return {
                "privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
                "comment_disabled": False,
                "duet_disabled": True,
                "stitch_disabled": True,
                "max_video_post_duration_sec": 600,
            }
        if path == "business/video/list/":
            return {
                "videos": (
                    [
                        {
                            "item_id": "123",
                            "caption": "hello",
                            "share_url": "https://www.tiktok.com/@brand/video/123",
                            "create_time": str(NOW),
                        }
                    ]
                    if self.owned
                    else []
                ),
                "has_more": False,
                "cursor": 1234,
            }
        if path == "business/publish/status/":
            return {
                "status": self.publish_status,
                "post_ids": ["123"] if self.publish_status == "PUBLISH_COMPLETE" else [],
            }
        if path == "business/webhook/update/":
            return {k: body[k] for k in ("app_id", "event_type", "callback_url")}
        if path == "business/comment/list/":
            if self.bad_read_after_write and self.writes:
                raise TikTokError("TikTokTransportUnavailable")
            ids = params.get("comment_ids", self.comments.keys())
            return {
                "comments": [self.comments[k] for k in ids if k in self.comments],
                "cursor": 8,
                "has_more": False,
            }
        self.writes.append((path, body))
        if path in {"business/video/publish/", "business/photo/publish/"}:
            return {"share_id": "v_pub_url~receipt"}
        if path in {"business/comment/create/", "business/comment/reply/create/"}:
            self.comments["789"] = {
                "comment_id": "789",
                "video_id": "123",
                "text": body["text"],
                "owner": True,
                "parent_comment_id": body.get("comment_id"),
                "create_time": str(NOW),
                "status": "PUBLIC",
            }
            return {"comment_id": "789"}
        if path == "business/comment/like/":
            self.comments[body["comment_id"]]["liked"] = body["action"] == "LIKE"
        elif path == "business/comment/hide/":
            self.comments[body["comment_id"]]["status"] = (
                "HIDDEN" if body["action"] == "HIDE" else "PUBLIC"
            )
        elif path == "business/comment/delete/":
            self.comments.pop(body["comment_id"], None)
        else:
            raise AssertionError(path)
        return {}


class TikTokTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"AP_ACCOUNTS_DIR": str(self.root / "profiles")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.api = OfficialAPI()
        self.engine = Engine(
            Store(self.root / "test.db"),
            AdapterRegistry(self.root, overrides={"tiktok": SimulationAdapter()}),
            clock=lambda: NOW,
        )
        self.manager = TikTokManager(self.engine, http_factory=self.api)
        self.config = {
            "client_id": "12345",
            "client_secret": "secret-app",
            "redirect_uri": "https://callback.example/oauth/tiktok/",
            "authorization_url": "https://www.tiktok.com/v2/auth/authorize?"
            + urlencode(
                {
                    "client_key": "12345",
                    "response_type": "code",
                    "scope": SCOPES,
                    "redirect_uri": "https://callback.example/oauth/tiktok/",
                }
            ),
            "media_url_prefixes": ["https://cdn.example/tiktok/"],
        }
        self.manager.save_app(TikTokAppInput.model_validate(self.config))
        self.profile = {
            "identity": "owned-account",
            "username": "brand",
            "client_id": "12345",
            "access_token": "secret-access",
            "refresh_token": "secret-refresh",
            "expires_at": NOW + 80000,
            "refresh_expires_at": NOW + 1000000,
            "scopes": SCOPES.split(","),
        }
        save_profile("tiktok", "brand", self.profile)

    def backend(self):
        return TikTokBackend("brand", http_factory=self.api, clock=lambda: NOW)

    def item(self, **changes):
        return {
            "action": "reply",
            "target": "owned-account",
            "target_owned": True,
            "text": "hello",
            "title": "",
            "media": [],
            "tiktok_video_id": "123",
            "tiktok_comment_id": "456",
            **changes,
        }

    def video(self, **changes):
        return self.item(
            action="publish",
            tiktok_video_id="",
            tiktok_comment_id="",
            tiktok_options=TikTokPostOptions().model_dump(),
            media=[
                {
                    "media_type": "VIDEO",
                    "mime_type": "video/mp4",
                    "source_url": "https://cdn.example/tiktok/video.mp4",
                }
            ],
            **changes,
        )

    def execute(self, item):
        self.receipt = {}

        def checkpoint(value):
            self.receipt.update(value)

        return self.backend().run("execute", {"payload": item}, checkpoint, lambda: None)

    def callback(self, username="brand"):
        value = self.manager.authorize(
            TikTokAuthorizeInput(backend_label="connected", username=username)
        )
        state = parse_qs(urlparse(value["authorization_url"]).query)["state"][0]
        return (
            self.config["redirect_uri"]
            + "?"
            + urlencode({"auth_code": "secret-code", "state": state})
        )

    def test_oauth_registers_verified_account_without_enabling_execution(self):
        result = self.manager.complete(self.callback())
        account = self.engine.account(result["account_id"])
        self.assertEqual(account["platform"], "tiktok")
        self.assertEqual(account["owned_target_id"], "owned-account")
        self.assertFalse(account["writable"])
        self.assertEqual(self.api.writes, [])

    def test_oauth_is_one_time_including_code_exchange_failure(self):
        callback = self.callback()
        self.manager.complete(callback)
        with self.assertRaises(Conflict):
            self.manager.complete(callback)
        self.assertEqual(sum(p == "tt_user/oauth2/token/" for p, _, _ in self.api.calls), 1)

    def test_oauth_rejects_wrong_state_and_redirect(self):
        callback = self.callback()
        for bad in (
            callback.replace("state=", "state=wrong"),
            callback.replace("callback.example", "attacker.example"),
        ):
            with self.assertRaises(Conflict):
                self.manager.complete(bad)
        self.assertEqual(self.api.calls, [])

    def test_oauth_rejects_expired_session(self):
        callback = self.callback()
        self.manager.clock = lambda: NOW + 601
        with self.assertRaises(Conflict):
            self.manager.complete(callback)

    def test_wrong_username_does_not_save_or_register(self):
        with self.assertRaisesRegex(BackendError, "TikTokIdentityMismatch"):
            self.manager.complete(self.callback("different"))
        self.assertEqual(self.engine.accounts(), [])

    def test_public_state_and_database_have_no_tokens_or_auth_code(self):
        self.manager.complete(self.callback())
        public = json.dumps(self.manager.status()) + json.dumps(self.engine.accounts())
        public += json.dumps(self.engine.store.rows("SELECT * FROM audit")) + json.dumps(
            self.engine.store.rows("SELECT * FROM tiktok_oauth")
        )
        for secret in ("secret-access", "secret-refresh", "secret-app", "secret-code"):
            self.assertNotIn(secret, public)

    def test_refresh_rotates_token_and_persists_expiry(self):
        save_profile("tiktok", "brand", {**self.profile, "expires_at": NOW})
        self.backend()
        saved = read_profile("tiktok", "brand")
        self.assertEqual(saved["refresh_token"], "secret-refresh-rotated")
        self.assertEqual(saved["expires_at"], NOW + 86400)

    def test_refresh_cannot_change_bound_identity(self):
        save_profile("tiktok", "brand", {**self.profile, "expires_at": NOW})
        self.api.subject = "other"
        with self.assertRaisesRegex(BackendError, "TikTokIdentityMismatch"):
            self.backend()
        self.assertEqual(read_profile("tiktok", "brand")["identity"], "owned-account")

    def test_expired_refresh_requires_new_authorization(self):
        save_profile(
            "tiktok", "brand", {**self.profile, "expires_at": NOW, "refresh_expires_at": NOW - 1}
        )
        with self.assertRaisesRegex(BackendError, "TikTokAuthorizationRequired"):
            self.backend()
        self.assertEqual(self.api.calls, [])

    def test_missing_publish_permission_blocks_before_write(self):
        self.api.grants = "user.info.basic,video.list"
        with self.assertRaisesRegex(BackendError, "TikTokPermissionDenied"):
            self.execute(self.video())
        self.assertEqual(self.api.writes, [])

    def test_forged_permission_inspector_subject_is_rejected(self):
        self.api.subject = "other"
        with self.assertRaisesRegex(BackendError, "TikTokIdentityMismatch"):
            self.backend().run("health", {}, lambda _: None, lambda: None)

    def test_video_publish_is_pending_until_status_and_owned_post_are_visible(self):
        result = self.execute(self.video())
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(self.receipt["publish_id"], "v_pub_url~receipt")
        self.api.publish_status = "PUBLISH_COMPLETE"
        verified = self.backend().run(
            "verify",
            {"payload": self.video(), "receipt": self.receipt},
            lambda _: None,
            lambda: self.fail("verify must not authorize a write"),
        )
        self.assertEqual(verified["status"], "confirmed")
        self.assertEqual(verified["published_at"], NOW)
        self.assertEqual(len(self.api.writes), 1)

    def test_platform_failure_is_terminal_without_a_second_post(self):
        self.api.publish_status = "FAILED"
        result = self.execute(self.video())
        self.assertEqual(result["status"], "failed_known")
        self.assertTrue(result["verified"])
        self.assertEqual(len(self.api.writes), 1)

    def test_photo_payload_does_not_mix_video_fields(self):
        item = self.video()
        item["media"] = [
            {
                "media_type": "IMAGE",
                "mime_type": "image/jpeg",
                "source_url": "https://cdn.example/tiktok/a.jpg",
            }
        ]
        item["tiktok_options"]["privacy_level"] = "PUBLIC_TO_EVERYONE"
        item["title"] = "photo title"
        self.execute(item)
        path, body = self.api.writes[0]
        self.assertEqual(path, "business/photo/publish/")
        self.assertEqual(body["photo_images"], ["https://cdn.example/tiktok/a.jpg"])
        self.assertEqual(body["post_info"]["title"], "photo title")
        self.assertNotIn("disable_duet", body["post_info"])
        self.assertNotIn("video_url", body)

    def test_url_prefix_boundary_cannot_be_spoofed(self):
        for url in (
            "https://cdn.example.attacker/tiktok/v.mp4",
            "https://cdn.example/tiktok-other/v.mp4",
            "http://cdn.example/tiktok/v.mp4",
            "https://secret@cdn.example/tiktok/v.mp4",
        ):
            with self.assertRaises(BackendError):
                media_url(url, self.config["media_url_prefixes"])

    def test_non_owned_video_or_target_cannot_receive_interaction(self):
        for item in (self.item(target="another-account"), self.item(target_owned=False)):
            with self.assertRaises(BackendError):
                self.execute(item)
        self.api.owned = False
        with self.assertRaisesRegex(BackendError, "TikTokOwnedVideoRequired"):
            self.execute(self.item())
        self.assertEqual(self.api.writes, [])

    def test_foreign_video_comment_is_not_imported_as_requested_video(self):
        self.api.comments["456"]["video_id"] = "999"
        result = self.backend().run(
            "content", {"account_id": "synthetic", "video_id": "123"}, lambda _: None, lambda: None
        )
        self.assertEqual(result["content_items"], [])
        self.assertEqual(result.get("reason"), "TikTokCommentVideoMismatch")

    def test_comment_read_permalink_must_bind_requested_video_and_parent(self):
        for url in [
            "https://www.tiktok.com/@brand/video/999?comment_id=456",
            "https://www.tiktok.com/@brand/video/123?comment_id=999",
            "https://www.tiktok.com/business-suite/comments?video_id=999&comment_id=456",
        ]:
            with self.subTest(url=url), self.assertRaises(BackendError):
                self.backend().run(
                    "content",
                    {
                        "account_id": "synthetic",
                        "video_id": "123",
                        "comment_id": "456",
                        "comment_url": url,
                    },
                    lambda _: None,
                    lambda: None,
                )
        self.assertEqual(self.api.writes, [])

    def test_management_receipt_cannot_confirm_a_different_comment(self):
        for action in ["like", "unlike", "hide_comment", "unhide_comment", "delete_comment"]:
            with self.subTest(action=action):
                result = self.backend().verify(
                    {
                        "payload": self.item(action=action, text=""),
                        "receipt": {"comment_id": "999", "mutation_accepted": True},
                    }
                )
                self.assertFalse(result["verified"])
                self.assertEqual(result["reason"], "TikTokReceiptMismatch")
        self.assertIn("456", self.api.comments)
        self.assertEqual(self.api.writes, [])

    def test_receipt_id_must_be_native_and_top_level_comment_cannot_be_a_reply(self):
        for identifier in ["None", "https://private.example/secret", "", "0"]:
            with self.subTest(identifier=identifier):
                result = self.backend().verify(
                    {"payload": self.item(), "receipt": {"comment_id": identifier}}
                )
                self.assertFalse(result["verified"])
                self.assertEqual(
                    result["reason"],
                    "TikTokReceiptMismatch" if identifier else "no_durable_receipt",
                )
        self.api.comments["789"] = {
            "comment_id": "789",
            "video_id": "123",
            "text": "hello",
            "owner": True,
            "parent_comment_id": "456",
        }
        result = self.backend().verify(
            {
                "payload": self.item(action="comment", tiktok_comment_id=""),
                "receipt": {"comment_id": "789"},
            }
        )
        self.assertFalse(result["verified"])

    def test_permissive_caller_flags_cannot_enable_external_video_or_dm(self):
        from auto_promotion.tiktok import TikTokInteractionInput

        a = self.engine.add_account(
            AccountInput(
                platform="tiktok",
                label="local-check",
                backend_label="brand",
                mode="simulation",
                min_interval=0,
            )
        )

        def check(**kw):
            return self.manager.check_interaction(a["id"], TikTokInteractionInput(**kw))

        missing = check(action="reply", video_id="123", target_owned=True)
        self.assertEqual(missing["reason"], "TikTokParentCommentRequired")
        external = check(
            action="reply", video_id="123", parent_comment_id="456", target_owned=False
        )
        self.assertEqual(external["reason"], "TikTokDesktopVideoCapabilityUnverified")
        self.assertFalse(external["execution_ready"])
        valid = check(action="comment", video_id="123", target_owned=True)
        self.assertTrue(valid["input_valid"])
        self.assertFalse(valid["execution_ready"])
        dm = check(action="send_message")
        self.assertEqual(dm["state"], "not_applicable")
        self.assertNotEqual(dm["reason"], "TikTokParentCommentRequired")
        self.assertEqual(self.api.writes, [])

    def test_reply_verifies_owner_and_parent_and_uses_string_ids(self):
        result = self.execute(self.item())
        self.assertEqual(result["status"], "confirmed")
        self.assertIn("comment_id=789", result["external_reference"])
        self.assertEqual(self.api.writes[0][1]["comment_id"], "456")
        self.api.comments["789"]["parent_comment_id"] = "other"
        result = self.backend().verify({"payload": self.item(), "receipt": self.receipt})
        self.assertFalse(result["verified"])

    def test_like_and_hide_have_documented_action_fields(self):
        for action, expected in (
            ("like", "LIKE"),
            ("unlike", "UNLIKE"),
            ("hide_comment", "HIDE"),
            ("unhide_comment", "UNHIDE"),
        ):
            result = self.execute(self.item(action=action, text=""))
            self.assertEqual(result["status"], "confirmed")
            self.assertEqual(self.api.writes[-1][1]["action"], expected)

    def test_delete_only_own_comment(self):
        with self.assertRaisesRegex(BackendError, "TikTokCanOnlyDeleteOwnComment"):
            self.execute(self.item(action="delete_comment", text=""))
        self.assertEqual(self.api.writes, [])
        self.api.comments["456"]["owner"] = True
        result = self.execute(self.item(action="delete_comment", text=""))
        self.assertEqual(result["status"], "confirmed")

    def test_native_bridge_never_replays_after_post_readback_fails(self):
        self.api.bad_read_after_write = True
        backend = self.backend()
        req = {
            "platform": "tiktok",
            "account": "brand",
            "operation": "execute",
            "payload": {"item_id": "one-item", "payload": self.item()},
        }
        with (
            patch("auto_promotion.native_bridge.verify_claim"),
            patch(
                "auto_promotion.backends.tiktok.call",
                side_effect=lambda label, operation, payload, checkpoint, authorize: backend.run(
                    operation, payload, checkpoint, authorize
                ),
            ),
        ):
            result = dispatch(req)
            second = dispatch(req)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(second["status"], "unknown")
        self.assertEqual(len(self.api.writes), 1)

    def test_native_comment_acceptance_survives_api403_readback(self):
        backend = self.backend()
        original = self.api.request

        def blocked(path, **kwargs):
            if path == "business/comment/list/" and self.api.writes:
                raise TikTokError("TikTokAccessDenied", retry_after=120)
            return original(path, **kwargs)

        self.api.request = blocked
        req = {
            "platform": "tiktok",
            "account": "brand",
            "operation": "execute",
            "payload": {
                "item_id": "access-denied-item",
                "attempt_id": "attempt",
                "task_id": "task",
                "payload": self.item(),
            },
        }
        with (
            patch("auto_promotion.native_bridge.verify_claim"),
            patch("auto_promotion.backends.tiktok.TikTokBackend", return_value=backend),
        ):
            result = dispatch(req)
            second = dispatch(req)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["comment_id"], "789")
        self.assertEqual(result["reason"], "TikTokAccessDenied")
        self.assertIn("tiktok_receipt_binding", result)
        self.assertEqual(second["comment_id"], "789")
        self.assertEqual(len(self.api.writes), 1)

    def test_manager_comment_binding_blocks_before_provider_and_recovers(self):
        from auto_promotion.tiktok import TikTokReadInput

        account = self.engine.add_account(
            AccountInput(platform="tiktok", label="reader", backend_label="brand", mode="live")
        )
        with patch.object(self.engine, "call", return_value={"content_items": []}) as provider:
            with self.assertRaisesRegex(Conflict, "TikTokCommentVideoMismatch"):
                self.manager.read(
                    account["id"],
                    "comments",
                    TikTokReadInput(
                        video_id="123",
                        comment_url="https://www.tiktok.com/@target/video/999?comment_id=456",
                    ),
                )
            provider.assert_not_called()
            self.manager.read(
                account["id"],
                "comments",
                TikTokReadInput(
                    video_id="123",
                    comment_url="https://www.tiktok.com/@target/video/123?comment_id=456",
                ),
            )
            self.assertEqual(provider.call_args.args[2]["comment_id"], "456")

    def test_read_video_first_page_omits_zero_timestamp_cursor(self):
        self.backend().run("targets", {"cursor": 0, "limit": 30}, lambda _: None, lambda: None)
        _, params, _ = self.api.calls[-1]
        self.assertNotIn("cursor", params)
        self.assertEqual(params["max_count"], 20)

    def test_content_ids_include_video_account_and_comment(self):
        row = {
            "platform": "tiktok",
            "target": "owned-account",
            "external_id": "456",
            "tiktok_video_id": "123",
            "tiktok_comment_id": "456",
        }
        self.assertNotEqual(content_key(row), content_key({**row, "tiktok_comment_id": "789"}))
        self.assertNotEqual(content_key(row), content_key({**row, "target": "another-account"}))

    def test_model_rejects_unknown_options_and_unsafe_comment_fields(self):
        with self.assertRaises(ValueError):
            TikTokPostOptions(undocumented=True)
        with self.assertRaises(BackendError):
            validate_item(self.item(reply_to=456))

    def test_api_errors_do_not_echo_secrets(self):
        app = create_app(db_path=self.root / "api.db", token="test-key")
        status, body, _ = asyncio.run(
            request(
                app,
                "/api/tiktok/app",
                "PUT",
                {"client_secret": "do-not-echo"},
                {"authorization": "Bearer test-key"},
            )
        )
        self.assertEqual(status, 422)
        self.assertNotIn("do-not-echo", json.dumps(body))
        status, body, _ = asyncio.run(request(app, "/api/tiktok/app"))
        self.assertEqual(status, 401)

    def test_task_preparation_does_not_execute_and_explicit_start_uses_frozen_ids(self):
        account = self.engine.add_account(
            AccountInput(
                platform="tiktok",
                mode="simulation",
                label="test",
                backend_label="simulation",
                min_interval=0,
            )
        )
        self.engine.check_account(account["id"])
        task = self.engine.create_task(
            TaskInput(
                name="TikTok workflow",
                account_ids=[account["id"]],
                policy={"starts_at": iso(NOW - 1), "ends_at": iso(NOW + 3600), "min_interval": 0},
            )
        )
        item = ItemInput(account_id=account["id"], scheduled_at=iso(NOW), **self.item())
        self.engine.set_items(task["id"], [item])
        self.engine.set_stop(False)
        self.assertEqual(self.engine.run_once(task["id"])["processed"], [])
        self.assertEqual(self.engine.store.rows("SELECT * FROM attempts"), [])
        started = self.engine.start_task(task["id"])
        frozen = started["items"][0]["payload"]
        self.assertEqual(frozen["tiktok_comment_id"], "456")
        self.assertEqual(frozen["tiktok_video_id"], "123")
        self.engine.run_once(task["id"])
        self.engine.run_once(task["id"])
        self.assertEqual(len(self.engine.store.rows("SELECT * FROM attempts")), 1)
        self.assertEqual(self.engine.task(task["id"])["items"][0]["status"], "confirmed")

    def test_pool_planning_and_evidence_preserve_tiktok_comment_target(self):
        account = self.engine.add_account(
            AccountInput(
                platform="tiktok",
                mode="simulation",
                label="pool account",
                backend_label="pool",
                min_interval=0,
            )
        )
        self.engine.check_account(account["id"])
        Pools(self.engine).set_profile(
            account["id"], AccountProfile(languages=["en"], topics=["video"])
        )
        pool = Pools(self.engine).save(PoolInput(name="TikTok pool", account_ids=[account["id"]]))
        campaigns = Campaigns(self.engine)
        imported = campaigns.import_content(
            [
                OpportunityInput(
                    platform="tiktok",
                    external_id="456",
                    target="owned-account",
                    target_owned=True,
                    tiktok_video_id="123",
                    tiktok_comment_id="456",
                    language="en",
                    context="question",
                    draft_text="hello",
                    permission="allowed",
                    permission_evidence="owned video",
                    permission_account_ids=[account["id"]],
                )
            ]
        )
        campaign = campaigns.create(
            CampaignInput(
                name="TikTok replies",
                pool_id=pool["id"],
                opportunity_ids=imported["ids"],
                policy={"starts_at": iso(NOW), "ends_at": iso(NOW + 86400), "min_interval": 0},
            )
        )
        plan = campaigns.plan(campaign["id"])
        assignment = plan["assignments"][0]
        self.assertIsNotNone(assignment["payload"], assignment["reasons"])
        self.assertEqual(assignment["payload"]["tiktok_comment_id"], "456")
        self.assertEqual(assignment["payload"]["tiktok_video_id"], "123")
        campaign = campaigns.materialize(campaign["id"])
        task_id = campaign["tasks"][0]["id"]
        campaigns.populate(task_id)
        exported = TaskTools(self.engine).evidence(task_id)
        self.assertEqual(exported["items"][0]["payload"]["tiktok_comment_id"], "456")
        self.assertEqual(self.engine.store.rows("SELECT * FROM attempts"), [])

    def test_foreign_platform_cannot_receive_tiktok_metadata(self):
        account = self.engine.add_account(
            AccountInput(
                platform="discord",
                mode="simulation",
                label="dc",
                backend_label="dc",
                min_interval=0,
            )
        )
        task = self.engine.create_task(
            TaskInput(
                name="wrong platform",
                account_ids=[account["id"]],
                policy={"starts_at": iso(NOW - 1), "ends_at": iso(NOW + 3600)},
            )
        )
        with self.assertRaisesRegex(Conflict, "TikTok"):
            self.engine.set_items(
                task["id"],
                [
                    ItemInput(
                        account_id=account["id"],
                        action="send_message",
                        target="123456789012345678",
                        text="hello",
                        scheduled_at=iso(NOW),
                        tiktok_comment_id="456",
                    )
                ],
            )

    def test_current_permissions_must_include_readback_capabilities(self):
        self.api.grants = "video.publish"
        with self.assertRaisesRegex(BackendError, "TikTokPermissionDenied"):
            self.execute(self.video())
        self.assertEqual(self.api.writes, [])

    def test_content_reader_omits_self_comments_and_preserves_page_cursor(self):
        self.api.comments["456"]["owner"] = True
        result = self.backend().run(
            "content", {"video_id": "123", "account_id": "acc-test"}, lambda _: None, lambda: None
        )
        self.assertEqual(result["content_items"], [])
        self.assertEqual(result["next_cursor"], 8)

    def test_no_public_post_id_cannot_be_reported_as_success(self):
        self.execute(self.video())
        self.api.publish_status = "PUBLISH_COMPLETE"
        self.api.owned = False
        result = self.backend().verify({"payload": self.video(), "receipt": self.receipt})
        self.assertFalse(result["verified"])
        self.assertEqual(len(self.api.writes), 1)

    def test_webhook_registration_uses_app_credentials_without_publishing(self):
        from auto_promotion.tiktok import TikTokWebhookInput

        result = self.manager.configure_webhook(TikTokWebhookInput(event_type="VIDEO"))
        self.assertEqual(result["callback_url"], "https://callback.example/webhooks/tiktok")
        _, _, body = self.api.calls[-1]
        self.assertEqual(body["secret"], "secret-app")
        self.assertNotIn("secret", result)
        self.assertEqual(self.api.writes, [])

    def test_literal_and_encoded_path_traversal_is_rejected(self):
        for path in (
            "../evil.mp4",
            "%2e%2e/evil.mp4",
            "%252e%252e/evil.mp4",
            "folder%5c..%5cevil.mp4",
        ):
            with self.assertRaises(BackendError):
                media_url("https://cdn.example/tiktok/" + path, self.config["media_url_prefixes"])

    def test_gateway_rejects_forged_webhooks_and_deduplicates_valid_ones(self):
        app = create_gateway(self.engine)
        event = {
            "event": "post.publish.complete",
            "client_key": "12345",
            "content": "secret-unneeded-content",
        }
        raw = json.dumps(event).encode()
        signature = hmac.new(
            b"secret-app", str(NOW).encode() + b"." + raw, hashlib.sha256
        ).hexdigest()
        self.assertEqual(asyncio.run(request(app, "/webhooks/tiktok", "POST", event))[0], 403)
        for _ in range(2):
            status = asyncio.run(
                request(
                    app,
                    "/webhooks/tiktok",
                    "POST",
                    event,
                    {"tiktok-signature": f"t={NOW},s={signature}"},
                )
            )[0]
            self.assertEqual(status, 200)
        rows = self.engine.store.rows("SELECT * FROM tiktok_events")
        self.assertEqual(len(rows), 1)
        self.assertNotIn("secret-unneeded-content", json.dumps(rows))
        self.assertEqual(self.api.writes, [])

    def test_signature_requires_fresh_timestamp_and_exact_body(self):
        raw = b'{"event":"x"}'
        signature = hmac.new(b"secret", str(NOW).encode() + b"." + raw, hashlib.sha256).hexdigest()
        header = f"t={NOW},s={signature}"
        self.assertTrue(verify_signature(raw, header, "secret", now=NOW))
        self.assertFalse(verify_signature(raw + b" ", header, "secret", now=NOW))
        self.assertFalse(verify_signature(raw, header, "secret", now=NOW + 301))
        self.assertFalse(verify_signature(raw, header + f",t={NOW}", "secret", now=NOW))


class TikTokTransportTests(unittest.TestCase):
    def test_http403_is_access_denied_not_rate_limit(self):
        error = HTTPError(
            "https://private.example/secret", 403, "PRIVATE", {"Retry-After": "120"}, None
        )
        with self.assertRaises(TikTokError) as caught:
            TikTokHTTP(
                opener=type(
                    "Opener", (), {"open": lambda *_a, **_k: (_ for _ in ()).throw(error)}
                )()
            ).request("business/video/list/")
        self.assertEqual(caught.exception.safe_code, "TikTokAccessDenied")
        self.assertFalse(caught.exception.limited)
        self.assertEqual(caught.exception.retry_after, 120)

    def test_transport_does_not_follow_redirect_or_leak_tokens_on_error(self):
        from unittest.mock import MagicMock

        opener = MagicMock()
        opener.open.side_effect = URLError("secret-access secret-app")
        with self.assertRaisesRegex(TikTokError, "TikTokTransportUnavailable") as raised:
            TikTokHTTP("secret-access", opener=opener).request(
                "business/get/", params={"business_id": "id"}
            )
        self.assertNotIn("secret-access", str(raised.exception))
        self.assertEqual(opener.open.call_count, 1)

    def test_transport_uses_access_token_header_and_json(self):
        from unittest.mock import MagicMock

        opener = MagicMock()
        opener.open.return_value = io.BytesIO(b'{"code":0,"data":{}}')
        TikTokHTTP("token", opener=opener).request(
            "business/comment/like/", body={"action": "LIKE"}
        )
        req = opener.open.call_args.args[0]
        self.assertEqual(req.get_header("Access-token"), "token")
        self.assertEqual(json.loads(req.data), {"action": "LIKE"})

    def test_http_rate_limit_preserves_retry_delay(self):
        from unittest.mock import MagicMock

        opener = MagicMock()
        opener.open.side_effect = HTTPError(
            "https://business-api.tiktok.com", 429, "limited", {"Retry-After": "120"}, None
        )
        with self.assertRaises(TikTokError) as raised:
            TikTokHTTP(opener=opener).request("business/get/")
        self.assertTrue(raised.exception.limited)
        self.assertEqual(raised.exception.retry_after, 120)


if __name__ == "__main__":
    unittest.main()
