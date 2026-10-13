import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.api import create_app


async def request(app, path, method="GET", body=None, headers=None):
    raw = json.dumps(body).encode() if body is not None else b""
    sent = []
    received = False

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        sent.append(message)

    hdr = {"host": "testserver", "content-type": "application/json", **(headers or {})}
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in hdr.items()],
        "client": ("testclient", 1234),
        "server": ("testserver", 80),
        "root_path": "",
    }
    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    data = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    try:
        content = json.loads(data)
    except ValueError:
        content = data.decode()
    return start["status"], content, dict(start["headers"])


class APITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = create_app(
            db_path=Path(self.tmp.name) / "db.sqlite",
            registry=AdapterRegistry(Path(self.tmp.name)),
            token="test-key",
        )

    def call(self, path, method="GET", body=None, auth=True, **headers):
        if auth:
            headers["authorization"] = "Bearer test-key"
        return asyncio.run(request(self.app, path, method, body, headers))

    def test_health_public_but_accounts_private(self):
        self.assertEqual(self.call("/health", auth=False)[0], 200)
        self.assertEqual(self.call("/api/accounts", auth=False)[0], 401)

    def test_session_httponly_and_csrf(self):
        self.assertEqual(self.call("/api/session", "POST", auth=False)[0], 403)
        status, _, headers = self.call(
            "/api/session", "POST", auth=False, **{"x-ap-client": "local-ui"}
        )
        self.assertEqual(status, 200)
        self.assertIn(b"HttpOnly", headers[b"set-cookie"])
        self.assertIn(b"SameSite=strict", headers[b"set-cookie"])

    def test_cross_origin_and_rebinding_rejected(self):
        self.assertEqual(self.call("/api/accounts", origin="https://evil.example")[0], 403)
        self.assertEqual(self.call("/api/accounts", host="evil.example")[0], 400)

    def test_unknown_fields_cannot_enable_live(self):
        self.assertEqual(
            self.call(
                "/api/accounts",
                "POST",
                {
                    "platform": "telegram",
                    "label": "test",
                    "backend_label": "t",
                    "mode": "simulation",
                    "token": "hidden",
                },
            )[0],
            422,
        )

    def test_account_settings_can_be_edited_without_changing_identity(self):
        status, account, _ = self.call(
            "/api/accounts",
            "POST",
            {
                "platform": "telegram",
                "label": "Original",
                "backend_label": "fixed",
                "mode": "simulation",
            },
        )
        self.assertEqual(status, 201)
        status, updated, _ = self.call(
            f"/api/accounts/{account['id']}",
            "PATCH",
            {"label": "Edited", "daily_limit": 7, "hourly_limit": 4, "min_interval": 0},
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["label"], "Edited")
        self.assertEqual(updated["backend_label"], "fixed")

    def test_complete_api_flow(self):
        status, a, _ = self.call(
            "/api/accounts",
            "POST",
            {
                "platform": "telegram",
                "label": "simulation",
                "backend_label": "test",
                "mode": "simulation",
                "min_interval": 0,
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(
            self.call(f"/api/accounts/{a['id']}/check", "POST")[1]["status"], "healthy"
        )
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        policy = {
            "starts_at": (now - timedelta(minutes=1)).isoformat(),
            "ends_at": (now + timedelta(hours=1)).isoformat(),
            "min_interval": 0,
        }
        status, t, _ = self.call(
            "/api/tasks",
            "POST",
            {"name": "API integration", "account_ids": [a["id"]], "policy": policy},
        )
        self.assertEqual(status, 201)
        id = t["id"]
        self.assertEqual(
            self.call(f"/api/tasks/{id}/approve/discovery_join", "POST", {"phrase": "好的"})[0], 409
        )
        self.assertEqual(
            self.call(
                f"/api/tasks/{id}/approve/discovery_join", "POST", {"phrase": t["discovery_phrase"]}
            )[0],
            200,
        )
        item = {
            "account_id": a["id"],
            "action": "send_message",
            "target": "@simulation",
            "text": "synthetic only",
            "scheduled_at": now.isoformat(),
        }
        self.assertEqual(self.call(f"/api/tasks/{id}/items", "PUT", [item])[0], 200)
        t = self.call(f"/api/tasks/{id}/prepare", "POST")[1]
        self.assertEqual(
            self.call(
                f"/api/tasks/{id}/approve/outbound", "POST", {"phrase": t["outbound_phrase"]}
            )[0],
            200,
        )
        self.assertEqual(self.call(f"/api/tasks/{id}/run", "POST")[1]["processed"], [])
        self.call("/api/control/stop", "POST", {"enabled": False})
        r = self.call(f"/api/tasks/{id}/run", "POST")[1]
        self.assertEqual(r["processed"][0]["status"], "confirmed")
        self.assertEqual(self.call(f"/api/tasks/{id}/run", "POST")[1]["processed"], [])
        self.assertEqual(len(self.call("/api/services")[1]), 5)
        self.assertGreater(len(self.call("/api/calls")[1]), 0)
        self.assertGreater(len(self.call("/api/audit")[1]), 0)

    def test_error_and_missing_task(self):
        self.assertEqual(self.call("/api/tasks/missing")[0], 404)
        self.assertEqual(self.call("/api/tasks", "POST", {})[0], 422)

    def test_openapi_and_static_shell(self):
        self.assertEqual(self.call("/openapi.json", auth=False)[0], 200)
        self.assertIn("Auto Promotion", self.call("/", auth=False)[1])
        self.assertIn(
            "default-src", self.call("/", auth=False)[2][b"content-security-policy"].decode()
        )

    def test_raw_credentials_absent_from_account_response(self):
        self.call(
            "/api/accounts", "POST", {"platform": "x", "label": "public", "backend_label": "public"}
        )
        output = json.dumps(self.call("/api/accounts")[1])
        for field in ["session_string", "api_hash", "access_token", "password"]:
            self.assertNotIn(field, output)


if __name__ == "__main__":
    unittest.main()
