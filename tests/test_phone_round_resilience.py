import sqlite3
import threading
import time
from unittest.mock import patch

from fastapi.testclient import TestClient

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.api import create_app
from auto_promotion.core import Engine
from auto_promotion.phone_rounds import PhoneRounds
from auto_promotion.store import Store


def test_idle_round_tick_does_not_compete_with_existing_writer(tmp_path):
    store = Store(tmp_path / "idle.sqlite")
    rounds = PhoneRounds(Engine(store, AdapterRegistry(tmp_path)))
    writer = sqlite3.connect(store.path, timeout=1)
    try:
        writer.execute("BEGIN IMMEDIATE")
        start = time.monotonic()
        rounds.tick()
        assert time.monotonic() - start < 1
    finally:
        writer.rollback()
        writer.close()


def test_health_survives_phone_round_error_and_reports_recovery(tmp_path):
    resumed = threading.Event()
    calls = []

    def transient_failure(_rounds):
        calls.append(True)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        resumed.set()

    app = create_app(db_path=tmp_path / "health.sqlite", worker=True,
                     report_worker=False, pooled_worker=True, phone_rounds_enabled=True)
    with patch.object(PhoneRounds, "tick", transient_failure):
        with TestClient(app) as client:
            assert resumed.wait(6)
            response = client.get("/health")
            assert response.status_code == 200
            status = response.json()["background"]["reports"]["phone_rounds"]
            assert status["alive"] is True
            assert status["last_error"] == "OperationalError"
            assert status["last_error_details"]["exception_type"] == "OperationalError"
            assert status["last_success_at"] >= status["last_error_at"]
