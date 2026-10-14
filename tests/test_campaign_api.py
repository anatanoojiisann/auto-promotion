import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from test_api import request

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.api import create_app


class CampaignAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = create_app(
            db_path=Path(self.tmp.name) / "api.db",
            registry=AdapterRegistry(Path(self.tmp.name)),
            token="pool-test-key",
        )

    def call(self, path, method="GET", body=None, auth=True):
        headers = {"authorization": "Bearer pool-test-key"} if auth else {}
        return asyncio.run(request(self.app, path, method, body, headers))

    def fixture(self):
        status, account, _ = self.call(
            "/api/accounts",
            "POST",
            {
                "platform": "telegram",
                "label": "API test",
                "backend_label": "api-test",
                "mode": "simulation",
                "min_interval": 0,
            },
        )
        self.assertEqual(status, 201)
        self.call(f"/api/accounts/{account['id']}/check", "POST")
        status, profile, _ = self.call(
            f"/api/accounts/{account['id']}/profile",
            "PUT",
            {"languages": ["en"], "topics": ["video"], "reply_template": "Exact approved draft."},
        )
        self.assertEqual((status, profile["version"]), (200, 1))
        status, pool, _ = self.call(
            "/api/pools", "POST", {"name": "API pool", "account_ids": [account["id"]]}
        )
        self.assertEqual(status, 201)
        return account, pool

    def test_new_endpoints_require_authentication(self):
        for path in (
            "/api/pools",
            "/api/campaigns",
            "/api/opportunities",
            "/api/execution/settings",
            "/api/drafts/status",
        ):
            self.assertEqual(self.call(path, auth=False)[0], 401)

    def test_api_plan_and_both_human_approval_stages(self):
        account, pool = self.fixture()
        status, imported, _ = self.call(
            "/api/opportunities/import",
            "POST",
            {
                "items": [
                    {
                        "platform": "telegram",
                        "external_id": "1",
                        "target": "@api_test",
                        "reply_to": 1,
                        "language": "en",
                        "context": "video question",
                        "confidence": 0.95,
                    }
                ]
            },
        )
        self.assertEqual(status, 200)
        now = datetime.now(timezone.utc)
        status, campaign, _ = self.call(
            "/api/campaigns",
            "POST",
            {
                "name": "API campaign",
                "pool_id": pool["id"],
                "opportunity_ids": imported["ids"],
                "policy": {
                    "starts_at": now.isoformat(),
                    "ends_at": (now + timedelta(days=2)).isoformat(),
                    "languages": ["en"],
                    "min_interval": 0,
                },
            },
        )
        self.assertEqual(status, 201)
        status, planned, _ = self.call(f"/api/campaigns/{campaign['id']}/plan", "POST", {})
        self.assertEqual((status, planned["summary"]), (200, {"planned": 1}))
        status, batched, _ = self.call(f"/api/campaigns/{campaign['id']}/materialize", "POST")
        self.assertEqual(status, 200)
        task_id = batched["tasks"][0]["id"]
        task = self.call(f"/api/tasks/{task_id}")[1]
        self.assertEqual(task["approvals"], [])
        self.assertEqual(self.call(f"/api/tasks/{task_id}/populate", "POST")[0], 409)
        status, task, _ = self.call(
            f"/api/tasks/{task_id}/approve/discovery_join",
            "POST",
            {"phrase": task["discovery_phrase"]},
        )
        self.assertEqual((status, len(task["items"])), (200, 1))
        self.assertEqual(task["items"][0]["payload"]["profile_version"], 1)
        status, task, _ = self.call(f"/api/tasks/{task_id}/prepare", "POST")
        self.assertEqual(status, 200)
        status, task, _ = self.call(
            f"/api/tasks/{task_id}/approve/outbound", "POST", {"phrase": task["outbound_phrase"]}
        )
        self.assertEqual((status, task["phase"]), (200, "ready"))
        self.assertEqual(len(task["approvals"]), 2)
        self.assertTrue(
            self.app.state.engine.store.one("SELECT value FROM meta WHERE key='global_stop'")[
                "value"
            ]
            == "1"
        )

    def test_public_message_read_is_local_import_without_approval_or_send(self):
        account, _ = self.fixture()
        status, result, _ = self.call(
            f"/api/accounts/{account['id']}/content",
            "POST",
            {"target": "@public_test", "limit": 1000},
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["imported"], 1)
        item = self.call("/api/opportunities")[1]["items"][0]
        self.assertEqual(item["payload"]["permission"], "unknown")
        self.assertEqual(self.app.state.store.one("SELECT count(*) n FROM attempts")["n"], 0)

    def test_invalid_templates_and_members_are_reviewable_errors(self):
        account, _ = self.fixture()
        self.assertEqual(
            self.call(
                f"/api/accounts/{account['id']}/profile",
                "PUT",
                {"reply_template": "{context.__class__}"},
            )[0],
            409,
        )
        self.assertEqual(
            self.call("/api/pools", "POST", {"name": "bad", "account_ids": ["missing"]})[0], 404
        )

    def test_configuration_does_not_enable_writes(self):
        status, _, _ = self.call(
            "/api/execution/settings",
            "PUT",
            {"global_limit": {"daily": 1000, "hourly": 100}, "max_workers": 30},
        )
        self.assertEqual(status, 200)
        self.assertFalse(self.app.state.engine.live_enabled)
        self.assertEqual(
            self.app.state.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"], "1"
        )
