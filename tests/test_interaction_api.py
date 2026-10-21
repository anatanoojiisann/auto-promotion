import tempfile
import unittest
from pathlib import Path

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
