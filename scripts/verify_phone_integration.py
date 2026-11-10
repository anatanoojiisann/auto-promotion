"""Build paired wheels and run offline integration on Python 3.12; no live task execution."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from uuid import uuid4

PACKAGES = {
    "phone-platform-contracts": "packages/phone-platform-contracts",
    "phone-fleet-protocol": "packages/phone-fleet-protocol",
    "phone-fleet-management": "control-system/fleet-management",
}
AP_TESTS = (
    "tests/test_publication_receipt_integration.py",
    "tests/test_content_production.py",
    "tests/test_content_phone_pipeline.py",
    "tests/test_content_origin_links.py",
    "tests/test_automatic_content_phone.py",
    "tests/test_content_queue.py",
    "tests/test_hf_content_queue.py",
    "tests/test_campaign_api.py",
    "tests/test_phone_social.py",
    "tests/test_phone_receipt_readback.py",
    "tests/test_phone_protocol_receipt_gateway.py",
    "tests/test_telegram_phone_observer.py",
    "tests/test_phone_reddit.py",
    "tests/test_mcp.py",
    "tests/test_phone_protocol_upgrade.py",
    "tests/test_phone_platform_independence.py",
    "tests/test_phone_known_unsent.py",
    "tests/test_engine.py",
    "tests/test_execution_channels.py",
    "tests/test_task_channels.py",
    "tests/test_interaction_readiness.py",
    "tests/test_persistent_pools.py",
    "tests/test_background_desktop.py",
    "tests/test_cloud_transport_diagnostics.py",
    "tests/test_cloud_workflow.py",
    "tests/test_phone_runtime_identity.py",
    "tests/test_outbound_text_integrity.py",
)
LEGACY_MODULES = ("test_campaigns", "test_interactions", "test_interaction_joint")
LEGACY_BOUNDARIES = (
    "tests/test_campaigns.py::CampaignTests::test_generated_drafts_remain_local_until_exact_approvals",
    "tests/test_campaigns.py::CampaignTests::test_campaign_pause_blocks_child_start",
    "tests/test_interactions.py::InteractionTests::test_T01_default_ten_minute_schedule_is_independent_of_generation",
    "tests/test_interactions.py::InteractionTests::test_T03_late_indexed_event_is_found_in_overlap_once",
    "tests/test_interactions.py::InteractionTests::test_T27_accepted_but_timeout_enters_verifying_no_resend",
    "tests/test_interactions.py::InteractionTests::test_T28_crash_after_claim_recovers_as_unknown_without_second_attempt",
    "tests/test_interaction_joint.py::JointTests::test_S14_S15_S22_duplicate_discoveries_one_task_one_primary",
    "tests/test_interaction_joint.py::JointTests::test_S25_S26_unknown_survives_expiry_pause_restart_without_resend",
)


def isolated_environment(work):
    prefixes = (
        "AP_",
        "AUTO_PROMOTION_",
        "PHONE_",
        "FLEET_",
        "TELEGRAM_",
        "REDDIT_",
        "INSTAGRAM_",
        "FACEBOOK_",
        "TIKTOK_",
        "TEMPORAL_",
        "OPENAI_",
        "ANTHROPIC_",
        "AWS_",
        "AZURE_",
        "GOOGLE_",
        "X_",
        "GEMINI_",
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.upper().startswith(prefixes)
        and k.upper() not in {"PYTHONPATH", "PYTHONHOME"}
        and not any(word in k.upper() for word in ("TOKEN", "SECRET", "API_KEY", "PASSWORD"))
    }
    home = work / "home"
    home.mkdir()
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        APPDATA=str(home / "roaming"),
        LOCALAPPDATA=str(home / "local"),
        AP_DB=str(home / "unused.db"),
        AP_ACCOUNTS_DIR=str(home / "accounts"),
        AUTO_PROMOTION_WORKSPACE=str(home),
        AUTO_PROMOTION_LIVE_WRITES="0",
        PYTHONDONTWRITEBYTECODE="1",
        PIP_DISABLE_PIP_VERSION_CHECK="1",
        PIP_NO_INPUT="1",
    )
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phone-source", type=Path, required=True)
    parser.add_argument(
        "--phone-commit", help="Exact phone revision supplied by the private repository CI"
    )
    parser.add_argument("--work-root", type=Path, default=Path(".local/phone-integration"))
    parser.add_argument(
        "--full-legacy", action="store_true", help="Also run all legacy campaign and HF tests"
    )
    parser.add_argument(
        "--all-tests", action="store_true", help="Run the complete offline AP pytest suite"
    )
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        parser.error("Use Python 3.12 for the paired phone packages")
    root = Path(__file__).resolve().parents[1]
    phone = args.phone_source.resolve(strict=True)
    pair = json.loads((root / "docs/integrations/phone-pair.json").read_text())
    if args.phone_commit:
        if not re.fullmatch(r"[a-f0-9]{40}", args.phone_commit):
            parser.error("An exact phone commit SHA is required")
        pair = {**pair, "commit": args.phone_commit}
    revision = subprocess.check_output(
        ["git", "-C", str(phone), "rev-parse", "HEAD"], text=True
    ).strip()
    dirty = subprocess.check_output(
        ["git", "-C", str(phone), "status", "--porcelain"], text=True
    ).strip()
    if revision != pair["commit"] or dirty:
        parser.error("Phone source must be the clean, exact commit in phone-pair.json")
    work = args.work_root.resolve() / uuid4().hex[:10]
    work.mkdir(parents=True)
    environment = isolated_environment(work)

    def run(*command, cwd=work, capture=False):
        return subprocess.run(
            [str(v) for v in command],
            cwd=cwd,
            env=environment,
            check=True,
            text=True,
            capture_output=capture,
        )

    run(sys.executable, "-m", "venv", work / "venv")
    python = work / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    # Reuse the phone service's tested dependency constraints; local wheels never go to PyPI.
    versions = {
        name: tomllib.loads((phone / path / "pyproject.toml").read_text())["project"]["version"]
        for name, path in PACKAGES.items()
    }
    if versions != pair["packages"]:
        raise RuntimeError("Paired package versions differ")
    lines = []
    for line in (phone / "control-system/requirements-integration.lock").read_text().splitlines():
        match = re.match(r"^([\w-]+)==", line)
        if match and match[1].lower().replace("_", "-").startswith(("phone-", "auto-promotion")):
            continue
        lines.append(line)
    constraint = work / "phone-constraints.txt"
    constraint.write_text("\n".join(lines) + "\n")
    wheels = work / "wheels"
    wheels.mkdir()
    for source in [*(phone / path for path in PACKAGES.values()), root]:
        run(python, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", wheels, source)
    built = sorted(wheels.glob("*.whl"))
    run(
        python,
        "-m",
        "pip",
        "install",
        "-c",
        constraint,
        "-c",
        root / "requirements-phone-test.txt",
        *built,
        "auto-promotion[dev,mcp,telegram,temporal,browser,phone-test]",
    )
    run(python, "-m", "pip", "check")
    run(
        python,
        "-I",
        "-c",
        "import auto_promotion, phone_fleet_protocol, pathlib, sys; "
        "assert all(pathlib.Path(m.__file__).is_relative_to(sys.prefix) "
        "for m in (auto_promotion, phone_fleet_protocol))",
    )
    report = {
        "mode": "offline_synthetic_only",
        "hardware_acceptance": "not_run",
        "pair": pair,
        "python": sys.version,
        "work": str(work),
        "wheels": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in built},
        "dependencies": json.loads(
            run(python, "-m", "pip", "list", "--format=json", capture=True).stdout
        ),
        "state": "running",
    }
    manifest = work / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2))
    ap_tests = (
        ("tests",)
        if args.all_tests
        else (
            *AP_TESTS,
            *(
                [f"tests/{name}.py" for name in LEGACY_MODULES]
                if args.full_legacy
                else LEGACY_BOUNDARIES
            ),
        )
    )
    report["full_legacy"] = args.full_legacy
    report["all_tests"] = args.all_tests
    suites = (
        (phone / "packages/phone-fleet-protocol", ["tests"], "protocol"),
        (
            phone / "control-system/fleet-management",
            [
                "tests/test_publication_receipt_integration.py",
                "tests/test_receipt_resource_release.py",
            ],
            "fleet",
        ),
        (root, ap_tests, "promotion"),
    )
    try:
        for cwd, tests, name in suites:
            run(
                python,
                "-m",
                "pytest",
                *tests,
                "-q",
                "--tb=short",
                "--junitxml=" + str(work / (name + ".xml")),
                cwd=cwd,
            )
    except subprocess.CalledProcessError:
        report["state"] = "failed"
        raise
    else:
        report["state"] = "passed"
    finally:
        manifest.write_text(json.dumps(report, indent=2))
        print("Integration manifest:", manifest)


if __name__ == "__main__":
    main()
