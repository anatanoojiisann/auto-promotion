"""Official search contracts; synthetic HTTP, profiles, clock and persistent queues only."""

import copy
import os
import socket
import unittest
from unittest.mock import Mock, patch

import test_interactions as fixtures

from auto_promotion.backends.common import BackendError
from auto_promotion.backends.x_api import XAPI, XHTTP, Rejected
from auto_promotion.core import Conflict
from auto_promotion.interaction_connectors import InteractionConnectors
from auto_promotion.interaction_simulation import drain_sender, source
from auto_promotion.interactions import Interactions
from auto_promotion.models import ItemInput, TaskInput


class OfficialScanTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.InteractionTests("runTest")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.addCleanup(patch.stopall)
        patch.object(
            socket, "create_connection", side_effect=AssertionError("offline only")
        ).start()
        patch.dict(os.environ, {"AP_X_BACKEND": "builtin"}).start()
        self.profile = {
            "connection_type": "x_api",
            "identity": "synthetic",
            "user_id": "100",
            "access_token": "SYNTHETIC_TOKEN_CANARY",
            "app_id": "test-app",
        }
        patch(
            "auto_promotion.backends.common.read_profile", side_effect=lambda *a, **k: self.profile
        ).start()
        self.io = InteractionConnectors(self.f.engine)
        self.io.model.status = lambda: {"configured": True}
        self.account = {**self.f.account, "mode": "live"}
        self.http = Mock()
        self.api = XAPI(self.profile, http=self.http, clock=lambda: self.f.now)

    def test_legacy_profile_cannot_pass_official_readiness_or_call_old_bridge(self):
        with patch.dict(os.environ, {"AP_X_BACKEND": "legacy"}):
            caps = self.io.capabilities(self.account)
            self.assertIn("x_api_builtin_backend_required", caps["blockers"])
            with patch.object(self.f.engine, "call", side_effect=AssertionError("legacy invoked")):
                with self.assertRaisesRegex(Conflict, "x_api_builtin_backend_required"):
                    self.io.discover(
                        self.account, "q", self.f.now - 600, self.f.now, None, self.f.cfg
                    )

    def test_every_selected_account_must_use_official_channel_not_only_scanner(self):
        f = self.f
        self.profile["connection_type"] = "browser"
        caps = self.io.capabilities(self.account)
        f.io.capabilities = lambda a: (
            {"transport": "x_api", "model": {"configured": True}} if a["id"] == "scanner" else caps
        )
        f.s.worker_available = lambda: True
        report = f.s.readiness(f.cfg, [{**self.account, "id": "scanner"}, self.account])
        self.assertFalse(report["ready"])
        self.assertIn("x_api_account_required", report["blockers"])
        self.assertEqual(report["accounts"][1]["blockers"], caps["blockers"])

    def test_missing_token_is_local_configuration_not_verified_api_health(self):
        del self.profile["access_token"]
        caps = self.io.capabilities(self.account)
        self.assertIn("x_api_token_missing", caps["blockers"])
        self.assertNotIn("SYNTHETIC_TOKEN_CANARY", str(caps))

    def test_search_rejects_ambiguous_empty_or_malformed_coverage(self):
        good = {"data": [self.tweet()], "meta": {"result_count": 1}}
        for response in (
            {},
            {"data": []},
            {"meta": {}},
            {"meta": {"result_count": True}},
            {"meta": {"result_count": -1}},
            {"meta": {"result_count": 1}},
            {"data": [], "meta": {"result_count": 0, "next_token": ""}},
            {"data": [], "meta": {"result_count": 0, "next_token": 123}},
            {**good, "meta": {"result_count": 0}},
            {"data": [None], "meta": {"result_count": 1}},
        ):
            with self.subTest(response=response):
                self.http.request.return_value = response
                with self.assertRaisesRegex(BackendError, "XAPISearchResponseInvalid"):
                    self.search()

    def tweet(self, event="9007199254740993123"):
        return {
            "id": event,
            "author_id": "200",
            "text": "Need help creating a coherent video scene",
            "created_at": self.f.iso(self.f.now),
            "lang": "en",
        }

    def search(self, cursor=None):
        return self.api.discover(
            {
                "high_frequency": True,
                "query": "q",
                "lower_at": self.f.now - 600,
                "upper_at": self.f.now,
                "cursor": cursor,
            }
        )

    def test_explicit_empty_page_is_complete_and_partial_error_is_not(self):
        self.http.request.return_value = {"meta": {"result_count": 0}}
        self.assertEqual(self.search()["candidates"], [])
        self.assertTrue(self.search()["coverage_complete"])
        self.http.request.return_value = {
            "meta": {"result_count": 0},
            "errors": [{"title": "partial"}],
        }
        self.assertFalse(self.search()["coverage_complete"])

    def test_official_source_url_reaches_original_frozen_queue_with_delay_and_no_early_send(self):
        f = self.f
        f.config(publishing={"enqueue_delay_min_seconds": 600, "enqueue_delay_max_seconds": 900})
        self.http.request.return_value = {"data": [self.tweet()], "meta": {"result_count": 1}}
        real_source = self.search()["candidates"][0]
        classification = source(1, "P1", f.now)["simulation_classification"]
        classification["intent_evidence"] = [real_source["text"]]
        f.io.classify = lambda *args: copy.deepcopy(classification)
        f.round([real_source])
        f.s.run_stage("generate")
        with patch.object(f.s, "draw_delay", return_value=733):
            f.s.run_stage("publish")
        candidate = f.candidate(real_source["id"])
        self.assertIsNotNone(candidate["item_id"], candidate["block_reason"])
        task = f.engine.task(candidate["task_id"])
        item = task["items"][0]
        self.assertEqual(item["payload"]["target"], real_source["url"])
        self.assertEqual(item["due_at"], f.now + 733)
        drain_sender(f.engine)
        self.assertEqual(f.store.rows("SELECT * FROM attempts"), [])
        f.now += 733
        drain_sender(f.engine)
        self.assertEqual(len(f.store.rows("SELECT * FROM attempts")), 1)

    def test_read_errors_keep_actionable_safe_codes_without_reclassifying_writes(self):
        for status, expected in (
            (401, "XAPIAuthenticationRejected"),
            (403, "XAPIAccessDenied"),
            (429, "XAPIRateLimited"),
        ):
            with self.subTest(status=status):
                self.http.request.side_effect = Rejected(status)
                result = self.search()
                self.assertEqual(result["reason"], expected)
                self.assertFalse(result["coverage_complete"])
                self.assertEqual(result["retry_after_seconds"], 900 if status == 429 else 0)
        # Existing write rejection type remains usable by execute's once-only handling.
        self.assertEqual(Rejected(429).status, 429)

    def use_official_scanner(self):
        f = self.f
        with f.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET mode='live',min_interval=120")
        f.s.io = self.io
        f.s.worker_available = lambda: True
        call = patch.object(
            f.engine,
            "call",
            side_effect=lambda a, op, payload, *args: (
                self.api.discover(payload)
                if op == "discover"
                else (_ for _ in ()).throw(AssertionError("unexpected platform operation"))
            ),
        )
        call.start()
        return call

    def test_pages_share_window_and_real_model_classification_receives_official_source(self):
        f = self.f
        self.use_official_scanner()
        self.http.request.side_effect = [
            {"meta": {"result_count": 0, "next_token": "page-2"}},
            {"data": [self.tweet()], "meta": {"result_count": 1}},
        ]
        classification = source(1, "P1", f.now)["simulation_classification"]
        classification["intent_evidence"] = [self.tweet()["text"]]
        self.io.model.structured = Mock(return_value=classification)
        run = f.s.trigger(f.p["id"], manual=True)["run_id"]
        f.s.run_stage("scan")
        self.assertEqual(len(f.store.rows("SELECT * FROM hf_candidates")), 1)
        f.s.run_stage("classify")
        self.assertEqual(f.candidate(self.tweet()["id"])["priority"], "P1")
        self.assertEqual(self.io.model.structured.call_count, 1)
        model_input = self.io.model.structured.call_args.args[2]["source"]
        self.assertEqual(model_input["url"], "https://x.com/i/web/status/" + self.tweet()["id"])
        params = [c.kwargs["params"] for c in self.http.request.call_args_list]
        self.assertEqual(params[0]["end_time"], params[1]["end_time"])
        self.assertEqual(params[1]["next_token"], "page-2")
        self.assertEqual(
            f.store.one("SELECT pages,complete FROM hf_query_progress WHERE run_id=?", (run,)),
            {"pages": 2, "complete": 1},
        )
        self.assertEqual(f.store.one("SELECT watermark FROM hf_watermarks")["watermark"], f.now)

    def test_later_page_rate_limit_retains_raw_work_and_cooldown_across_restart(self):
        f = self.f
        self.use_official_scanner()
        self.http.request.side_effect = [
            {"data": [self.tweet()], "meta": {"result_count": 1, "next_token": "page-2"}},
            Rejected(429, 1800),
        ]
        run = f.s.trigger(f.p["id"], manual=True)["run_id"]
        f.s.run_stage("scan")
        self.assertEqual(
            f.store.one("SELECT error FROM hf_scan_runs WHERE id=?", (run,))["error"],
            "XAPIRateLimited",
        )
        self.assertEqual(f.store.rows("SELECT * FROM hf_watermarks"), [])
        self.assertEqual(len(f.store.rows("SELECT * FROM hf_candidates")), 1)
        self.assertEqual(
            len(f.store.rows("SELECT * FROM execution_jobs WHERE kind='hf_classify'")), 1
        )
        self.assertEqual(f.s.policy(f.p["id"])["next_scan_at"], f.now + 1800)
        self.assertEqual(f.s.policy(f.p["id"])["empty_streak"], 0)
        f.s = Interactions(f.engine, connectors=self.io, worker_available=lambda: True)
        f.now += 600
        self.assertIn("x_api_read_cooldown", f.s.status(f.p["id"])["readiness"]["blockers"])
        f.s.trigger(f.p["id"], manual=True)
        f.s.run_stage("scan")
        self.assertEqual(self.http.request.call_count, 2)
        f.now += 1200
        self.http.request.side_effect = None
        self.http.request.return_value = {"meta": {"result_count": 0}}
        f.s.trigger(f.p["id"], manual=True)
        f.s.run_stage("scan")
        self.assertEqual(self.http.request.call_count, 3)
        self.assertEqual(f.s.policy(f.p["id"])["empty_streak"], 0)

    def test_malformed_second_page_never_advances_watermark_or_drops_first_page(self):
        f = self.f
        self.use_official_scanner()
        self.http.request.side_effect = [
            {"data": [self.tweet()], "meta": {"result_count": 1, "next_token": "page-2"}},
            {},
        ]
        run = f.s.trigger(f.p["id"], manual=True)["run_id"]
        f.s.run_stage("scan")
        self.assertEqual(
            f.store.one("SELECT error FROM hf_scan_runs WHERE id=?", (run,))["error"],
            "XAPISearchResponseInvalid",
        )
        self.assertEqual(len(f.store.rows("SELECT * FROM hf_candidates")), 1)
        self.assertEqual(f.store.rows("SELECT * FROM hf_watermarks"), [])
        f.s.settle_rounds()
        self.assertEqual(f.s.policy(f.p["id"])["empty_streak"], 0)

    def test_another_policy_cooldown_after_page_one_stops_the_next_read(self):
        f = self.f
        self.use_official_scanner()

        def first_page(*args, **kwargs):
            with f.store.connect(write=True) as db:
                db.execute(
                    "INSERT INTO meta(key,value) VALUES(?,?)",
                    ("hf_x_read_after:" + f.account["id"], str(f.now + 3600)),
                )
            return {"data": [self.tweet()], "meta": {"result_count": 1, "next_token": "page-2"}}

        self.http.request.side_effect = first_page
        f.s.trigger(f.p["id"], manual=True)
        f.s.run_stage("scan")
        self.assertEqual(self.http.request.call_count, 1)
        self.assertEqual(len(f.store.rows("SELECT * FROM hf_candidates")), 1)
        self.assertEqual(f.store.rows("SELECT * FROM hf_watermarks"), [])
        self.assertEqual(f.s.policy(f.p["id"])["next_scan_at"], f.now + 3600)

    def test_safe_error_survives_actual_engine_wrapping_into_failed_round(self):
        f = self.f

        def discover(account, query, lower, upper, cursor, config):
            return f.engine.call(
                account,
                "discover",
                {
                    "high_frequency": True,
                    "query": query,
                    "lower_at": lower,
                    "upper_at": upper,
                    "cursor": cursor,
                },
            )

        f.io.discover = discover
        self.http.request.return_value = {}
        with patch.object(
            f.engine.adapters.simulation,
            "call",
            side_effect=lambda a, op, payload: self.api.discover(payload),
        ):
            run = f.round(classify=False)
        self.assertEqual(run["error"], "XAPISearchResponseInvalid")
        self.assertEqual(f.store.rows("SELECT * FROM hf_watermarks"), [])

    def test_credential_failure_and_missing_model_never_count_as_empty_round(self):
        f = self.f
        self.use_official_scanner()
        self.http.request.side_effect = Rejected(401)
        f.s.trigger(f.p["id"], manual=True)
        f.s.run_stage("scan")
        self.assertEqual(f.s.status(f.p["id"])["runs"][0]["error"], "XAPIAuthenticationRejected")
        self.io.model.status = lambda: {"configured": False}
        f.now += 600
        f.s.trigger(f.p["id"], manual=True)
        f.s.run_stage("scan")
        self.assertEqual(self.http.request.call_count, 1)
        f.s.settle_rounds()
        self.assertEqual(f.s.policy(f.p["id"])["empty_streak"], 0)
        self.assertEqual(f.store.rows("SELECT * FROM hf_watermarks"), [])

    def test_final_claim_refuses_channel_switch_without_an_attempt(self):
        from auto_promotion.interaction_intents import candidate_guard

        f = self.f
        f.round([source(2001, "P1", f.now)])
        f.s.run_stage("generate")
        f.s.run_stage("publish")
        item = f.candidate(2001)["item_id"]
        with f.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET mode='live'")
        with patch.dict(os.environ, {"AP_X_BACKEND": "legacy"}), f.store.connect() as db:
            self.assertEqual(candidate_guard(db, item, f.now), "x_api_builtin_backend_required")
        self.profile["connection_type"] = "browser"
        with f.store.connect() as db:
            self.assertEqual(candidate_guard(db, item, f.now), "x_api_account_required")

    def test_official_url_unknown_remains_locked_through_restart_and_repeat_scan(self):
        f = self.f
        self.http.request.return_value = {"data": [self.tweet()], "meta": {"result_count": 1}}
        real_source = self.search()["candidates"][0]
        f.io.classify = lambda *args: {
            **source(1, "P1", f.now)["simulation_classification"],
            "intent_evidence": [real_source["text"]],
        }
        calls = []
        original = f.engine.adapters.simulation.call

        def unknown(account, operation, payload):
            if operation in {"execute", "verify"}:
                if operation == "execute":
                    calls.append(payload["item_id"])
                return {"status": "unknown", "verified": False, "submission_started": True}
            return original(account, operation, payload)

        f.engine.adapters.simulation.call = unknown
        f.round([real_source])
        f.generate_publish()
        f.s = Interactions(f.engine, connectors=f.io)
        f.now += 600
        f.round([real_source])
        f.generate_publish()
        self.assertEqual(f.candidate(real_source["id"])["state"], "VERIFYING")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(f.store.rows("SELECT * FROM attempts")), 1)

    def test_url_validation_is_exact_not_arbitrary_web_path(self):
        f = self.f
        task = f.engine.create_task(
            TaskInput(
                name="urls",
                account_ids=[f.account["id"]],
                policy={"starts_at": f.iso(f.now - 1), "ends_at": f.iso(f.now + 3600)},
            )
        )
        for url in ("https://x.com/i/web/status/123", "https://twitter.com/example/status/123/"):
            f.engine.validate_item(
                task,
                ItemInput(
                    account_id=f.account["id"],
                    action="reply",
                    target=url,
                    text="x",
                    scheduled_at=f.iso(f.now),
                ),
                account=f.account,
            )
        for url in (
            "https://x.com.evil/i/web/status/123",
            "https://evil@x.com/i/web/status/123",
            "https://x.com/i/web/status/123?token=CANARY",
            "https://x.com/i/web/status/123#x",
            "https://x.com/i/web/extra/status/123",
            "https://x.com:444/i/web/status/123",
        ):
            with self.subTest(url=url), self.assertRaises(Conflict):
                f.engine.validate_item(
                    task,
                    ItemInput(
                        account_id=f.account["id"],
                        action="reply",
                        target=url,
                        text="x",
                        scheduled_at=f.iso(f.now),
                    ),
                    account=f.account,
                )

    def test_api_call_history_never_reports_incomplete_search_as_ok(self):
        f = self.f
        for reason in (
            "XAPIAuthenticationRejected",
            "XAPIAccessDenied",
            "XAPIRateLimited",
            "XAPIPartialSearch",
        ):
            with patch.object(
                f.engine.adapters.simulation,
                "call",
                return_value={
                    "candidates": [],
                    "coverage_complete": False,
                    "reason": reason,
                },
            ):
                f.engine.call(f.account, "discover", {})
            row = f.store.rows("SELECT status,error_code FROM calls ORDER BY id DESC LIMIT 1")[0]
            self.assertEqual(row, {"status": "error", "error_code": reason})

    def test_rate_limit_http_headers_do_not_retry_early_or_leak_response_body(self):
        import io
        from urllib.error import HTTPError

        error = HTTPError(
            "https://api.x.com/2/tweets/search/recent",
            429,
            "SECRET_ERROR_CANARY",
            {"Retry-After": "1800", "x-rate-limit-reset": str(int(self.f.now + 3600))},
            io.BytesIO(b"SECRET_BODY_CANARY"),
        )
        opener = Mock()
        opener.open.side_effect = error
        with (
            patch("auto_promotion.backends.x_api.build_opener", return_value=opener),
            patch("auto_promotion.backends.x_api.time.time", return_value=self.f.now),
        ):
            with self.assertRaises(Rejected) as caught:
                XHTTP(self.profile).request("GET", "tweets/search/recent")
        self.assertEqual(caught.exception.retry_after_seconds, 3600)
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertTrue(error.fp.closed)

    def test_invalid_or_http_date_retry_header_never_discards_valid_reset(self):
        from email.utils import formatdate
        from urllib.error import HTTPError

        for retry, expected in (
            ("invalid", 3600),
            (formatdate(self.f.now + 7200, usegmt=True), 7201),
        ):
            with self.subTest(retry=retry):
                error = HTTPError(
                    "https://api.x.com/2/tweets/search/recent",
                    429,
                    "CANARY",
                    {"Retry-After": retry, "x-rate-limit-reset": str(int(self.f.now + 3600))},
                    None,
                )
                opener = Mock()
                opener.open.side_effect = error
                with (
                    patch("auto_promotion.backends.x_api.build_opener", return_value=opener),
                    patch("auto_promotion.backends.x_api.time.time", return_value=self.f.now),
                ):
                    with self.assertRaises(Rejected) as caught:
                        XHTTP(self.profile).request("GET", "tweets/search/recent")
                self.assertEqual(caught.exception.retry_after_seconds, expected)


if __name__ == "__main__":
    unittest.main()
