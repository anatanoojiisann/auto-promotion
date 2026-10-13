"""Opt-in, offline Chromium contracts: no real platform accounts or network."""

import json
import os
import unittest
from urllib.parse import urlparse

from auto_promotion.backends.browser import BrowserBackend
from auto_promotion.backends.common import BackendError


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
        self.post_limited = False
        self.context.route("**/*", self.route)
        self.backend = BrowserBackend(
            "x", "local-x", self.context, self.page, {"identity": "brand"}
        )
        self.receipts = []

    def authorize(self):
        self.checks += 1

    def payload(self, action="reply"):
        return {
            "payload": {
                "action": action,
                "target": "https://x.com/target/status/456",
                "text": "exact approved text",
                "media": [],
            }
        }

    def route(self, route):
        parsed = urlparse(route.request.url)
        if parsed.path == "/CreateTweet":
            self.posts += 1
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
        text = "exact approved text" if own else "target post"
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
        dialog_compose = (
            """<div id="reply-dialog" role="dialog" hidden>
          <div contenteditable="true" data-testid="tweetTextarea_0"></div>
          <button data-testid="tweetButton" onclick="fetch('/CreateTweet', {method:'POST'})">Post</button>
        </div>"""
            if self.reply_as_dialog
            else ""
        )
        article = f"""
        <article data-testid="tweet">
          <div data-testid="User-Name">Name @{author}</div>
          <a href="/{author}/status/{identifier}"><time>today</time></a>
          <div data-testid="tweetText">{text}</div>
          <button data-testid="{done}" onclick="this.dataset.testid='unlike'">Like</button>
          <button data-testid="reply" onclick="document.querySelector('{reply_destination}').hidden=false">Reply</button>
          {extra_reply_control}
        </article>"""
        route.fulfill(
            content_type="text/html",
            body=f"""<button data-testid="SideNav_AccountSwitcher_Button">Brand @brand</button>
            {article}{article if self.duplicate_target else ""}
            <div id="reply-compose" {" " if self.reply_as_dialog else "hidden"}>
              <div contenteditable="true" data-testid="tweetTextarea_0"></div>
              <button data-testid="tweetButtonInline" onclick="{inline_action}">Post</button>
            </div>{dialog_compose}""",
        )

    def test_reply_uses_one_submission_and_verifies_target_and_actual_author(self):
        result = self.backend.run("execute", self.payload(), self.receipts.append, self.authorize)
        self.assertEqual((self.posts, self.checks), (1, 1))
        self.assertTrue(result["verified"])
        self.assertTrue(self.receipts[0]["target_verified"])
        self.assertEqual(result["external_reference"], "https://x.com/brand/status/123")

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

    def test_visible_modal_reply_takes_priority_over_inline_composer(self):
        self.reply_as_dialog = True
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
        with self.assertRaises(BackendError):
            self.backend.run("execute", self.payload(), self.receipts.append, self.authorize)
        self.assertEqual((self.posts, self.checks), (0, 0))

    def test_rate_limit_returns_cooldown_without_resubmission(self):
        self.post_limited = True
        result = self.backend.run("execute", self.payload(), self.receipts.append, self.authorize)
        self.assertTrue(result["rate_limited"])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(self.posts, 1)
