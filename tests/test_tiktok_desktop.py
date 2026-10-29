"""Real routing/bridge contracts; CLI and all platform responses are synthetic."""

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_tiktok as fixtures

from auto_promotion import native_bridge
from auto_promotion.backends.common import BackendError, save_profile
from auto_promotion.backends.tiktok_desktop import call, validate_item
from auto_promotion.models import digest
from auto_promotion.routing import route_for


class DesktopBridgeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TikTokTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.settings = {
            "identity": "owned-account",
            "username": "brand",
            "browser_channel": "tabbit",
            "tabbit_instance": "A" * 16,
            "instance_id": "A" * 16,
            "connection_type": "tiktok_tabbit_candidate",
            "experimental_tabbit_enabled": True,
            "tabbit_tab_id": 123,
        }
        save_profile("tiktok", "brand", self.settings)
        self.item = {
            "action": "comment",
            "target": "https://www.tiktok.com/@target/video/123",
            "text": "exact text",
            "tiktok_video_id": "123",
            "tiktok_comment_id": "",
            "media": [],
        }
        self.payload = {
            "task_id": "task",
            "item_id": "item",
            "attempt_id": "attempt",
            "payload": self.item,
        }
        self.phases = []
        self.mode = "normal"
        self.cli_patch = patch("auto_promotion.backends.tabbit.cli", side_effect=self.cli)
        self.cli_patch.start()
        self.addCleanup(self.cli_patch.stop)

    def cli(self, arguments, instance, *, code=None, **_kw):
        self.assertEqual(instance, "A" * 16)
        if arguments[0] == "create":
            return {
                "tabs": [
                    {"tabId": 123, "state": "available", "url": "https://www.tiktok.com/@brand"}
                ]
            }
        if arguments[0] == "finish":
            return {"ok": True}
        self.assertEqual(arguments[0], "nodejs")
        phase = json.loads(re.search(r"^const phase=(.*);$", code, re.M)[1])
        item = json.loads(re.search(r"^const item=(.*);$", code, re.M)[1])
        self.phases.append(phase)
        if self.mode == "nav403" and phase in {"prepare", "preflight"}:
            return {
                "status": "failed",
                "result": {
                    "issuesDelta": [
                        {
                            "code": "SITE_ACCESS_BLOCKED",
                            "operation": "navigation",
                            "origin": "https://www.tiktok.com",
                            "details": {"status": 403},
                        }
                    ]
                },
            }
        if phase in {"prepare", "preflight"}:
            value = {
                "prepared": True,
                "video_id": item["tiktok_video_id"],
                "parent_comment_id": item.get("tiktok_comment_id", ""),
                "text_hash": digest(item["text"]),
            }
        elif phase == "submit":
            value = {
                "status": "unknown",
                "verified": False,
                "submission_started": True,
                "comment_id": "789",
            }
        elif phase == "verify":
            if self.mode == "timeout":
                raise BackendError("TabbitConnectionUnavailable")
            value = {
                "status": "confirmed",
                "verified": True,
                "comment_id": "789",
                "external_reference": "https://www.tiktok.com/@target/video/123?comment_id=789",
            }
        else:
            value = {
                "healthy": True,
                "identity_verified": True,
                "account_identity": "owned-account",
                "permissions": ["comment", "reply"],
            }
        return {"status": "succeeded", "result": {"value": value}}

    def test_actual_native_route_submits_once_then_reads_independent_receipt(self):
        request = {
            "platform": "tiktok",
            "account": "brand",
            "operation": "execute",
            "payload": self.payload,
        }
        with patch("auto_promotion.native_bridge.verify_claim") as guard:
            result = native_bridge.dispatch(request)
            second = native_bridge.dispatch(request)
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["comment_id"], "789")
        self.assertIn("tiktok_receipt_binding", result)
        self.assertEqual(second["comment_id"], "789")
        self.assertEqual(self.phases, ["prepare", "submit", "verify"])
        self.assertEqual(guard.call_count, 3)  # initial, final authorize, repeated initial

    def test_final_stop_prevents_submit(self):
        authorize = Mock(side_effect=BackendError("Stopped"))
        result = call(self.settings, "execute", self.payload, Mock(), authorize)
        self.assertEqual(result["status"], "failed_known")
        self.assertEqual(self.phases, ["prepare"])
        authorize.assert_called_once()

    def test_submit_receipt_survives_readback_timeout_and_cannot_replay(self):
        self.mode = "timeout"
        request = {
            "platform": "tiktok",
            "account": "brand",
            "operation": "execute",
            "payload": self.payload,
        }
        with patch("auto_promotion.native_bridge.verify_claim"):
            result = native_bridge.dispatch(request)
            second = native_bridge.dispatch(request)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["comment_id"], "789")
        self.assertEqual(second["comment_id"], "789")
        self.assertEqual(self.phases.count("submit"), 1)

    def test_navigation403_failure_is_not_missing_executor_or_rate_limit(self):
        self.mode = "nav403"
        result = call(self.settings, "preflight", self.payload, Mock(), Mock())
        self.assertEqual(result["reason"], "TikTokAccessDenied")
        self.assertEqual(result["failure_class"], "access_denied")
        self.assertFalse(result.get("rate_limited", False))
        self.assertEqual(self.phases, ["preflight"])

    def test_route_and_task_input_use_desktop_contract_without_owned_api_gate(self):
        account = {
            "id": "synthetic",
            "identity_hash": "bound",
            "platform": "tiktok",
            "mode": "live",
            "backend_label": "brand",
        }
        self.assertEqual(route_for(account).pool, "tiktok.tabbit")
        validate_item(self.item)
        for changes in [
            {"target": "https://www.tiktok.com/@target/video/999"},
            {"action": "reply"},
            {
                "action": "reply",
                "tiktok_comment_id": "456",
                "target": self.item["target"] + "?comment_id=999",
            },
        ]:
            with self.subTest(changes=changes), self.assertRaises(BackendError):
                validate_item({**self.item, **changes})

    def test_receipt_from_other_task_never_triggers_readback(self):
        saved = []
        result = call(self.settings, "execute", self.payload, saved.append, lambda: None)
        before = list(self.phases)
        other = {**self.payload, "task_id": "another", "receipt": result}
        result = call(self.settings, "verify", other, Mock(), Mock())
        self.assertFalse(result["verified"])
        self.assertEqual(result["reason"], "TikTokReceiptBindingMismatch")
        self.assertEqual(self.phases, before)

    def test_shared_tabbit_resource_is_consistent_across_platforms(self):
        base = {
            "id": "synthetic",
            "identity_hash": "bound",
            "mode": "live",
            "backend_label": "brand",
        }
        profile = dict(self.settings)
        x = route_for({**base, "platform": "x"}, profile=profile)
        tiktok = route_for({**base, "platform": "tiktok"}, profile=profile)
        self.assertEqual(x.resource, tiktok.resource)
        other = route_for(
            {**base, "platform": "tiktok"},
            profile={**profile, "tabbit_instance": "B" * 16, "instance_id": "B" * 16},
        )
        self.assertNotEqual(other.resource, x.resource)
        self.assertNotEqual(
            route_for({**base, "platform": "tiktok"}, profile={"browser_channel": "tabbit"}).pool,
            "tiktok.tabbit",
        )

    def test_candidate_is_disabled_without_explicit_enrollment(self):
        for changes in (
            {"experimental_tabbit_enabled": False},
            {"connection_type": "tiktok_desktop"},
            {"connection_type": ""},
            {"instance_id": "B" * 16},
            {"instance_id": 123, "tabbit_instance": 123},
        ):
            with self.subTest(changes=changes):
                result = call({**self.settings, **changes}, "execute", self.payload, Mock(), Mock())
                self.assertEqual(result["reason"], "TikTokTabbitCandidateDisabled")
        self.assertEqual(self.phases, [])

    def test_registered_desktop_route_takes_priority_over_candidate(self):
        account = {"id": "one", "platform": "tiktok", "mode": "live", "backend_label": "brand"}
        profile = {**self.settings, "connection_type": "tiktok_desktop"}
        route = route_for(account, profile=profile)
        self.assertEqual(route.pool, "tiktok.desktop")
        self.assertEqual(route.resource, "desktop:tabbit:" + digest("A" * 16)[:24])

    def test_disabling_candidate_preserves_unresolved_receipt_without_reading(self):
        prior = {
            "status": "unknown",
            "comment_id": "789",
            "tiktok_receipt_binding": "saved-binding",
        }
        result = call(
            {**self.settings, "experimental_tabbit_enabled": False},
            "verify",
            {**self.payload, "receipt": prior},
            Mock(),
            Mock(),
        )
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["comment_id"], "789")
        self.assertEqual(result["tiktok_receipt_binding"], "saved-binding")
        self.assertFalse(result["verified"])
        self.assertEqual(self.phases, [])

    def test_existing_x_resource_binding_is_not_rewritten(self):
        account = {"id": "one", "platform": "x", "mode": "live", "backend_label": "brand"}
        profile = {"browser_channel": "tabbit", "tabbit_instance": "A" * 16}
        self.assertEqual(
            route_for(account, profile=profile).resource, "desktop:tabbit:" + digest("default")[:24]
        )

    def test_403_cooldown_persists_and_prevents_additional_reads(self):
        from auto_promotion.core import Engine
        from auto_promotion.models import AccountInput

        engine = self.fixture.engine
        account = engine.add_account(
            AccountInput(
                platform="tiktok",
                label="cooldown",
                backend_label="cooldown",
                mode="simulation",
                min_interval=0,
            )
        )
        denied = {
            "reason": "TikTokAccessDenied",
            "healthy": False,
            "failure_class": "access_denied",
            "retry_after_seconds": 120,
            "rate_limited": False,
        }
        with patch.object(engine, "_call_unlocked", return_value=denied) as actual:
            first = engine.call(account, "targets")
            self.assertFalse(first["rate_limited"])
            self.assertEqual(actual.call_count, 1)
        restarted = Engine(engine.store, engine.adapters, clock=engine.clock)
        with patch.object(restarted, "_call_unlocked", return_value={"targets": []}) as actual:
            result = restarted.call(account, "targets")
            self.assertEqual(result["reason"], "TikTokAccountCooldown")
            actual.assert_not_called()
            restarted.clock = lambda: engine.clock() + 121
            restarted.call(account, "targets")
            actual.assert_called_once()


@unittest.skipUnless(os.getenv("AP_TEST_BROWSER_BACKENDS") == "1", "offline browser opt-in")
class DesktopDOMTests(unittest.TestCase):
    def test_production_program_on_intercepted_chromium(self):
        result = subprocess.run(
            ["node", "scripts/test_tiktok_desktop.cjs"],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "AP_PYTHON": sys.executable},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
