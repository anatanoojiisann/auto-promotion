import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from auto_promotion.api import create_app
from auto_promotion.interaction_simulation import OfflineRegistry, SimulationConnectors, source
from auto_promotion.models import AccountInput


class InteractionAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1800000000.0
        self.app = create_app(
            db_path=Path(self.tmp.name) / "api.db",
            registry=OfflineRegistry(Path(self.tmp.name)),
            token="test-only-token",
            clock=lambda: self.now,
        )
        self.client = TestClient(self.app)
        self.client.headers["Authorization"] = "Bearer test-only-token"
        engine = self.app.state.engine
        account = engine.add_account(
            AccountInput(platform="x", mode="simulation", label="api-sim", backend_label="api-sim")
        )
        engine.check_account(account["id"])
        self.config = self.client.get("/api/interactions/defaults").json()
        self.config.update(
            enabled=True,
            account_ids=[account["id"]],
            queries=["synthetic"],
            facts=[
                {
                    "id": "demo-fact",
                    "text": "Synthetic fact",
                    "source": "simulation://fact",
                    "verified_at": self.now - 100,
                    "valid_until": self.now + 86400,
                }
            ],
        )

    def save(self):
        response = self.client.post("/api/interactions", json={"config": self.config})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_private_routes_and_config_version_conflicts(self):
        self.assertEqual(
            self.client.get("/api/interactions", headers={"Authorization": ""}).status_code, 401
        )
        policy = self.save()
        self.assertEqual(policy["routing"]["P0"], "auto")
        self.assertEqual(
            self.client.post("/api/interactions", json={"config": self.config}).status_code, 409
        )
        self.assertEqual(self.client.get("/api/interactions/" + policy["id"]).status_code, 200)

    def test_candidate_routes_use_real_backend_and_optimistic_versions(self):
        policy = self.save()
        service = self.app.state.interactions
        io = SimulationConnectors(self.app.state.engine)
        service.io = io
        io.sources = [source(2001, "P2", self.now)]
        self.assertEqual(
            self.client.post(f"/api/interactions/{policy['id']}/scan").status_code, 200
        )
        service.run_stage("scan")
        service.run_stage("classify")
        candidates = self.client.get(f"/api/interactions/{policy['id']}/candidates").json()
        self.assertEqual(len(candidates), 1)
        row = candidates[0]
        payload = {"action": "adopt", "versions": {row["id"]: row["version"]}}
        self.assertEqual(
            self.client.post("/api/interactions/actions", json=payload).status_code, 200
        )
        self.assertEqual(
            self.client.post("/api/interactions/actions", json=payload).status_code, 409
        )
        self.assertEqual(
            self.client.get(f"/api/interactions/{policy['id']}/candidates?limit=0").status_code, 409
        )

    def test_isolated_simulation_does_not_modify_current_store(self):
        before = self.app.state.store.rows("SELECT * FROM tasks")
        result = self.client.post("/api/interactions/simulate")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()["passed"])
        self.assertEqual(before, self.app.state.store.rows("SELECT * FROM tasks"))

    def test_defaults_are_disabled_and_invalid_combinations_are_rejected(self):
        defaults = self.client.get("/api/interactions/defaults").json()
        self.assertFalse(defaults["enabled"])
        self.assertEqual(defaults["scan"]["interval_seconds"], 600)
        self.config["scan"]["interval_seconds"] = 3600
        self.assertEqual(
            self.client.post("/api/interactions", json={"config": self.config}).status_code, 422
        )

    def test_live_enable_uses_current_worker_and_model_readiness_without_network(self):
        service = self.app.state.interactions
        with self.app.state.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET mode='live'")
        service.io.capabilities = lambda account: {
            "transport": "x_api",
            "model": {"configured": False},
        }
        self.config.update(run_mode="live", enabled=False)
        report = self.client.post("/api/interactions/readiness", json={"config": self.config})
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.json()["model_connectivity"], "unverified")
        self.assertIn("worker_unavailable", report.json()["blockers"])
        self.assertIn("model_connection_unavailable", report.json()["blockers"])
        self.save()
        self.config["enabled"] = True
        response = self.client.post(
            "/api/interactions", json={"config": self.config, "expected_version": 1}
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("worker_unavailable", response.text)

    def test_account_options_reject_second_browser_account_and_never_export_private_profile(self):
        engine, service = self.app.state.engine, self.app.state.interactions
        first = self.config["account_ids"][0]
        browser = engine.add_account(
            AccountInput(platform="x", mode="simulation", label="browser", backend_label="browser")
        )
        with self.app.state.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET mode='live',min_interval=120")
        service.worker_available = lambda: True
        service.io.model.status = lambda: {"configured": True}

        def profile(platform, label):
            return {
                "connection_type": "x_api" if label == "api-sim" else "browser",
                "identity": "SENSITIVE_IDENTITY",
                "user_id": "100",
                "access_token": "TOKEN_CANARY",
            }

        with patch("auto_promotion.backends.common.read_profile", side_effect=profile):
            response = self.client.get("/api/interactions/accounts")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("CANARY", response.text)
            self.assertNotIn("SENSITIVE_IDENTITY", response.text)
            options = {a["id"]: a for a in response.json()}
            self.assertEqual(options[first]["capabilities"]["blockers"], [])
            self.assertIn(
                "x_api_account_required", options[browser["id"]]["capabilities"]["blockers"]
            )
            self.config["account_ids"].append(browser["id"])
            self.config["run_mode"] = "live"
            result = self.client.post("/api/interactions", json={"config": self.config})
            self.assertEqual(result.status_code, 409)
            self.assertIn("x_api_account_required", result.text)

    def test_import_route_keeps_partial_coverage_and_rejects_permission_fields(self):
        self.config["scan"]["source"] = "imported_results"
        policy = self.save()
        event = {
            k: v for k, v in source(2002, "P1", self.now).items() if not k.startswith("simulation_")
        }
        url = f"/api/interactions/{policy['id']}/imports"
        self.assertEqual(
            self.client.post(
                url, json={"sources": [{**event, "coverage_complete": True}]}
            ).status_code,
            422,
        )
        result = self.client.post(url, json={"sources": [event]})
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json()["coverage_complete"])
        self.assertEqual(
            self.client.post(f"/api/interactions/{policy['id']}/scan").status_code, 409
        )
        report = self.client.get(f"/api/interactions/{policy['id']}").json()
        self.assertEqual(report["runs"][0]["coverage_incomplete"], 1)
        self.assertEqual(report["empty_streak"], 0)

    def test_joint_template_processing_and_schedule_routes(self):
        config = self.client.get("/api/interactions/defaults?profile=joint_p0_p1").json()
        self.assertEqual(config["schema_version"], 2)
        self.assertFalse(config["enabled"])
        self.assertEqual(config["scan"]["min_interval_seconds"], 3000)
        self.assertFalse(config["fallback"]["enabled"])
        saved = self.client.post("/api/interactions", json={"config": config})
        self.assertEqual(saved.status_code, 200, saved.text)
        policy = saved.json()
        path = "/api/interactions/" + policy["id"]
        self.assertEqual(self.client.get(path + "/processing-tasks").json(), [])
        self.assertEqual(self.client.get(path + "/processing-tasks?limit=0").status_code, 409)
        updated = self.client.post(
            path + "/reschedule", json={"expected_version": policy["version"]}
        )
        self.assertEqual(updated.status_code, 200)
        self.assertGreaterEqual(updated.json()["next_scan_at"], self.now + 3000)
        self.assertEqual(
            self.client.post(path + "/reschedule", json={"expected_version": 999}).status_code, 409
        )
        self.assertEqual(self.client.get(path, headers={"Authorization": ""}).status_code, 401)
