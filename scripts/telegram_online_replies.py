"""Submit exact, evidence-reviewed replies to the existing online queue.

This client has no Telegram adapter: all platform reads/writes run in Workers.
Preparing content does not enable a pipeline. Start is a separate explicit action.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from auto_promotion.adapters import AdapterRegistry  # noqa: E402
from auto_promotion.cloud_control import CloudControl, CloudUnavailable  # noqa: E402
from auto_promotion.config import load_environment  # noqa: E402
from auto_promotion.core import Engine  # noqa: E402
from auto_promotion.execution_channels import binding  # noqa: E402
from auto_promotion.store import Store  # noqa: E402


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    sub = cli.add_subparsers(dest="operation", required=True)
    for command in ("start", "stop", "status"):
        option = sub.add_parser(command)
        option.add_argument("--plan-id", required=True)
        if command == "start":
            option.add_argument("--task-id", required=True)
    for command in ("prepare", "reconfirm"):
        option = sub.add_parser(command)
        option.add_argument("--file", type=Path, required=True)
    return cli


def main(argv=None):
    args = parser().parse_args(argv)
    load_environment(ROOT / ".env")
    load_environment(ROOT / "var/cloud-node.env")
    db = Path(os.getenv("AP_DB", ROOT / "var/auto-promotion.db"))
    engine = Engine(Store(db, initialize=False), AdapterRegistry(ROOT))
    cloud = CloudControl.configured()
    if cloud is None:
        raise CloudUnavailable("CloudControlNotConfigured")
    if args.operation in {"prepare", "reconfirm"}:
        if args.file.stat().st_size > 100000:
            raise CloudUnavailable("PreparedReplyFileTooLarge")
        body = json.loads(args.file.read_text(encoding="utf-8-sig"))
        if not isinstance(body, dict):
            raise CloudUnavailable("InvalidPreparedReplyFile")
    else:
        body = {"plan_id": args.plan_id}
    if args.operation == "start":
        # Mirror only the explicitly scoped task; do not hand off other accounts.
        task = engine.task(args.task_id)
        if task["state"] != "active" or task["policy"]["execution_channel"] != "cloud":
            raise CloudUnavailable("ExplicitStartedCloudTaskRequired")
        cloud.sync(engine, task_ids=[args.task_id])
        body["reference_task_id"] = args.task_id
    body.update(
        local_store_id=binding(engine),
        source=body.get("source") or "explicit_online_reply_" + args.operation,
    )
    result = cloud.request("/v1/workflow/replies/" + args.operation, body, operator=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    try:
        main()
    except CloudUnavailable as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        raise SystemExit(1) from None
