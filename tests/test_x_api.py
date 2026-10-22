import hashlib
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from auto_promotion.backends.x_api import XAPI, XHTTP, Rejected
from auto_promotion.models import canonical


class HTTP:
    def __init__(self):
        self.requests = []
        self.responses = []

    def request(self, method, path, **kwargs):
        self.requests.append((method, path, kwargs))
        return self.responses.pop(0)


class XAPIContractTests(unittest.TestCase):
    def test_read_429_retains_retry_after_and_reset_headers(self):
        headers = Message()
        headers["Retry-After"] = "123"
        headers["x-rate-limit-reset"] = "1800000300"
        error = HTTPError("https://api.x.com/2/tweets/search/recent", 429, "limited", headers, None)
        with patch("auto_promotion.backends.x_api.build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(Rejected) as caught:
                XHTTP({"access_token": "synthetic-test-only"}).request(
                    "GET", "tweets/search/recent"
                )
        self.assertGreaterEqual(caught.exception.retry_after, 123)
        self.assertEqual(caught.exception.retry_after, caught.exception.retry_after_seconds)
        self.assertEqual(caught.exception.reset_at, 1800000300)

    def test_native_source_keeps_actual_mention_and_reply_owner_ids(self):
        tweet = {
            **self.tweet,
            "referenced_tweets": [{"type": "replied_to", "id": "77"}],
            "conversation_id": "70",
        }
        result = self.api._source(
            tweet, {"tweets": [{"id": "77", "author_id": "300", "text": "original"}]}
        )
        self.assertEqual(result["mentioned_user_ids"], ["100"])
        self.assertEqual(result["in_reply_to_user_id"], "300")
        self.assertEqual(result["in_reply_to_event_id"], "77")

    def setUp(self):
        self.now = 1800000000.0
        self.http = HTTP()
        self.settings = {"identity": "brand", "user_id": "100", "app_id": "app-1"}
        self.api = XAPI(self.settings, http=self.http, clock=lambda: self.now)
        self.tweet = {
            "id": "9007199254740993123",
            "author_id": "200",
            "text": "@brand can you help with a scene?",
            "created_at": "2027-01-15T08:00:00Z",
            "entities": {"mentions": [{"id": "100", "username": "brand"}]},
        }

    def record(self, **values):
        return {
            "source_reference": "simulation://document",
            "verified_by": "fixture",
            "verified_at": self.now - 60,
            "valid_until": self.now + 86400,
            **values,
        }

    def approve(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        document = Path(self.tmp.name) / "approval.txt"
        document.write_text("Synthetic approval evidence for this test only.")
        self.settings["api_plan_evidence"] = self.record(plan="self_serve")
        self.settings["automation_approval"] = self.record(
            account_user_id="100",
            app_id="app-1",
            document_path=str(document),
            document_sha256=hashlib.sha256(document.read_bytes()).hexdigest(),
        )
        self.settings["recipient_intents"] = {
            self.tweet["id"]: self.record(
                author_id="200",
                account_user_id="100",
                willing_to_receive=True,
                opted_out=False,
                opt_out_checked_at=self.now,
            )
        }

    def test_unknown_account_state_is_not_publish_permission(self):
        result = self.api.eligibility(self.tweet, {})
        self.assertEqual(result["status"], "unknown")
        self.assertIn("platform_approval_unverified", result["reasons"])

    def test_exact_account_summoning_approval_and_recipient_intent_required(self):
        self.approve()
        self.assertEqual(self.api.eligibility(self.tweet, {})["status"], "eligible")
        self.tweet["entities"]["mentions"][0]["id"] = "different-brand-account"
        self.assertIn("reply_target_ineligible", self.api.eligibility(self.tweet, {})["reasons"])

    def test_enterprise_does_not_remove_approval_or_recipient_intent(self):
        self.settings["api_plan_evidence"] = self.record(plan="enterprise")
        result = self.api.eligibility(self.tweet, {})
        self.assertNotIn("reply_target_ineligible", result["reasons"])
        self.assertIn("recipient_intent_unverified", result["reasons"])
        self.assertIn("platform_approval_unverified", result["reasons"])

    def test_public_tool_recommendation_does_not_imply_consent(self):
        self.approve()
        self.tweet["text"] = "Anyone recommend a tool?"
        self.tweet["entities"] = {}
        self.settings["recipient_intents"] = {}
        result = self.api.eligibility(self.tweet, {})
        self.assertEqual(result["status"], "ineligible")
        self.assertIn("recipient_intent_unverified", result["reasons"])

    def test_quoted_own_post_can_summon_but_optout_blocks(self):
        self.approve()
        self.tweet["entities"] = {}
        self.tweet["referenced_tweets"] = [{"type": "quoted", "id": "80"}]
        includes = {"tweets": [{"id": "80", "author_id": "100", "text": "Our post"}]}
        self.assertEqual(self.api.eligibility(self.tweet, includes)["status"], "eligible")
        self.settings["recipient_intents"][self.tweet["id"]]["opted_out"] = True
        self.assertNotEqual(self.api.eligibility(self.tweet, includes)["status"], "eligible")

    def test_fixed_search_window_cursor_and_string_ids_no_since_id(self):
        self.http.responses = [
            {"data": [self.tweet], "meta": {"result_count": 1, "next_token": "page-2"}}
        ]
        result = self.api.discover(
            {
                "high_frequency": True,
                "query": "video",
                "lower_at": self.now - 1200,
                "upper_at": self.now,
                "cursor": "page-1",
            }
        )
        params = self.http.requests[0][2]["params"]
        self.assertNotIn("since_id", params)
        self.assertIn("start_time", params)
        self.assertEqual(params["next_token"], "page-1")
        self.assertEqual(result["next_cursor"], "page-2")
        self.assertEqual(result["candidates"][0]["id"], "9007199254740993123")

    def test_partial_search_never_claims_complete_coverage(self):
        self.http.responses = [{"data": [self.tweet], "errors": [{"title": "partial"}]}]
        result = self.api.discover(
            {
                "high_frequency": True,
                "query": "video",
                "lower_at": self.now - 1200,
                "upper_at": self.now,
            }
        )
        self.assertFalse(result["coverage_complete"])

    def test_readonly_verify_matches_account_content_and_reply_target(self):
        self.http.responses = [
            {
                "data": [
                    {
                        "id": "reply-1",
                        "author_id": "100",
                        "text": "Useful answer",
                        "created_at": "2027-01-15T08:00:00Z",
                        "referenced_tweets": [{"type": "replied_to", "id": self.tweet["id"]}],
                    }
                ]
            }
        ]
        result = self.api.verify(
            {
                "payload": {
                    "target": "https://x.com/example/status/" + self.tweet["id"],
                    "text": "Useful answer",
                }
            }
        )
        self.assertTrue(result["verified"])
        self.assertTrue(all(r[0] == "GET" for r in self.http.requests))

    def test_missing_reply_or_truncated_lookup_stays_unknown(self):
        self.http.responses = [{"data": [], "meta": {"next_token": "more"}}]
        result = self.api.verify(
            {"payload": {"target": "https://x.com/example/status/123", "text": "Useful answer"}}
        )
        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "unknown")

    def test_no_secrets_in_capability_result(self):
        self.settings["access_token"] = "secret-never-display"
        result = self.api.eligibility(self.tweet, {})
        self.assertNotIn("secret-never-display", canonical(result))
