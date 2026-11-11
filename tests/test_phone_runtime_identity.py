import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from auto_promotion import runtime_identity

PHONE_RUNTIME_FILES = {
    "backends/phone.py",
    "phone_fleet.py",
    "phone_fleet_demo.py",
    "phone_lab.py",
    "phone_protocol.py",
    "phone_private.py",
    "phone_readiness.py",
    "phone_reddit.py",
    "phone_reddit_api.py",
    "phone_round_readiness.py",
    "phone_rounds.py",
    "phone_rounds_api.py",
    "phone_social.py",
    "phone_receipt_recovery.py",
    "phone_receipt_diagnostics.py",
    "phone_local_gate.py",
    "workflow_health.py",
    "runtime_release.py",
    "temporal_runtime.py",
    "temporal_tasks.py",
    "temporal_task_workflow.py",
    "phone_visual_preparation.py",
    "task_tools.py",
    "phone_social_api.py",
    "phone_software_check.py",
    "phone_telegram.py",
    "telegram_phone_observer.py",
    "telegram_receipt_budget.py",
}


class PhoneRuntimeIdentityTests(unittest.TestCase):
    def test_each_phone_source_change_changes_startup_fingerprint(self):
        self.assertLessEqual(PHONE_RUNTIME_FILES, set(runtime_identity.FINGERPRINT_FILES))
        with tempfile.TemporaryDirectory() as folder:
            package = Path(folder) / "src" / "auto_promotion"
            for name in runtime_identity.FINGERPRINT_FILES:
                path = package / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((name + " original").encode())
            with (
                patch.object(runtime_identity, "__file__", str(package / "runtime_identity.py")),
                patch.object(
                    runtime_identity.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=1, stdout=""),
                ),
            ):
                before = runtime_identity.capture_runtime("synthetic")["source_fingerprint"]
                for name in sorted(PHONE_RUNTIME_FILES):
                    with self.subTest(file=name):
                        path = package / name
                        original = path.read_bytes()
                        path.write_bytes(original + b" changed")
                        changed = runtime_identity.capture_runtime("synthetic")
                        self.assertNotEqual(changed["source_fingerprint"], before)
                        path.write_bytes(original)
                        self.assertEqual(
                            runtime_identity.capture_runtime("synthetic")["source_fingerprint"],
                            before,
                        )

    def test_missing_phone_runtime_file_does_not_return_partial_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            package = Path(folder) / "src" / "auto_promotion"
            for name in runtime_identity.FINGERPRINT_FILES:
                path = package / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"synthetic")
            (package / "phone_rounds.py").unlink(missing_ok=True)
            with (
                patch.object(runtime_identity, "__file__", str(package / "runtime_identity.py")),
                self.assertRaises(FileNotFoundError),
            ):
                runtime_identity.capture_runtime("synthetic")
