import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import test_statistics

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.backends.engagement import api_metrics, metrics_result, parse_count, post_id
from auto_promotion.core import Engine
from auto_promotion.engagement import INTERVAL, Engagement
from auto_promotion.store import Store


class EngagementTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_statistics.StatisticsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store, self.now = self.fixture.store, self.fixture.now
        self.item = self.fixture.item(
            platform="x",
            action="reply",
            published=self.now - 500,
            reference="https://x.com/our_account/status/123456",
        )
        self.adapter = Mock()
        self.engine = Engine(
            self.store,
            AdapterRegistry(Path(self.store.path).parent, overrides={"x": self.adapter}),
            clock=lambda: self.now,
            live_enabled=False,
        )
        self.metrics = Engagement(self.engine)
        self.adapter.call.side_effect = lambda a, operation, payload: self.result()

    def result(self, **values):
        return {
            "verified": True,
            "engagement_subject_verified": True,
            "external_reference": "https://x.com/our_account/status/123456",
            "engagement_observed_at": self.now,
            "engagement_source": "x_api_public_metrics",
            "engagement_metrics": {"views": 15, "likes": 0, "replies": 2, **values},
        }

    def test_periodic_reads_survive_restart_and_never_change_execution(self):
        before = {
            t: self.store.rows("SELECT * FROM " + t)
            for t in ("items", "tasks", "attempts", "feedback_checks")
        }
        self.assertEqual(len(self.metrics.run_due()["processed"]), 1)
        self.assertEqual(self.adapter.call.call_args.args[1], "engagement")
        self.assertTrue(self.adapter.call.call_args.args[2]["read_only"])
        self.assertEqual(Engagement(self.engine).run_due()["processed"], [])
        self.now += INTERVAL
        self.metrics.run_due()
        self.assertEqual(self.adapter.call.call_count, 2)
        self.assertEqual(len(self.metrics.history(self.item)), 2)
        self.assertEqual(before, {t: self.store.rows("SELECT * FROM " + t) for t in before})

    def test_missing_is_null_zero_is_real_and_decreases_are_retained(self):
        self.metrics.run_due()
        self.assertEqual(self.metrics.snapshot()["items"][0]["metrics"]["likes"], 0)
        self.now += INTERVAL
        self.adapter.call.side_effect = lambda *a: self.result(views=10, likes=None, replies=0)
        self.metrics.run_due()
        row = self.metrics.snapshot()["items"][0]
        self.assertEqual(row["delta"]["views"], -5)
        self.assertIsNone(row["metrics"]["likes"])
        self.assertEqual(row["delta"]["replies"], -2)

    def test_failure_preserves_previous_snapshot_and_hides_stale_current_counts(self):
        self.metrics.run_due()
        self.now += INTERVAL
        self.adapter.call.side_effect = RuntimeError("token=never store this")
        self.metrics.run_due()
        samples = self.metrics.history(self.item)
        self.assertEqual(len(samples), 2)
        self.assertIsNone(samples[0]["metrics"]["views"])
        self.assertEqual(samples[1]["metrics"]["views"], 15)
        self.assertNotIn("token=", json.dumps(samples))

    def test_subject_binding_and_fresh_evidence_are_required(self):
        row = self.metrics.publications()[0]
        for change in (
            {"external_reference": "https://x.com/parent/status/999"},
            {"engagement_subject_verified": False},
            {"engagement_observed_at": self.now - 600},
            {"verified": False},
        ):
            saved = self.metrics.save(row, {**self.result(), **change})
            self.assertTrue(all(v is None for v in saved["metrics"].values()))

    def test_compact_metrics_do_not_produce_exact_deltas(self):
        row = self.metrics.publications()[0]
        self.metrics.save(row, {**self.result(), "engagement_estimated": ["views"]})
        self.now += INTERVAL
        self.metrics.save(row, self.result(views=20))
        self.assertIsNone(self.metrics.snapshot()["items"][0]["delta"]["views"])

    def test_private_messages_unsupported_platforms_and_simulation_do_not_open_browser(self):
        self.fixture.item(
            platform="tiktok", action="reply_direct_message", published=self.now - 100
        )
        self.fixture.item(platform="instagram", action="comment", published=self.now - 100)
        self.fixture.item(platform="x", mode="simulation", published=self.now - 100)
        self.fixture.item(platform="x", verified=False, published=self.now - 100)
        self.metrics.run_due()
        self.assertEqual(self.adapter.call.call_count, 1)
        rows = self.metrics.snapshot()["items"]
        self.assertEqual(len(rows), 3)
        self.assertEqual(
            {r["capability"] for r in rows}, {"supported", "unavailable", "not_applicable"}
        )

    def test_account_disabled_or_cooling_does_not_read(self):
        with self.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET cooldown_until=?", (self.now + 10,))
        self.metrics.run_due()
        self.adapter.call.assert_not_called()

    def test_two_collectors_claim_only_once(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: Engagement(self.engine).run_due(), range(2)))
        self.assertEqual(sum(len(r["processed"]) for r in results), 1)
        self.assertEqual(self.adapter.call.call_count, 1)

    def test_schema_additions_migrate_existing_database(self):
        with self.store.connect(write=True) as db:
            db.execute("DROP TABLE engagement_polls")
            db.execute("DROP TABLE engagement_snapshots")
        Store(self.store.path)
        self.assertEqual(self.metrics.history(self.item), [])

    def test_api_routes_read_saved_data_without_platform_calls(self):
        from auto_promotion.api import create_app

        self.metrics.save(self.metrics.publications()[0], self.result())
        app = create_app(
            db_path=self.store.path, registry=self.engine.adapters, token="test", worker=False
        )
        routes = {r.path: r.endpoint for r in app.routes if hasattr(r, "endpoint")}
        response = routes["/api/engagement"]()
        self.assertEqual(response["items"][0]["metrics"]["likes"], 0)
        history = routes["/api/items/{item_id}/engagement"](self.item)
        self.assertEqual(len(history["samples"]), 1)
        self.adapter.call.assert_not_called()


class MetricsParsingTests(unittest.TestCase):
    def test_compact_and_exact_counts(self):
        for text, expected in (
            ("1,234 Likes", (1234, False)),
            ("1.2K", (1200, True)),
            ("2.5万", (25000, True)),
            ("0 Replies", (0, False)),
            ("Reply", (None, False)),
            ("1.234", (None, False)),
            ("", (None, False)),
        ):
            self.assertEqual(parse_count(text), expected)

    def test_exact_accessibility_label_wins_over_rounded_text(self):
        result = metrics_result(
            {
                "subject": True,
                "views": ["1.2K Views", "1,234 Views"],
                "likes": ["Like"],
                "replies": ["0 Replies"],
            },
            "reference",
        )
        self.assertEqual(
            result["engagement_metrics"],
            {"views": 1234, "likes": None, "replies": 0, "reposts": None},
        )
        self.assertEqual(result["engagement_estimated"], [])

    def test_only_canonical_platform_status_references(self):
        self.assertEqual(post_id("https://x.com/i/web/status/1234"), "1234")
        for url in (
            "https://x.com.evil.test/a/status/1",
            "http://x.com/a/status/1",
            "https://x.com/a",
            "https://x.com/a/status/1/photo/1",
        ):
            with self.assertRaises(RuntimeError):
                post_id(url)

    def test_api_uses_our_publication_and_get_only(self):
        settings = {"user_id": "12"}
        with patch("auto_promotion.backends.x_api.XAPI") as constructor:
            api = constructor.return_value
            api.health.return_value = {"healthy": True}
            api.http.request.return_value = {
                "data": {
                    "id": "123",
                    "author_id": "12",
                    "public_metrics": {"like_count": 0, "impression_count": 42, "reply_count": 3},
                }
            }
            result = api_metrics(settings, "ref", "123")
            self.assertEqual(result["engagement_metrics"]["views"], 42)
            self.assertEqual(api.http.request.call_args.args, ("GET", "tweets/123"))
            api.http.request.return_value["data"]["author_id"] = "other"
            with self.assertRaises(RuntimeError):
                api_metrics(settings, "ref", "123")


if __name__ == "__main__":
    unittest.main()
