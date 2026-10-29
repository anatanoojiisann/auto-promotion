import asyncio
import tempfile
import unittest
from pathlib import Path

from test_api import request

from auto_promotion.alert_store import AlertStore
from auto_promotion.api import create_app


class AlertAPITests(unittest.TestCase):
    def test_status_is_private_read_only_and_does_not_install_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            app = create_app(db_path=path / "business.db", token="operator")
            status, _, _ = asyncio.run(request(app, "/api/alerts/status"))
            self.assertEqual(status, 401)
            status, data, _ = asyncio.run(
                request(app, "/api/alerts/status", headers={"Authorization": "Bearer operator"})
            )
            self.assertEqual(status, 200)
            self.assertEqual(data["service_state"], "not_installed")
            self.assertFalse((path / "alerts.db").exists())
            store = AlertStore(path / "alerts.db")
            with store.connect(write=True) as db:
                store.enqueue(db, "test", "test", "private body")
            _, data, _ = asyncio.run(
                request(app, "/api/alerts/status", headers={"Authorization": "Bearer operator"})
            )
            self.assertEqual(data["queue"], {"pending": 1})
            self.assertNotIn("private body", str(data))


if __name__ == "__main__":
    unittest.main()
