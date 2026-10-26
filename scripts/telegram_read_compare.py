"""Compare pinned baseline/current Python Telegram reads with one isolated synthetic client.

No production profile or platform connection is used. Counts are instrumented
logical _call attempts in the fake, never observed wire calls or SDK retries.
"""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from test_telegram_incremental_reads import SyntheticTelegram, messages  # noqa: E402

from auto_promotion.backends.common import save_profile  # noqa: E402
from auto_promotion.backends.telegram import TelegramBackend  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline-file", type=Path, help="Exact original source, verified against pinned blob"
    )
    args = parser.parse_args()
    blob = "c1f699b100954c0e1c9025c5baf4cd65427e7782"
    if args.baseline_file:
        source = args.baseline_file.read_bytes()
    else:
        result = subprocess.run(["git", "show", blob], cwd=ROOT, capture_output=True)
        if result.returncode:
            parser.error(
                "Pinned local baseline object unavailable; supply --baseline-file with that exact pre-optimization source"
            )
        source = result.stdout
    actual = hashlib.sha1(b"blob " + str(len(source)).encode() + b"\0" + source).hexdigest()
    if actual != blob:
        parser.error("Baseline does not match pinned source blob")
    with (
        tempfile.TemporaryDirectory() as directory,
        patch.dict(os.environ, {"AP_ACCOUNTS_DIR": directory}),
    ):
        baseline = Path(directory) / "baseline.py"
        baseline.write_bytes(source)
        spec = importlib.util.spec_from_file_location(
            "auto_promotion.backends._read_baseline", baseline
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        save_profile("telegram", "test", {"identity": "42", "api_id": 1, "api_hash": "synthetic"})
        evidence = {}
        for label, backend_type in (
            ("baseline", module.TelegramBackend),
            ("current", TelegramBackend),
        ):
            client = SyntheticTelegram(messages(5))
            results, counts = [], []
            for _ in range(2):
                before = len(client.calls)
                backend = backend_type("test", client_factory=lambda *args: client)
                result = asyncio.run(
                    backend.run(
                        "discover",
                        {
                            "policy": {"queries": ["video", "AI"], "max_candidates": 10},
                            "read_connection_revision": "comparison",
                        },
                        lambda result: None,
                        lambda: None,
                    )
                )
                results.append(len(result["candidates"]))
                counts.append(len(client.calls) - before)
            evidence[label] = {
                "logical_call_attempts": counts,
                "total": sum(counts),
                "candidate_counts": results,
            }
        print(
            json.dumps(
                {
                    "baseline_source_blob": blob,
                    "fixture": "two discover calls, two queries, same public group, five messages",
                    "count_scope": "synthetic_telethon_logical_call_attempt",
                    "wire_attempts": None,
                    "sdk_internal_retries": None,
                    **evidence,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
