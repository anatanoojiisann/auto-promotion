"""Capture safe startup evidence once, never infer running code from a later HEAD."""

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

FINGERPRINT_FILES = (
    "account_blocks.py",
    "read_cooldowns.py",
    "interaction_intents.py",
    "queue_store.py",
    "task_channels.py",
    "cloud_control.py",
    "store.py",
    "api.py",
    "pool_runtime.py",
    "native_bridge.py",
    "service_host.py",
    "core.py",
    "health_checks.py",
    "backends/common.py",
    "backends/telegram.py",
    "backends/telegram_reads.py",
    "models.py",
    "telegram_reading.py",
    "adapters.py",
    "execution_diagnostics.py",
    "target_checks.py",
    "campaigns.py",
    "growth.py",
    "statistics.py",
    "backends/browser.py",
    "backends/tabbit.py",
    "backends/browser_use.py",
    "interactions.py",
    "readiness.py",
)


def capture_runtime(component):
    root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    for name in FINGERPRINT_FILES:
        path = Path(__file__).parent / name
        digest.update(name.encode())
        digest.update(path.read_bytes())
    commit = None
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode == 0 and len(result.stdout.strip()) == 40:
            commit = result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {
        "component": component,
        "pid": os.getpid(),
        "started_at": time.time(),
        "python": sys.version.split()[0],
        "git_commit_at_start": commit,
        "source_fingerprint": digest.hexdigest(),
    }


def result_runtime(component):
    observed = capture_runtime(component)
    return {
        "runtime_git_commit_at_start": observed["git_commit_at_start"],
        "runtime_source_fingerprint": observed["source_fingerprint"],
        "runtime_started_at": observed["started_at"],
    }
