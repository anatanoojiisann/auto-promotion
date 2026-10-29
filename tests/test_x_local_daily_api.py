import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_api import request

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.api import create_app


class LocalDailyAPITests(unittest.TestCase):
    def test_local_activation_requires_auth_and_never_contacts_cloud(self):
        with tempfile.TemporaryDirectory() as folder:
            app = create_app(
                db_path=Path(folder) / "db.sqlite",
                registry=AdapterRegistry(Path(folder)),
                token="test-key",
            )
            with patch(
                "auto_promotion.cloud_control.CloudControl.configured",
                side_effect=AssertionError("unexpected cloud"),
            ):
                unauthorized = asyncio.run(
                    request(app, "/api/accounts/x/local-daily-limit/unlimited", "POST", {}, {})
                )
                self.assertEqual(unauthorized[0], 401)
                status, data, _ = asyncio.run(
                    request(
                        app,
                        "/api/accounts/x/local-daily-limit/unlimited",
                        "POST",
                        {},
                        {"authorization": "Bearer test-key"},
                    )
                )
            self.assertEqual(status, 200)
            self.assertEqual(
                (data["platform"], data["channel"], data["enabled"]), ("x", "local", True)
            )
            self.assertEqual(
                app.state.engine.store.one("SELECT value FROM meta WHERE key='global_stop'")[
                    "value"
                ],
                "1",
            )


if __name__ == "__main__":
    unittest.main()
