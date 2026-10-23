"""Opt-in real-browser contracts using intercepted synthetic pages only."""

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

from auto_promotion.backends.browser import BrowserBackend, x_editor_text
from auto_promotion.backends.common import BackendError


@unittest.skipUnless(
    os.getenv("AP_TEST_BROWSER_BACKENDS") == "1", "offline browser tests are opt-in"
)
class OriginalPostBrowserTests(unittest.TestCase):
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
        self.page.set_default_timeout(2000)
        self.posts, self.uploads = 0, 0
        self.upload_ready_at_submit = None
        self.author, self.parent, self.quoted = "brand", None, False
        self.draft, self.hide_media = "", False
        self.hide_composer_switcher = False
        self.composer_identity = "brand"
        self.published_html = None
        self.initial_attachment_count = 0
        self.saved, self.authorized = [], []
        self.item = {
            "action": "post",
            "target": "https://x.com/brand",
            "target_owned": True,
            "text": "Exact original post",
            "media": [],
        }
        self.context.route("**/*", self.route)

    def image(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        target = Path(folder.name) / "one.png"
        target.write_bytes(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
            )
        )
        self.item["media"] = [
            {"path": str(target), "filename": target.name, "mime_type": "image/png"}
        ]

    def route(self, route):
        request, path = route.request, urlparse(route.request.url).path
        if urlparse(request.url).hostname != "x.com":
            raise AssertionError("No external network is allowed in this fixture")
        if path == "/CreateTweet":
            self.posts += 1
            self.uploads = request.post_data_json["files"]
            self.upload_ready_at_submit = request.post_data_json["ready"]
            legacy = {
                "created_at": "Tue Oct 20 12:00:00 +0000 2026",
                "full_text": self.item["text"],
                "in_reply_to_status_id_str": self.parent,
                "is_quote_status": self.quoted,
                "extended_entities": {
                    "media": [{"id_str": str(i), "type": "photo"} for i in range(self.uploads)]
                },
            }
            route.fulfill(
                content_type="application/json",
                body=json.dumps(
                    {
                        "data": {
                            "create_tweet": {
                                "tweet_results": {"result": {"rest_id": "1234", "legacy": legacy}}
                            }
                        }
                    }
                ),
            )
            return
        if request.method != "GET":
            raise AssertionError("Unexpected mutation in fixture")
        switcher = '<button data-testid="SideNav_AccountSwitcher_Button">Brand @brand</button>'
        if path == "/compose/post":
            style = ' style="visibility:hidden"' if self.hide_composer_switcher else ""
            switcher = (
                '<button data-testid="SideNav_AccountSwitcher_Button"'
                + style
                + ">Name @"
                + self.composer_identity
                + "</button>"
            )
            route.fulfill(
                content_type="text/html; charset=utf-8",
                body=switcher
                + f"""<div role="dialog">
            <div contenteditable="true" data-testid="tweetTextarea_0">{self.draft}</div>
            <div data-testid="attachments">{"<img>" * self.initial_attachment_count}</div><input type="file" multiple onchange="const a=document.querySelector('[data-testid=attachments]');a.innerHTML='<div role=progressbar></div>';setTimeout(()=>{{a.innerHTML=Array.from(this.files,()=>'<img>').join('');}},80)">
            <button data-testid="tweetButton" onclick="fetch('/CreateTweet',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{files:document.querySelector('input').files.length,ready:!document.querySelector('[role=progressbar]') && document.querySelectorAll('[data-testid=attachments] img').length===document.querySelector('input').files.length}})}})">Post</button></div>""",
            )
        elif path == "/brand/status/1234":
            photos = (
                "" if self.hide_media else '<div data-testid="tweetPhoto"></div>' * self.uploads
            )
            route.fulfill(
                content_type="text/html; charset=utf-8",
                body=switcher
                + f"""<article data-testid="tweet"><div data-testid="User-Name">Name @{self.author}</div><a href="/brand/status/1234"><time datetime="2026-10-20T12:00:00Z">now</time></a><div data-testid="tweetText">{self.published_html if self.published_html is not None else self.item["text"]}</div>{photos}</article>""",
            )
        elif path == "/":
            route.fulfill(content_type="text/html; charset=utf-8", body=switcher)
        else:
            raise AssertionError("Unexpected page: " + path)

    def execute(self, authorize=None):
        backend = BrowserBackend("x", "synthetic", self.context, self.page, {"identity": "brand"})
        return backend.run(
            "execute",
            {"payload": self.item},
            self.saved.append,
            authorize or (lambda: self.authorized.append(True)),
        )

    def test_text_post_is_submitted_once_with_real_page_verification(self):
        result = self.execute()
        self.assertEqual(self.posts, 1)
        self.assertEqual(len(self.authorized), 1)
        self.assertTrue(result["verified"])
        self.assertEqual(result["published_at"], 1792497600)
        self.assertEqual(self.saved[0]["external_reference"], "https://x.com/brand/status/1234")

    def test_png_is_uploaded_and_verified_as_part_of_the_post(self):
        self.image()
        self.assertTrue(self.execute()["verified"])
        self.assertEqual(self.uploads, 1)
        self.assertTrue(self.upload_ready_at_submit)

    def test_original_lines_are_entered_as_paragraphs_before_one_submit(self):
        self.item["text"] = "First 🌍\n\n두 번째\n\nhttps://example.test/approved"
        self.assertTrue(self.execute()["verified"])
        self.assertEqual(self.posts, 1)

    def test_reply_or_quote_receipt_cannot_be_an_original_post(self):
        self.parent = "99"
        self.assertFalse(self.execute()["verified"])
        self.parent, self.quoted = None, True
        self.assertFalse(self.execute()["verified"])

    def test_wrong_author_cannot_confirm_a_post(self):
        self.author = "other"
        self.assertFalse(self.execute()["verified"])

    def test_missing_image_on_the_actual_post_cannot_confirm(self):
        self.image()
        self.hide_media = True
        self.assertFalse(self.execute()["verified"])

    def test_existing_draft_is_preserved_without_submission(self):
        self.draft = "user draft"
        result = self.execute()
        self.assertEqual(result["status"], "failed_known")
        self.assertEqual(result["reason"], "XComposerNotEmpty")
        self.assertFalse(result["submission_started"])
        self.assertEqual(self.posts, 0)
        self.assertEqual(self.page.get_by_test_id("tweetTextarea_0").inner_text(), "user draft")

    def test_empty_contenteditable_newline_is_cleared_before_exact_text(self):
        self.draft = "\n"
        result = self.execute()
        self.assertTrue(result["verified"])
        self.assertEqual(self.posts, 1)
        self.assertEqual(len(self.authorized), 1)

    def test_draft_blocks_keep_exact_blank_lines_without_rendered_extra_newlines(self):
        self.page.goto("https://x.com/compose/post")
        editor = self.page.get_by_test_id("tweetTextarea_0")
        editor.evaluate(
            "el => el.innerHTML = '<div data-block=true>First paragraph</div>' + "
            "'<div data-block=true><br></div><div data-block=true>두 번째 문단</div>'"
        )
        approved = "First paragraph\n\n두 번째 문단"
        self.assertNotEqual(editor.inner_text(), approved)
        self.assertEqual(x_editor_text(editor), approved)

    def test_stop_after_preparation_prevents_any_submit(self):
        def stopped():
            raise BackendError("ExecutionStopped")

        result = self.execute(stopped)
        self.assertEqual(result["status"], "failed_known")
        self.assertEqual(self.posts, 0)

    def test_composer_overlay_keeps_the_exact_account_identity_check(self):
        self.hide_composer_switcher = True
        self.assertTrue(self.execute()["verified"])
        self.assertEqual(self.posts, 1)

    def test_hidden_composer_identity_mismatch_never_submits(self):
        self.hide_composer_switcher = True
        self.composer_identity = "other"
        result = self.execute()
        self.assertEqual(result["reason"], "BrowserIdentityMismatch")
        self.assertEqual(self.posts, 0)

    def test_public_text_uses_observed_expanded_link_and_emoji(self):
        self.item["text"] = "Translation 🌍\n\nhttps://example.test/download?platform=android"
        self.published_html = 'Translation <img alt="🌍">\n\n<a href="https://t.co/short" title="https://example.test/download?platform=android">example.test/download…</a>'
        backend = BrowserBackend("x", "synthetic", self.context, self.page, {"identity": "brand"})
        result = backend.verify(
            {
                "payload": self.item,
                "receipt": {
                    "external_reference": "https://x.com/brand/status/1234",
                    "target_verified": True,
                },
            }
        )
        self.assertTrue(result["verified"])

    def test_wrong_expanded_link_cannot_confirm(self):
        self.item["text"] = "https://example.test/approved"
        self.published_html = '<a href="https://t.co/short" title="https://example.test/other">example.test/approved</a>'
        backend = BrowserBackend("x", "synthetic", self.context, self.page, {"identity": "brand"})
        result = backend.verify(
            {
                "payload": self.item,
                "receipt": {
                    "external_reference": "https://x.com/brand/status/1234",
                    "target_verified": True,
                },
            }
        )
        self.assertFalse(result["verified"])

    def test_editor_emoji_images_keep_the_exact_approved_character(self):
        self.page.goto("https://x.com/compose/post")
        editor = self.page.get_by_test_id("tweetTextarea_0")
        editor.evaluate(
            "el => el.innerHTML = '<div data-block=true>Translate <img alt=🌍></div><div data-block=true><br></div><div data-block=true>Now</div>'"
        )
        self.assertEqual(x_editor_text(editor), "Translate 🌍\n\nNow")

    def test_identical_approved_text_draft_is_preserved_and_submitted_once(self):
        self.draft = self.item["text"]
        self.assertTrue(self.execute()["verified"])
        self.assertEqual(self.posts, 1)
        self.assertEqual(len(self.authorized), 1)

    def test_existing_media_is_never_reused_or_overwritten(self):
        self.draft = self.item["text"]
        self.initial_attachment_count = 1
        result = self.execute()
        self.assertEqual(result["reason"], "XComposerNotEmpty")
        self.assertEqual(self.posts, 0)


if __name__ == "__main__":
    unittest.main()
