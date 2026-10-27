"""Read-only platform notices on local synthetic pages; no platform access."""

import os
import unittest

from auto_promotion.backends.browser import BrowserBackend
from auto_promotion.backends.browser_status import STATUS_JS, check_context_status
from auto_promotion.backends.common import BackendError


@unittest.skipUnless(os.getenv("AP_TEST_BROWSER_BACKENDS") == "1", "offline browser opt-in")
class BrowserAccountStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls.runtime = sync_playwright().start()
        cls.browser = cls.runtime.chromium.launch(
            executable_path=os.getenv("AP_BROWSER_EXECUTABLE") or None, headless=True
        )

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.runtime.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()

    def test_visible_verification_and_restrictions_block_even_with_correct_identity(self):
        for notice, expected in (
            ("<h1>Verify you are human</h1>", "BrowserVerificationRequired"),
            ("<h1>正在进行安全验证</h1><p>请验证您是真人</p>", "BrowserVerificationRequired"),
            ('<div role="dialog">Authenticate your account</div>', "BrowserVerificationRequired"),
            ('<iframe title="Security challenge"></iframe>', "BrowserVerificationRequired"),
            ("<h2>Your account has been locked</h2>", "BrowserAccountRestricted"),
            ('<div role="alert">账号已被限制</div>', "BrowserAccountRestricted"),
        ):
            with self.subTest(notice=notice):
                self.page.set_content(
                    notice
                    + '<button data-testid="SideNav_AccountSwitcher_Button">Brand @brand</button>'
                )
                backend = BrowserBackend(
                    "x", "offline", self.context, self.page, {"identity": "brand"}
                )
                with self.assertRaisesRegex(BackendError, expected):
                    backend.check_identity()

    def test_user_posts_and_hidden_challenges_do_not_mark_account_as_restricted(self):
        self.page.set_content("""
          <article data-testid="tweet"><h2>Your account is suspended</h2></article>
          <shreddit-post><h1>Verify you are human</h1></shreddit-post>
          <iframe style="display:none" title="Security challenge"></iframe>
          <iframe style="width:0;height:0;border:0" title="Security challenge"></iframe>
          <div style="opacity:0"><iframe title="Security challenge"></iframe></div>
          <button data-testid="SideNav_AccountSwitcher_Button">Brand @brand</button>
        """)
        self.assertIsNone(self.page.evaluate(STATUS_JS))
        BrowserBackend(
            "x", "offline", self.context, self.page, {"identity": "brand"}
        ).check_identity()

    def test_account_access_and_suspension_redirects_are_detected(self):
        self.page.route(
            "**/*",
            lambda route: route.fulfill(content_type="text/html", body="<p>platform notice</p>"),
        )
        for path, expected in (
            ("/account/access", "BrowserVerificationRequired"),
            ("/account/suspended", "BrowserAccountRestricted"),
        ):
            self.page.goto("https://synthetic.test" + path)
            self.assertEqual(self.page.evaluate(STATUS_JS), expected)

    def test_verification_in_second_page_survives_before_home_navigation(self):
        self.context.route("**/*", lambda route: route.fulfill(content_type="text/html", body="<p>offline</p>"))
        self.page.goto("https://x.com/home")
        second = self.context.new_page()
        second.goto("https://x.com/account/access")
        with self.assertRaisesRegex(BackendError, "BrowserVerificationRequired"):
            check_context_status(self.context)
        self.assertEqual(second.url, "https://x.com/account/access")
