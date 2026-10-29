import importlib.util
import sys
import unittest
from pathlib import Path

root = Path("var/private/tiktok-dm-latest10-20261027").resolve()
control = None
if (root / "local-batch-task-api.py").is_file():
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location(
        "mira_batch_control", root / "local-batch-task-api.py"
    )
    control = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(control)


@unittest.skipUnless(
    control is not None, "Optional private operations fixture is not in this checkout"
)
class MiraLocalControlHealthTests(unittest.TestCase):
    def test_sidecar_requires_current_source_and_existing_live_worker(self):
        sidecar = {
            "status": "ok",
            "worker": False,
            "report_worker": False,
            "statistics_only": False,
            "live_writes": True,
            "execution_mode": "pooled",
            "runtime": {"source_fingerprint": "current"},
        }
        main = {"status": "ok", "worker": True, "live_writes": True, "execution_mode": "pooled"}
        self.assertTrue(control.control_health_valid(sidecar, main, "current", 18780))
        for changed in (
            {"worker": True},
            {"report_worker": True},
            {"live_writes": False},
            {"statistics_only": True},
            {"runtime": {"source_fingerprint": "stale"}},
        ):
            with self.subTest(sidecar=changed):
                self.assertFalse(
                    control.control_health_valid({**sidecar, **changed}, main, "current", 18780)
                )
        for changed in (
            {"worker": False},
            {"status": "degraded"},
            {"live_writes": False},
            {"execution_mode": "legacy"},
        ):
            with self.subTest(main=changed):
                self.assertFalse(
                    control.control_health_valid(sidecar, {**main, **changed}, "current", 18780)
                )
        self.assertFalse(control.control_health_valid(sidecar, main, "current", 9999))

    def test_primary_service_keeps_its_original_fingerprint_requirement(self):
        main = {
            "status": "ok",
            "worker": True,
            "live_writes": True,
            "execution_mode": "pooled",
            "runtime": {"source_fingerprint": "current"},
        }
        self.assertTrue(control.control_health_valid(main, main, "current", 8780))
        self.assertFalse(control.control_health_valid(main, main, "newer", 8780))


if __name__ == "__main__":
    unittest.main()
