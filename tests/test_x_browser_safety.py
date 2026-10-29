"""Synthetic X state signals: no account, cookies, or platform network."""

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlparse

from auto_promotion.backends import browser


class Locator:
    def __init__(self, visible=False):
        self.visible = visible

    def count(self):
        return int(self.visible)

    def nth(self, _index):
        return self

    def is_visible(self):
        return self.visible

    def evaluate(self, _script):
        return self.visible


class Page:
    def __init__(self, url="https://x.com/home", visible=()):
        self.url, self.visible = url, set(visible)

    def locator(self, selector):
        return Locator(selector in self.visible)


class XSignalTests(unittest.TestCase):
    def test_reliable_url_paths_are_classified_without_reading_content(self):
        for path, reason in [
            ("/account/access", "x_challenge"),
            ("/i/flow/login", "x_login_required"),
            ("/account/suspended", "x_account_restricted"),
        ]:
            with self.subTest(path=path), self.assertRaises(browser.XStateError) as caught:
                browser.check_x_state(Page("https://x.com" + path))
            self.assertEqual(caught.exception.account_block_reason, reason)

    def test_tweet_captcha_text_and_untrusted_urls_do_not_trigger(self):
        for url in [
            "https://x.com/captcha/status/123?next=/account/access",
            "https://example.com/account/access",
            "https://x.com/account/accessibility",
        ]:
            browser.check_x_state(Page(url))

    def test_only_visible_challenge_component_is_a_signal(self):
        selector = 'iframe[src^="https://client-api.arkoselabs.com/"]'
        browser.check_x_state(Page())
        with self.assertRaises(browser.XStateError) as caught:
            browser.check_x_state(Page(visible=[selector]))
        self.assertEqual(caught.exception.failure_class, "challenge")

    def response(self, value, status=200):
        return SimpleNamespace(status=status, json=lambda: value)

    def test_structured_errors_are_independent_of_http_success(self):
        for code, kind, block in [
            (326, "restricted", "x_account_restricted"),
            (89, "login_required", "x_login_required"),
            ("CHALLENGE_REQUIRED", "challenge", "x_challenge"),
            (88, "rate_limited", None),
            (187, "response_rejected", None),
        ]:
            with self.subTest(code=code), self.assertRaises(browser.XStateError) as caught:
                browser.parse_create_tweet(self.response({"errors": [{"code": code}]}))
            self.assertEqual(caught.exception.failure_class, kind)
            self.assertEqual(caught.exception.account_block_reason, block)
            self.assertTrue(caught.exception.confirmed_rejected)

    def test_generic_403_and_429_do_not_claim_captcha(self):
        for status, kind in [(403, "http_forbidden"), (429, "rate_limited")]:
            with self.subTest(status=status), self.assertRaises(browser.XStateError) as caught:
                browser.parse_create_tweet(self.response({}, status))
            self.assertEqual(caught.exception.failure_class, kind)
            self.assertIsNone(caught.exception.account_block_reason)

    def test_receipt_id_wins_over_simultaneous_errors(self):
        result = {"rest_id": "123"}
        document = {
            "errors": [{"code": 326}],
            "data": {"create_tweet": {"tweet_results": {"result": result}}},
        }
        self.assertEqual(browser.parse_create_tweet(self.response(document)), result)

    def test_partial_success_still_records_independent_account_restriction(self):
        backend = browser.BrowserBackend("x", "synthetic", None, Page(), {"identity": "brand"})
        response = self.response(
            {
                "errors": [{"code": 326}],
                "data": {"create_tweet": {"tweet_results": {"result": {"rest_id": "123"}}}},
            }
        )
        saved = []

        def partial(_operation, _payload, checkpoint, _authorize):
            data = backend.create_tweet_data(response)
            self.assertEqual(data["rest_id"], "123")
            backend.step("parse_receipt", True)
            checkpoint({"external_reference": "https://x.com/brand/status/123"})
            raise backend.response_failure

        backend._run = partial
        result = backend.run("execute", {}, saved.append, Mock())
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["account_block_reason"], "x_account_restricted")
        self.assertEqual(result["external_reference"], "https://x.com/brand/status/123")

    def test_preparation_refusal_does_not_count_as_page_anomaly(self):
        result, _ = self.run_failure(error=browser.BackendError("XComposerNotEmpty"))
        self.assertEqual(result["failure_class"], "preparation_failed")
        self.assertNotIn("browser_page_anomaly", result)

    def test_verify_challenge_preserves_top_level_receipt(self):
        backend = browser.BrowserBackend(
            "x", "synthetic", None, Page("https://x.com/account/access"), {"identity": "brand"}
        )
        backend.navigate_home = lambda: None
        url = "https://x.com/brand/status/123"
        result = backend.run("verify", {"external_reference": url}, Mock(), Mock())
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["external_reference"], url)

    def test_generic_message_mentions_do_not_claim_challenge(self):
        with self.assertRaises(browser.XStateError) as caught:
            browser.parse_create_tweet(self.response({"errors": [{"message": "captcha text"}]}))
        self.assertIsNone(caught.exception.account_block_reason)

    def run_failure(self, *, submitted=False, receipt=None, error=None):
        backend = browser.BrowserBackend("x", "synthetic", None, Page(), {"identity": "brand"})
        checkpoint = Mock()

        def fail(*_args):
            backend.step("target_lookup", submitted)
            raise error or browser.XStateError("XChallengeRequired", "challenge", "x_challenge")

        backend._run = fail
        payload = {"payload": {"action": "reply"}, "receipt": receipt or {}}
        return backend.run("execute", payload, checkpoint, Mock()), checkpoint

    def test_before_submit_challenge_is_failed_known_and_checkpointed(self):
        result, checkpoint = self.run_failure()
        self.assertEqual(result["status"], "failed_known")
        self.assertTrue(result["confirmed_not_submitted"])
        self.assertEqual(result["account_block_reason"], "x_challenge")
        checkpoint.assert_called_once()

    def test_after_submit_challenge_stays_unknown(self):
        result, _ = self.run_failure(submitted=True)
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result.get("confirmed_not_submitted", False))

    def test_existing_receipt_is_preserved_on_challenge(self):
        receipt = {"message_ids": [123], "external_reference": "https://x.com/brand/status/123"}
        result, _ = self.run_failure(receipt=receipt)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["message_ids"], [123])
        self.assertEqual(result["external_reference"], receipt["external_reference"])

    def test_network_and_dom_timeout_are_not_verification_claims(self):
        for error, kind in [(OSError("network"), "network"), (TimeoutError(), "page_anomaly")]:
            result, _ = self.run_failure(error=error)
            self.assertEqual(result["failure_class"], kind)
            self.assertNotIn("account_block_reason", result)
            self.assertEqual(result["status"], "failed_known")

    def test_legacy_verification_error_is_a_durable_stop_at_every_submission_boundary(self):
        for submitted, receipt in [(False, None), (True, None), (False, {"message_ids": [123]})]:
            with self.subTest(submitted=submitted, receipt=receipt):
                result, checkpoint = self.run_failure(
                    submitted=submitted,
                    receipt=receipt,
                    error=browser.BackendError("BrowserVerificationRequired"),
                )
                self.assertEqual(result.get("account_block_reason"), "x_challenge")
                self.assertEqual(result["failure_class"], "challenge")
                self.assertEqual(
                    result["status"], "unknown" if submitted or receipt else "failed_known"
                )
                checkpoint.assert_called_once()

    def test_legacy_health_and_preflight_verification_are_not_healthy_or_allowed(self):
        for operation in ("health", "preflight"):
            with self.subTest(operation=operation):
                backend = browser.BrowserBackend(
                    "x", "synthetic", None, Page(), {"identity": "brand"}
                )

                def fail(*_args):
                    raise browser.BackendError("BrowserVerificationRequired")

                backend._run = fail
                saved = Mock()
                result = backend.run(operation, {}, saved, Mock())
                self.assertEqual(result.get("account_block_reason"), "x_challenge")
                self.assertFalse(result.get("healthy" if operation == "health" else "allowed"))
                saved.assert_called_once()


class XContextSignalTests(unittest.TestCase):
    def context(self, *pages):
        return SimpleNamespace(pages=list(pages))

    def test_closed_sibling_is_ignored_but_unreadable_open_page_is_not_success(self):
        selected, sibling = Page(), Page()
        sibling.is_closed = lambda: True
        sibling.locator = Mock(side_effect=RuntimeError("SECRET"))
        browser.check_x_context_state(self.context(selected, sibling), selected)
        sibling.is_closed = lambda: False
        with self.assertRaisesRegex(browser.BackendError, "XStateObservationUnavailable"):
            browser.check_x_context_state(self.context(selected, sibling), selected)

    def test_unreadable_page_does_not_hide_another_pages_challenge(self):
        broken, challenge = Page(), Page("https://x.com/account/access")
        broken.locator = Mock(side_effect=RuntimeError("SECRET"))
        with self.assertRaises(browser.XStateError) as caught:
            browser.check_x_context_state(self.context(broken, challenge), broken)
        self.assertEqual(caught.exception.account_block_reason, "x_challenge")

    def test_context_limit_and_closed_selected_page_fail_closed_without_claiming_captcha(self):
        selected = Page()
        selected.is_closed = lambda: True
        for context in (self.context(selected), self.context(*[Page() for _ in range(33)])):
            with self.subTest(context=context), self.assertRaises(browser.BackendError) as caught:
                browser.check_x_context_state(context, selected)
            self.assertNotIsInstance(caught.exception, browser.XStateError)

    def test_another_account_context_is_never_inspected(self):
        selected = Page()
        other = self.context(Page("https://x.com/account/access"))
        browser.check_x_context_state(self.context(selected), selected)
        self.assertEqual(len(other.pages), 1)

    def test_selected_closed_or_removed_during_observation_cannot_verify_health(self):
        for closed in (False, True):
            with self.subTest(closed=closed):
                selected = Page()
                context = self.context(selected)
                selected.is_closed = lambda: closed and not context.pages

                def inspect(_selector):
                    context.pages.clear()
                    return Locator()

                selected.locator = inspect
                with self.assertRaisesRegex(browser.BackendError, "XStateObservationUnavailable"):
                    browser.check_x_context_state(context, selected)

    def test_selected_page_from_another_context_is_not_inspected(self):
        selected, own = Page("https://x.com/account/access"), Page()
        with self.assertRaises(browser.BackendError) as caught:
            browser.check_x_context_state(self.context(own), selected)
        self.assertNotIsInstance(caught.exception, browser.XStateError)

    def test_navigation_during_inspection_and_new_page_invalidate_success(self):
        selected = Page()
        selected.locator = Mock(
            side_effect=lambda _: setattr(selected, "url", "https://x.com/another") or Locator()
        )
        with self.assertRaisesRegex(browser.BackendError, "XStateObservationUnavailable"):
            browser.check_x_context_state(self.context(selected), selected)
        selected = Page()
        context = self.context(selected)
        selected.locator = Mock(side_effect=lambda _: context.pages.append(Page()) or Locator())
        with self.assertRaisesRegex(browser.BackendError, "XStateObservationUnavailable"):
            browser.check_x_context_state(context, selected)


@unittest.skipUnless(os.getenv("AP_TEST_BROWSER_BACKENDS") == "1", "offline browser opt-in")
class XStageBrowserTests(unittest.TestCase):
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
        self.context.set_offline(True)
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.stage, self.errors, self.http_status = None, None, 200
        self.posts, self.saved = 0, []
        self.payload = {
            "payload": {
                "action": "reply",
                "target": "https://x.com/target/status/456",
                "text": "ordinary captcha discussion",
                "media": [],
            }
        }
        self.context.route("**/*", self.route)
        self.backend = browser.BrowserBackend(
            "x", "synthetic", self.context, self.page, {"identity": "brand"}
        )

    def route(self, route):
        parsed = urlparse(route.request.url)
        if parsed.hostname == "challenges.cloudflare.com":
            route.fulfill(content_type="text/html", body="<h1>Synthetic challenge</h1>")
            return
        if parsed.hostname != "x.com":
            route.abort()
            raise AssertionError("Fixture disallows all external network")
        if parsed.path == "/CreateTweet":
            self.posts += 1
            document = (
                {"errors": self.errors}
                if self.errors
                else {
                    "data": {
                        "create_tweet": {
                            "tweet_results": {
                                "result": {
                                    "rest_id": "123",
                                    "legacy": {"in_reply_to_status_id_str": "456"},
                                }
                            }
                        }
                    }
                }
            )
            route.fulfill(
                status=self.http_status, content_type="application/json", body=json.dumps(document)
            )
            return
        if route.request.method != "GET":
            raise AssertionError("Unexpected fixture mutation")
        challenge = '<div data-testid="arkoseChallenge">Human check</div>'
        own = parsed.path.endswith("/123")
        blocked = (
            (self.stage == "home" and parsed.path == "/")
            or (self.stage == "target" and parsed.path.endswith("/456"))
            or (self.stage == "receipt" and own)
        )
        content = '<button data-testid="SideNav_AccountSwitcher_Button">Brand @brand</button>'
        if blocked:
            content += challenge
        identifier, author = ("123", "brand") if own else ("456", "target")
        editor_signal = (
            "document.getElementById('challenge').hidden=false;" if self.stage == "composer" else ""
        )
        content += f"""<article data-testid="tweet"><div data-testid="User-Name">Name @{author}</div>
            <a href="/{author}/status/{identifier}"><time>today</time></a>
            <div data-testid="tweetText">ordinary captcha discussion</div>
            <button data-testid="reply" onclick="document.querySelector('#compose').hidden=false;
              {editor_signal}">Reply</button>
            </article><div id="challenge" data-testid="arkoseChallenge" hidden>Verify</div>
            <div id="compose" hidden><div contenteditable="true" data-testid="tweetTextarea_0"></div>
            <button data-testid="tweetButtonInline" onclick="fetch('/CreateTweet',{{method:'POST'}})">Send</button></div>"""
        route.fulfill(content_type="text/html", body=content)

    def run_backend(self, operation="execute", authorize=None):
        return self.backend.run(
            operation, self.payload, self.saved.append, authorize or (lambda: None)
        )

    def test_home_health_and_preflight_challenge_are_not_healthy_or_allowed(self):
        self.stage = "home"
        health = self.run_backend("health")
        self.assertFalse(health["healthy"])
        self.assertEqual(health["account_block_reason"], "x_challenge")
        preflight = self.run_backend("preflight")
        self.assertFalse(preflight["allowed"])
        self.assertEqual(preflight["account_block_reason"], "x_challenge")
        self.assertEqual(self.posts, 0)

    def test_target_and_editor_challenge_never_submit(self):
        for stage in ("target", "composer"):
            self.stage = stage
            result = self.run_backend()
            self.assertEqual(result["status"], "failed_known")
            self.assertEqual(result["account_block_reason"], "x_challenge")
        self.assertEqual(self.posts, 0)

    def test_challenge_appearing_at_final_authorization_prevents_click(self):
        def challenge():
            self.page.locator("#challenge").evaluate("e => e.hidden = false")

        result = self.run_backend(authorize=challenge)
        self.assertEqual(result["account_block_reason"], "x_challenge")
        self.assertEqual(result["status"], "failed_known")
        self.assertEqual(self.posts, 0)

    def test_challenge_after_durable_receipt_retains_unknown_and_reference(self):
        self.stage = "receipt"
        result = self.run_backend()
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["account_block_reason"], "x_challenge")
        self.assertEqual(result["external_reference"], "https://x.com/brand/status/123")
        self.assertTrue(result["submission_started"])
        self.assertEqual(self.posts, 1)

    def test_http_200_challenge_and_restricted_response_are_known_refusals(self):
        for code, block in [("CHALLENGE_REQUIRED", "x_challenge"), (326, "x_account_restricted")]:
            self.errors = [{"code": code}]
            result = self.run_backend()
            self.assertEqual(result["status"], "failed_known")
            self.assertTrue(result["confirmed_rejected"])
            self.assertEqual(result["account_block_reason"], block)
        self.assertEqual(self.posts, 2)

    def test_http_403_429_keep_independent_categories(self):
        for status, kind in [(403, "http_forbidden"), (429, "rate_limited")]:
            self.http_status, self.errors = status, [{"code": 999}]
            result = self.run_backend()
            self.assertEqual(result["failure_class"], kind)
            self.assertEqual(result["status"], "failed_known")
            self.assertNotIn("account_block_reason", result)
        self.assertEqual(self.posts, 2)

    def test_tweet_text_about_captcha_is_not_a_challenge(self):
        result = self.run_backend()
        self.assertEqual(result["status"], "confirmed")
        self.assertNotIn("account_block_reason", result)
        self.assertEqual(self.posts, 1)

    def test_existing_challenge_is_not_discarded_by_home_navigation(self):
        for operation in ("health", "preflight"):
            with self.subTest(operation=operation):
                self.page.goto("https://x.com/account/access")
                result = self.run_backend(operation)
                self.assertEqual(result.get("account_block_reason"), "x_challenge")
                self.assertEqual(self.page.url, "https://x.com/account/access")
        self.assertEqual(self.posts, 0)

    def test_same_context_challenge_tab_blocks_health_preflight_and_execute(self):
        sibling = self.context.new_page()
        sibling.goto("https://x.com/account/access")
        for operation in ("health", "preflight", "execute"):
            with self.subTest(operation=operation):
                result = self.run_backend(operation)
                self.assertEqual(result.get("account_block_reason"), "x_challenge")
                self.assertFalse(result.get("healthy" if operation == "health" else "allowed"))
        self.assertEqual(self.posts, 0)

    def test_hidden_third_party_frames_and_safe_text_do_not_block(self):
        sibling = self.context.new_page()
        sibling.goto("https://x.com/home")
        for attributes in ("hidden", 'style="opacity:0"', 'aria-hidden="true"'):
            with self.subTest(attributes=attributes):
                sibling.set_content(
                    "<h1>captcha documentation</h1><div "
                    + attributes
                    + '><iframe width="400" height="100" src="https://challenges.cloudflare.com/test"></iframe></div>'
                )
                result = self.run_backend("health")
                self.assertTrue(result["healthy"])
                self.assertNotIn("account_block_reason", result)


if __name__ == "__main__":
    unittest.main()
