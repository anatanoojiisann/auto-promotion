"""Persistent completion/priority gate. This script never reads credentials or sends."""

import argparse
import json
import sys
from pathlib import Path

from auto_promotion.completion_cycles import CompletionCycles
from auto_promotion.completion_sources import refresh_sources
from auto_promotion.config import database_path
from auto_promotion.store import Store


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(database_path()))
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "scan-result", "authorize", "result", "observe"):
        p = sub.add_parser(name)
        p.add_argument("--file", required=True)
    for name in ("status", "refresh"):
        sub.add_parser(name)
    p = sub.add_parser("claim")
    p.add_argument("--account", required=True)
    p.add_argument("--owner", required=True)
    p = sub.add_parser("begin")
    p.add_argument("--candidate", required=True)
    p.add_argument("--hash", required=True)
    p.add_argument("--task")
    p = sub.add_parser("notice-delivered")
    p.add_argument("--round", required=True)
    args = parser.parse_args()
    cycles = CompletionCycles(Store(args.db))
    data = (
        json.loads(Path(args.file).read_text(encoding="utf-8-sig")) if hasattr(args, "file") else {}
    )
    if args.command == "init":
        cycles.configure(
            enabled=data["enabled"],
            source=data["source"],
            wait_seconds=data.get("wait_seconds", 7200),
        )
        for account in data.get("external_accounts", []):
            cycles.enroll(**account)
        result = refresh_sources(cycles)
    elif args.command == "refresh":
        result = refresh_sources(cycles)
    elif args.command == "claim":
        refresh_sources(cycles)
        result = {"round_id": cycles.claim_scan(args.account, args.owner)}
    elif args.command == "scan-result":
        cycles.finish_scan(**data)
        result = cycles.status()
    elif args.command == "authorize":
        cycles.authorize(**data)
        result = cycles.status()
    elif args.command == "begin":
        refresh_sources(cycles)
        cycles.begin(args.candidate, args.hash, task_id=args.task)
        result = {"reserved": args.candidate}
    elif args.command == "result":
        cycles.result(**data)
        result = cycles.status()
    elif args.command == "observe":
        cycles.observe(**data)
        result = cycles.status()
    elif args.command == "notice-delivered":
        cycles.acknowledge_notice(args.round)
        result = {"notice_delivered": args.round}
    else:
        result = cycles.status()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
