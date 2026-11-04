import csv
import io
import unittest
from pathlib import Path
from unittest.mock import Mock

import test_statistics
from fastapi.testclient import TestClient

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.api import create_app
from auto_promotion.core import Engine
from auto_promotion.engagement import Engagement
from auto_promotion.engagement_effects import EngagementEffects
from auto_promotion.models import canonical
from auto_promotion.statistics import day_bounds


class EngagementEffectsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_statistics.StatisticsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store, self.now = self.fixture.store, self.fixture.now
        self.adapter = Mock()
        self.engine = Engine(
            self.store,
            AdapterRegistry(Path(self.store.path).parent, overrides={"x": self.adapter}),
            clock=lambda: self.now,
            live_enabled=False,
        )
        self.engagement = Engagement(self.engine)
        self.effects = EngagementEffects(self.engagement)

    def item(self, text="Example content", **kwargs):
        options = {"platform": "x", "action": "reply", "published": self.now - 3600, **kwargs}
        key = self.fixture.item(**options)
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE items SET payload=? WHERE id=?",
                (canonical({"action": options["action"], "text": text, "target": "target"}), key),
            )
        return key

    def sample(self, key, views=10, likes=0, replies=2, estimated=None):
        row = next(row for row in self.engagement.publications() if row["item_id"] == key)
        self.engagement.save(
            row,
            {
                "verified": True,
                "engagement_subject_verified": True,
                "external_reference": row["external_reference"],
                "engagement_observed_at": self.now,
                "engagement_source": "test",
                "engagement_estimated": estimated or [],
                "engagement_metrics": {"views": views, "likes": likes, "replies": replies},
            },
        )

    def test_publication_dates_use_beijing_midnight_and_never_planned_time(self):
        midnight, end = day_bounds("2026-10-13")
        self.fixture.now = self.now = end + 60
        inside = self.item(published=midnight, due=midnight - 100)
        self.item(published=midnight - 1, due=midnight + 100)
        self.item(published=end)
        unknown = self.item(published=None)
        result = self.effects.snapshot(date_from="2026-10-13", date_to="2026-10-13")
        self.assertEqual([i["item_id"] for i in result["items"]], [inside])
        self.assertEqual(result["unknown_publication_total"], 1)
        self.assertEqual(
            self.effects.snapshot(publication_time="unknown")["items"][0]["item_id"], unknown
        )
        self.assertEqual(self.effects.snapshot()["total"], 4)

    def test_combined_filters_search_and_options(self):
        wanted = self.item("Hello Needle 世界")
        self.sample(wanted)
        self.item("Other text", platform="telegram")
        self.item("Needle without sample")
        result = self.effects.snapshot(
            platform="x",
            account_id=wanted,
            task_id=wanted,
            q="nEEdLe 世界",
            status="complete",
        )
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["text"], "Hello Needle 世界")
        self.assertEqual(result["all_total"], 3)
        self.assertEqual(len(result["options"]["accounts"]), 3)
        self.assertEqual(self.effects.snapshot(q="' OR 1=1 --")["total"], 0)
        self.assertEqual(
            self.effects.snapshot(q="NO MATCH")["totals"]["views"]["available_items"], 0
        )

    def test_numeric_sort_keeps_zero_before_missing_in_both_directions(self):
        zero, high, missing, tie = [self.item() for _ in range(4)]
        self.sample(zero, views=0)
        self.sample(high, views=20)
        self.sample(tie, views=20)
        self.assertEqual(
            [i["item_id"] for i in self.effects.snapshot(sort="views")["items"]],
            [high, tie, zero, missing],
        )
        self.assertEqual(
            [i["item_id"] for i in self.effects.snapshot(sort="views", direction="asc")["items"]],
            [zero, high, tie, missing],
        )
        self.assertEqual(self.effects.snapshot(sort="likes")["items"][-1]["item_id"], missing)

    def test_summary_and_export_cover_all_matches_across_pages(self):
        for index in range(53):
            key = self.item("batch" if index < 52 else "other")
            self.sample(key, views=index, likes=0, replies=1)
        first = self.effects.snapshot(q="batch", limit=25)
        second = self.effects.snapshot(q="batch", limit=25, offset=25)
        self.assertEqual((len(first["items"]), first["total"]), (25, 52))
        self.assertEqual(first["totals"], second["totals"])
        self.assertEqual(first["totals"]["views"]["observed_total"], sum(range(52)))
        rows = list(csv.DictReader(io.StringIO(self.effects.export(q="batch").decode("utf-8-sig"))))
        self.assertEqual(len(rows), 52)
        self.assertEqual(sum(int(row["观看累计"]) for row in rows), sum(range(52)))

    def test_failures_missing_approximation_and_history_remain_distinct(self):
        failed, unsupported, pending, private = (
            self.item(),
            self.item(platform="instagram"),
            self.item(),
            self.item(platform="tiktok", action="send_direct_message"),
        )
        self.sample(failed, views=100, estimated=["views"])
        self.now += 1
        self.sample(failed, views=90, likes=None)
        row = self.effects.snapshot(account_id=failed)["items"][0]
        self.assertIsNone(row["delta"]["views"])
        self.assertEqual(row["collection_state"], "partial")
        self.now += 1
        publication = next(r for r in self.engagement.publications() if r["item_id"] == failed)
        self.engagement.save(publication, {"reason": "AccountBusy"})
        self.assertEqual(self.effects.snapshot(status="failed")["items"][0]["item_id"], failed)
        self.assertEqual(
            self.effects.snapshot(status="unavailable")["items"][0]["item_id"], unsupported
        )
        self.assertEqual(self.effects.snapshot(status="pending")["items"][0]["item_id"], pending)
        self.assertEqual(
            self.effects.snapshot(status="not_applicable")["items"][0]["item_id"], private
        )
        detail = self.effects.detail(failed, limit=2)
        self.assertEqual((len(detail["samples"]), detail["sample_total"]), (2, 3))
        self.assertIsNone(detail["item"]["metrics"]["views"])
        self.assertEqual(detail["samples"][1]["metrics"]["views"], 90)
        self.now += 12000
        self.assertEqual(self.effects.snapshot(status="stale")["total"], 1)

    def test_export_is_utf8_bom_quotes_multiline_and_neutralizes_formulas(self):
        key = self.item(' \t=HYPERLINK("evil")\n第二行,"quoted"')
        self.sample(key, views=12, likes=None)
        self.now += 1
        self.sample(key, views=10, likes=None, estimated=["replies"])
        raw = self.effects.export()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
        self.assertTrue(rows[0]["正文"].startswith("' \t="))
        self.assertIn('\n第二行,"quoted"', rows[0]["正文"])
        self.assertEqual(rows[0]["点赞累计"], "")
        self.assertEqual(rows[0]["回复累计"], "约 2")
        self.assertEqual(rows[0]["观看较上次变化"], "-2")
        self.assertIn("+08:00", rows[0]["发布时间(北京时间)"])

    def test_invalid_queries_and_pagination_are_rejected(self):
        for query in (
            {"sort": "payload"},
            {"direction": "sideways"},
            {"status": "confirmed"},
            {"date_from": "2026-13-01"},
            {"date_from": "20261013"},
            {"date_from": "2026-10-14", "date_to": "2026-10-13"},
            {"q": "a" * 501},
            {"limit": 101},
            {"offset": -1},
            {"publication_time": "planned"},
        ):
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.effects.snapshot(**query)

    def test_read_only_scope_excludes_simulation_unknown_and_unverified_publications(self):
        key = self.item()
        self.sample(key)
        self.item(mode="simulation")
        hidden = self.item(verified=False)
        self.item(status="unknown")
        self.item(reference="simulation://not-real")
        tables = ("items", "tasks", "attempts", "meta", "engagement_snapshots", "engagement_polls")
        before = {table: self.store.rows("SELECT * FROM " + table) for table in tables}
        self.assertEqual(self.effects.snapshot()["total"], 1)
        self.assertIsNone(self.effects.detail(hidden))
        self.effects.export()
        self.effects.detail(key)
        self.assertEqual(
            before, {table: self.store.rows("SELECT * FROM " + table) for table in tables}
        )
        self.adapter.call.assert_not_called()

    def test_http_routes_authentication_filters_export_detail_and_validation(self):
        key = self.item("needle")
        self.sample(key)
        app = create_app(
            db_path=self.store.path,
            registry=self.engine.adapters,
            clock=lambda: self.now,
            worker=False,
            report_worker=False,
            token="test",
        )
        with TestClient(app) as client:
            for path in (
                "/api/engagement/effects",
                "/api/engagement/export",
                f"/api/engagement/items/{key}",
            ):
                self.assertEqual(client.get(path).status_code, 401)
            client.headers["Authorization"] = "Bearer test"
            response = client.get("/api/engagement/effects", params={"q": "needle", "limit": 25})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["total"], 1)
            self.assertFalse(response.json()["scheduler_running"])
            exported = client.get("/api/engagement/export", params={"q": "needle"})
            self.assertEqual(exported.status_code, 200)
            self.assertIn("attachment", exported.headers["Content-Disposition"])
            self.assertEqual(
                len(list(csv.reader(io.StringIO(exported.content.decode("utf-8-sig"))))), 2
            )
            self.assertEqual(client.get(f"/api/engagement/items/{key}").json()["sample_total"], 1)
            self.assertEqual(client.get("/api/engagement/items/missing").status_code, 404)
            for path in ("/api/engagement/effects", "/api/engagement/export"):
                self.assertEqual(client.get(path, params={"sort": "not_allowed"}).status_code, 422)
                self.assertEqual(
                    client.get(path, headers={"Origin": "https://evil.example"}).status_code, 403
                )
            self.assertEqual(client.get(f"/api/engagement/items/{key}?limit=1001").status_code, 422)
            self.assertEqual(client.get("/api/engagement").json()["total"], 1)
        self.adapter.call.assert_not_called()
