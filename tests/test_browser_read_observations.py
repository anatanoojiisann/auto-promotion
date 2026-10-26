"""Read-only browser diagnostics; every browser request uses synthetic routes."""

import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from auto_promotion.backends import browser, browser_use, tabbit
from auto_promotion.backends.common import BackendError


class ReadObservationContractTests(unittest.TestCase):
    settings = {"identity": "brand", "tabbit_instance": "0123456789ABCDEF"}
    inventory = {
        "task": {"reused": False},
        "truncated": False,
        "tabs": [{"state": "available", "tabId": 17, "url": "https://x.com/brand"}],
    }
    payload = {
        "high_frequency": True,
        "read_only": True,
        "query": "video",
        "lower_at": 0,
        "upper_at": 9999999999,
        "timeout_seconds": 5,
    }

    def scan(self, value, finish=None):
        responses = [
            self.inventory,
            {"status": "succeeded", "result": {"value": value}},
            {"ok": True} if finish is None else finish,
        ]
        with patch.object(tabbit, "cli", side_effect=responses) as cli:
            result = tabbit.call("x", self.settings, "discover", self.payload)
        return result, cli

    def value(self, **changes):
        return {
            "url": "https://x.com/search?q=video",
            "posts": [
                {
                    "id": "123",
                    "author_id": "@someone",
                    "datetime": "2026-10-25T00:00:00Z",
                    "text": "video help",
                }
            ],
            "read_contract": "repo_x_scan_v1",
            "read_observed_at": 1000,
            "read_primary_request_count": 3,
            "read_primary_response_count": 3,
            "read_resource_failures": {},
            **changes,
        }

    def test_tabbit_known_primary_429_is_not_a_successful_sample(self):
        result, _ = self.scan(
            self.value(
                rate_limited=True,
                read_primary_rate_limited=True,
                retry_after_seconds=120,
                read_state="rate_limited",
            )
        )
        self.assertTrue(result["rate_limited"])
        self.assertEqual(result["retry_after_seconds"], 120)
        self.assertEqual(result["candidates"], [])
        self.assertFalse(result["read_page_complete"])

    def test_tabbit_external_search_url_is_rejected(self):
        with self.assertRaisesRegex(BackendError, "TabbitSearchUnavailable"):
            self.scan(self.value(url="https://example.com/search?q=video"))

    def test_tabbit_snapshot_preserves_observation_and_stable_page_fingerprint(self):
        first, _ = self.scan(self.value())
        second, _ = self.scan(self.value(read_observed_at=2000))
        self.assertEqual(first["read_observed_at"], 1000)
        self.assertEqual(first["read_page_fingerprint"], second["read_page_fingerprint"])
        self.assertEqual(first["read_observation_source"], "tabbit_repository_dom")
        self.assertFalse(first["coverage_complete"])

    def test_tabbit_legacy_unstamped_receipt_is_not_stamped_fresh(self):
        value = self.value()
        value.pop("read_observed_at")
        value.pop("read_contract")
        result, _ = self.scan(value)
        self.assertIsNone(result["read_observed_at"])
        self.assertEqual(result["read_observation_source"], "tabbit_unstamped_receipt")

    def test_tabbit_primary_limit_survives_cleanup_failure(self):
        result, _ = self.scan(
            self.value(
                rate_limited=True, read_primary_rate_limited=True, read_state="rate_limited"
            ),
            finish={"forcedCleanup": True},
        )
        self.assertTrue(result["rate_limited"])
        self.assertEqual(result["read_cleanup_state"], "incomplete")

    def test_browser_use_health_preserves_original_manual_observation_time(self):
        settings = {
            "identity": "brand",
            "browser_instance_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "browser_family": "chrome",
            "browser_tab_id": "123",
            "browser_use_observation": {
                "observed_at": 1000,
                "handles": ["brand"],
                "profile_href": "/brand",
            },
        }
        with patch.object(browser_use.time, "time", return_value=1100):
            result = browser_use.call("x", settings, "health")
        self.assertEqual(result["read_observed_at"], 1000)
        self.assertEqual(result["read_observation_source"], "browser_use_manual_identity")
        self.assertFalse(result["live_probe"])

    def test_valid_long_retry_after_is_not_replaced_by_short_fallback(self):
        self.assertEqual(
            browser.retry_after(SimpleNamespace(headers={"Retry-After": "604801"})), 604801
        )

    def test_tabbit_pending_read_task_is_not_replaced_with_a_new_name(self):
        with patch.object(
            tabbit, "cli", side_effect=[self.inventory, {"status": "queued"}, {"status": "running"}]
        ) as first:
            with self.assertRaisesRegex(BackendError, "TabbitReadOnlyPending"):
                tabbit.call("x", self.settings, "discover", self.payload)
        with patch.object(tabbit, "cli", return_value={"task": {"reused": True}}) as second:
            with self.assertRaisesRegex(BackendError, "TabbitInventoryUnavailable"):
                tabbit.call("x", self.settings, "discover", self.payload)
        self.assertEqual(first.call_args_list[0].args[0], second.call_args_list[0].args[0])
        self.assertEqual(second.call_count, 1)

    def test_tabbit_health_cannot_pass_a_visible_account_challenge(self):
        value = {
            "read_contract": "repo_x_scan_v1",
            "handles": ["brand"],
            "profile": "/brand",
            "account_block_reason": "x_challenge",
            "reason": "XChallengeRequired",
        }
        with patch.object(
            tabbit,
            "cli",
            side_effect=[
                self.inventory,
                {"status": "succeeded", "result": {"value": value}},
                {"ok": True},
            ],
        ):
            result = tabbit.call("x", self.settings, "health")
        self.assertFalse(result["healthy"])
        self.assertEqual(result["account_block_reason"], "x_challenge")


@unittest.skipUnless(os.getenv("AP_TEST_BROWSER_BACKENDS") == "1", "offline browser opt-in")
class PlaywrightReadObservationTests(unittest.TestCase):
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
        self.mode = "normal"
        self.requests = []
        self.saved = []
        self.context.route("**/*", self.route)
        self.backend = browser.BrowserBackend(
            "x", "synthetic", self.context, self.page, {"identity": "brand"}
        )

    def route(self, route):
        from urllib.parse import urlparse

        url = urlparse(route.request.url)
        self.requests.append(url.path)
        if url.hostname == "api.x.com":
            route.fulfill(
                status=429 if self.mode == "primary429" else 200,
                headers={"Access-Control-Allow-Origin": "*", "Retry-After": "120"},
                content_type="application/json",
                body="{}",
            )
            return
        if url.hostname == "video.twimg.com":
            route.abort("aborted")
            return
        self.assertEqual(url.hostname, "x.com", "all requests must use intercepted fixture hosts")
        switcher = '<button data-testid="SideNav_AccountSwitcher_Button">Brand @brand</button>'
        if url.path == "/search":
            if self.mode == "challenge":
                route.fulfill(
                    content_type="text/html",
                    body=switcher + '<div data-testid="arkoseChallenge">Check</div>',
                )
                return
            article = '<article data-testid="tweet"><a href="/someone/status/123"><time datetime="2026-10-25T00:00:00Z">now</time></a><div data-testid="tweetText">video help</div></article>'
            route.fulfill(
                content_type="text/html",
                body=switcher
                + '<div data-testid="primaryColumn"></div>'
                + "<script>(async()=>{await fetch('https://api.x.com/i/api/graphql/hash/SearchTimeline');"
                + (
                    "await fetch('https://video.twimg.com/media.mp4').catch(()=>{});"
                    if self.mode == "media"
                    else ""
                )
                + "document.querySelector('[data-testid=primaryColumn]').innerHTML="
                + repr(article)
                + ";})();</script>",
            )
        else:
            route.fulfill(content_type="text/html", body=switcher)

    def scan(self, **changes):
        return self.backend.run(
            "discover",
            {**ReadObservationContractTests.payload, **changes},
            self.saved.append,
            lambda: self.fail("read-only scan cannot authorize"),
        )

    def test_search_xhr_429_stops_and_discards_stale_dom(self):
        self.mode = "primary429"
        result = self.scan()
        self.assertTrue(result.get("rate_limited"))
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["retry_after_seconds"], 120)
        self.assertFalse(result["coverage_complete"])

    def test_known_rate_limit_survives_browser_listener_cleanup_failure(self):
        self.mode = "primary429"
        with patch.object(self.page, "unroute", side_effect=RuntimeError("synthetic cleanup")):
            result = self.scan()
        self.assertTrue(result["rate_limited"])
        self.assertEqual(result["read_cleanup_state"], "incomplete")

    def test_media_failure_is_separate_from_rate_limit_or_challenge(self):
        self.mode = "media"
        result = self.scan()
        self.assertEqual(len(result["candidates"]), 1)
        self.assertFalse(result.get("rate_limited", False))
        self.assertNotIn("account_block_reason", result)
        self.assertEqual(sum(result["read_resource_failures"].values()), 1)

    def test_search_challenge_is_persisted_before_return(self):
        self.mode = "challenge"
        result = self.scan(timeout_seconds=1)
        self.assertEqual(result.get("account_block_reason"), "x_challenge")
        self.assertTrue(self.saved)
        self.assertFalse(result["coverage_complete"])

    def test_primary_request_budget_denies_before_search_xhr(self):
        result = self.scan(read_request_limit=2)
        self.assertEqual(result["read_state"], "budget_exhausted")
        self.assertFalse(result.get("rate_limited", False))
        self.assertNotIn("/i/api/graphql/hash/SearchTimeline", self.requests)

    def test_repeated_dom_has_same_fingerprint_but_distinct_fresh_observation(self):
        first, second = self.scan(), self.scan()
        self.assertEqual(first["read_page_fingerprint"], second["read_page_fingerprint"])
        self.assertGreaterEqual(second["read_observed_at"], first["read_observed_at"])

    def test_tabbit_repository_javascript_with_intercepted_chromium(self):
        result = subprocess.run(
            ["node", "scripts/test_x_read_observations.cjs"],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "AP_PYTHON": sys.executable},
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
