"""Opt-in, offline Chromium contracts: no real platform accounts or network."""

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

from auto_promotion.backends.browser import BrowserBackend


@unittest.skipUnless(
    os.getenv("AP_TEST_BROWSER_BACKENDS") == "1", "local Chromium tests are opt-in"
)
class XBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls.runtime = sync_playwright().start()
        cls.browser = cls.runtime.chromium.launch(
            executable_path=os.getenv("AP_BROWSER_EXECUTABLE")
            or cls.runtime.chromium.executable_path,
            headless=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.runtime.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(3000)
        self.posts = 0
        self.checks = 0
        self.relation = "456"
        self.author = "brand"
        self.already_liked = False
        self.duplicate_target = False
        self.duplicate_reply_control = False
        self.reply_as_dialog = False
        self.nested_reply_dialog = False
        self.duplicate_modal_editor = False
        self.late_modal_after_input = False
        self.with_media = False
        self.post_limited = False
        self.reply_delay_ms = 0
        self.reply_control_delay_ms = 0
        self.pending_image = False
        self.pending_routes = []
        self.editor_requires_keyboard = False
        self.expected_text = "exact approved text"
        self.post_dropped = False
        self.context.route("**/*", self.route)
        self.backend = BrowserBackend(
            "x", "local-x", self.context, self.page, {"identity": "brand"}
        )
        self.receipts = []

    def authorize(self):
        self.checks += 1

    def tearDown(self):
        for route in self.pending_routes:
            route.abort()

    def payload(self, action="reply"):
        return {
            "payload": {
                "action": action,
                "target": "https://x.com/target/status/456",
                "text": self.expected_text,
                "media": [],
            }
        }

    def route(self, route):
        parsed = urlparse(route.request.url)
        if parsed.path == "/pending-image":
            # Keep an unrelated asset pending while the target DOM is usable.
            self.pending_routes.append(route)
            return
        if parsed.path == "/CreateTweet":
            self.posts += 1
            if self.post_dropped:
                route.abort()
                return
            if self.post_limited:
                route.fulfill(status=429, content_type="application/json", body="{}")
                return
            route.fulfill(
                content_type="application/json",
                body=json.dumps(
                    {
                        "data": {
                            "create_tweet": {
                                "tweet_results": {
                                    "result": {
                                        "rest_id": "123",
                                        "legacy": {"in_reply_to_status_id_str": self.relation},
                                    }
                                }
                            }
                        }
                    }
                ),
            )
            return
        if parsed.hostname != "x.com" or route.request.method != "GET":
            raise AssertionError("Unexpected request in offline fixture")
        own = parsed.path.endswith("/123")
        identifier, author = ("123", self.author) if own else ("456", "target")
        text = self.expected_text if own else "target post"
        done = "unlike" if self.already_liked else "like"
        extra_reply_control = (
            '<button data-testid="reply" onclick="throw new Error(\'wrong reply control\')">Other reply</button>'
            if self.duplicate_reply_control
            else ""
        )
        reply_destination = "#reply-dialog" if self.reply_as_dialog else "#reply-compose"
        inline_action = (
            "throw new Error('wrong composer')"
            if self.reply_as_dialog
            else "fetch('/CreateTweet', {method:'POST'})"
        )
        modal_editor = '<div contenteditable="true" data-testid="tweetTextarea_0"></div>'
        if self.duplicate_modal_editor:
            modal_editor += '<div contenteditable="true" data-testid="tweetTextarea_0"></div>'
        media_input = '<input type="file" multiple>' if self.with_media else ""
        modal_body = (
            modal_editor
            + media_input
            + "<button data-testid=\"tweetButton\" onclick=\"fetch('/CreateTweet', {method:'POST'})\">Post</button>"
        )
        if self.nested_reply_dialog:
            modal_body = '<div role="dialog">' + modal_body + "</div>"
        dialog_compose = (
            ('<div id="reply-dialog" role="dialog" hidden>' + modal_body + "</div>")
            if (self.reply_as_dialog or self.late_modal_after_input)
            else ""
        )
        late_open = (
            "document.querySelector('#reply-dialog').hidden=false;"
            if self.late_modal_after_input
            else ""
        )
        article = f"""
        <article data-testid="tweet">
          <div data-testid="User-Name">Name @{author}</div>
          <a href="/{author}/status/{identifier}"><time>today</time></a>
          <div data-testid="tweetText">{text}</div>
          {'<div data-testid="tweetPhoto"></div>' if own and self.with_media else ""}
          <button data-testid="{done}" onclick="this.dataset.testid='unlike'">Like</button>
          <button data-testid="reply" style="visibility:{"hidden" if self.reply_control_delay_ms else "visible"}" onclick="setTimeout(()=>document.querySelector('{reply_destination}').hidden=false,{self.reply_delay_ms})">Reply</button>
          {extra_reply_control}
        </article>"""
        route.fulfill(
            content_type="text/html; charset=utf-8",
            body=f"""<button data-testid="SideNav_AccountSwitcher_Button">Brand @brand</button>
            {article}{article if self.duplicate_target else ""}
            {'<img src="/pending-image">' if self.pending_image else ""}
            <div id="reply-compose" {" " if self.reply_as_dialog else "hidden"}>
              <div contenteditable="true" data-testid="tweetTextarea_0" onkeydown="document.querySelector('[data-testid=tweetButtonInline]').disabled=false;{late_open}"></div>
              {media_input}
              <button data-testid="tweetButtonInline" {"disabled" if self.editor_requires_keyboard else ""} onclick="{inline_action}">Post</button>
            </div>{dialog_compose}
            <script>setTimeout(()=>document.querySelectorAll('article [data-testid=reply]').forEach(
              button=>button.style.visibility='visible'),{self.reply_control_delay_ms})</script>""",
        )

    def test_reply_uses_one_submission_and_verifies_target_and_actual_author(self):
        result = self.backend.run("execute", self.payload(), self.receipts.append, self.authorize)
        self.assertEqual((self.posts, self.checks), (1, 1))
        self.assertTrue(result["verified"])
        self.assertTrue(self.receipts[0]["target_verified"])
        self.assertEqual(result["external_reference"], "https://x.com/brand/status/123")

    def test_identity_timeout_is_rechecked_once_before_submission(self):
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        original = self.backend.check_identity
        calls = []

        def flaky(*args, **kwargs):
            calls.append(True)
            if len(calls) == 1:
                raise PlaywrightTimeoutError("synthetic identity delay")
            return original(*args, **kwargs)

        with patch.object(self.backend, "check_identity", side_effect=flaky):
            result = self.backend.run(
                "execute", self.payload(), self.receipts.append, self.authorize
            )
        self.assertEqual(result["status"], "confirmed")
        self.assertGreaterEqual(len(calls), 3)
        self.assertEqual((self.posts, self.checks), (1, 1))

    def test_identity_timeout_followed_by_challenge_never_retries_or_submits(self):
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        calls = []

        def challenge(*args, **kwargs):
            calls.append(True)
            self.page.evaluate("""() => { const node=document.createElement('div');
              node.dataset.testid='arkoseChallenge'; node.textContent='Verify'; document.body.append(node); }""")
            raise PlaywrightTimeoutError("synthetic identity delay")

        with patch.object(self.backend, "check_identity", side_effect=challenge):
            result = self.backend.run(
                "execute", self.payload(), self.receipts.append, self.authorize
            )
        self.assertEqual(result["status"], "failed_known")
        self.assertEqual(result["account_block_reason"], "x_challenge")
        self.assertEqual(len(calls), 1)
        self.assertEqual((self.posts, self.checks), (0, 0))

    def test_high_frequency_browser_read_is_partial_and_never_submits(self):
        from auto_promotion.models import epoch

        when = "2026-10-22T06:00:00Z"

        def search(route):
            route.fulfill(
                content_type="text/html; charset=utf-8",
                body=f'''<button data-testid="SideNav_AccountSwitcher_Button">Brand @brand</button>
                <div data-testid="primaryColumn"><article data-testid="tweet"><a href="/target/status/456"><time datetime="{when}">now</time></a>
                <div data-testid="tweetText">Looking for a useful video tool</div></article></div>''',
            )

        self.context.route(re.compile(r"https://x\.com/search\?.*"), search)
        at = epoch(when)
        result = self.backend.run(
            "discover",
            {
                "high_frequency": True,
                "read_only": True,
                "query": "video tool",
                "lower_at": at - 100,
                "upper_at": at + 100,
                "cursor": None,
            },
            self.receipts.append,
            self.authorize,
        )
        self.assertEqual(result["candidates"][0]["id"], "456")
        self.assertEqual(result["candidates"][0]["source_event_at"], at)
        self.assertIn("video tool", result["candidates"][0]["query_visible_text"])
        self.assertFalse(result["coverage_complete"])
        self.assertEqual((self.posts, self.checks), (0, 0))

    def test_reply_to_another_parent_stays_unknown(self):
        self.relation = "999"
        result = self.backend.run("execute", self.payload(), self.receipts.append, self.authorize)
        self.assertEqual(self.posts, 1)
        self.assertEqual(result["status"], "unknown")

    def test_extra_reply_control_does_not_prevent_target_reply(self):
        self.duplicate_reply_control = True
        result = self.backend.run("execute", self.payload(), self.receipts.append, self.authorize)
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual((self.posts, self.checks), (1, 1))

    def test_reply_control_that_renders_after_target_is_waited_for(self):
        self.reply_control_delay_ms = 600
        result = self.backend.run("execute", self.payload(), self.receipts.append, self.authorize)
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual((self.posts, self.checks), (1, 1))

    def test_visible_modal_reply_takes_priority_over_inline_composer(self):
        self.reply_as_dialog = True
        result = self.backend.run("execute", self.payload(), self.receipts.append, self.authorize)
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual((self.posts, self.checks), (1, 1))

    def test_nested_dialogs_with_one_editor_use_the_modal_once(self):
        self.reply_as_dialog = True
        self.nested_reply_dialog = True
        result = self.backend.run("execute", self.payload(), self.receipts.append, self.authorize)
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual((self.posts, self.checks), (1, 1))

    def test_two_visible_modal_editors_never_submit(self):
        self.reply_as_dialog = True
        self.nested_reply_dialog = True
        self.duplicate_modal_editor = True
        result = self.backend.run("execute", self.payload(), self.receipts.append, self.authorize)
        self.assertEqual(result["status"], "failed_known")
        self.assertEqual(result["reason"], "BrowserReplyComposerAmbiguous")
        self.assertTrue(result["confirmed_not_submitted"])
        self.assertEqual((self.posts, self.checks), (0, 0))

    def test_nested_modal_with_media_keeps_same_editor_and_submits_once(self):
        self.reply_as_dialog = True
        self.nested_reply_dialog = True
        self.with_media = True
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "fixture.png"
            image.write_bytes(b"synthetic image bytes")
            payload = self.payload()
            payload["payload"]["media"] = [{"path": str(image)}]
            result = self.backend.run("execute", payload, self.receipts.append, self.authorize)
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual((self.posts, self.checks), (1, 1))

    def test_late_modal_after_typing_does_not_submit_the_background_reply(self):
        self.late_modal_after_input = True
        result = self.backend.run("execute", self.payload(), self.receipts.append, self.authorize)
        self.assertEqual(result["status"], "failed_known")
        self.assertTrue(result["confirmed_not_submitted"])
        self.assertEqual((self.posts, self.checks), (0, 0))

    def test_delayed_reply_composer_is_ready_before_filling(self):
        self.reply_delay_ms = 1200
        result = self.backend.run("execute", self.payload(), self.receipts.append, self.authorize)
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual((self.posts, self.checks), (1, 1))

    def test_opening_modal_is_selected_before_touching_background_inline_editor(self):
        self.reply_as_dialog = True
        self.reply_delay_ms = 2000
        result = self.backend.run("execute", self.payload(), self.receipts.append, self.authorize)
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual((self.posts, self.checks), (1, 1))

    def test_unrelated_pending_asset_does_not_block_target_or_receipt(self):
        self.pending_image = True
        result = self.backend.run("execute", self.payload(), self.receipts.append, self.authorize)
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual((self.posts, self.checks), (1, 1))

    def test_keyboard_editor_retains_exact_approved_unicode_before_one_submission(self):
        self.editor_requires_keyboard = True
        self.expected_text = "图生视频 — let’s test one frame."
        result = self.backend.run("execute", self.payload(), self.receipts.append, self.authorize)
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual((self.posts, self.checks), (1, 1))

    def test_url_username_cannot_replace_actual_author_verification(self):
        self.author = "another_user"
        result = self.backend.run("execute", self.payload(), self.receipts.append, self.authorize)
        self.assertFalse(result["verified"])

    def test_existing_like_is_read_back_without_a_write(self):
        self.already_liked = True
        result = self.backend.run(
            "execute", self.payload("like"), self.receipts.append, self.authorize
        )
        self.assertTrue(result["verified"])
        self.assertEqual((self.posts, self.checks), (0, 0))

    def test_optimistic_like_is_not_confirmed_after_reload(self):
        result = self.backend.run(
            "execute", self.payload("like"), self.receipts.append, self.authorize
        )
        self.assertEqual(self.checks, 1)
        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "unknown")

    def test_ambiguous_target_never_submits(self):
        self.duplicate_target = True
        result = self.backend.run("execute", self.payload(), self.receipts.append, self.authorize)
        self.assertEqual(result["status"], "failed_known")
        self.assertTrue(result["confirmed_not_submitted"])
        self.assertEqual((self.posts, self.checks), (0, 0))

    def test_rate_limit_returns_cooldown_without_resubmission(self):
        self.post_limited = True
        result = self.backend.run("execute", self.payload(), self.receipts.append, self.authorize)
        self.assertTrue(result["rate_limited"])
        self.assertEqual(result["status"], "failed_known")
        self.assertTrue(result["confirmed_rejected"])
        self.assertEqual(self.posts, 1)

    def test_dropped_submission_response_records_step_and_submission_boundary(self):
        self.post_dropped = True
        expect_response = self.page.expect_response
        with patch.object(
            self.page,
            "expect_response",
            side_effect=lambda predicate, timeout: expect_response(predicate, timeout=500),
        ):
            result = self.backend.run(
                "execute", self.payload(), self.receipts.append, self.authorize
            )
        self.assertEqual(self.posts, 1)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["execution_step"], "submission_response")
        self.assertTrue(result["submission_started"])
