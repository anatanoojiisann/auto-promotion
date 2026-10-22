"""Check or record a two-hour manual-review fallback; never publish."""

import argparse
import json
import time

from auto_promotion.config import database_path
from auto_promotion.interaction_observer import fallback_status, record_fallback
from auto_promotion.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("status", "record"))
    parser.add_argument("--db", default=str(database_path()))
    parser.add_argument("--policy-id", required=True)
    parser.add_argument("--result", choices=("prepared", "no_match"))
    parser.add_argument("--reason")
    parser.add_argument("--candidate-id")
    parser.add_argument("--priority", choices=("P2", "P3"))
    parser.add_argument("--task-id")
    parser.add_argument("--source-url")
    args = parser.parse_args()
    store = Store(args.db)
    if args.action == "record":
        if not args.result or not args.reason:
            parser.error("record requires --result and --reason")
        value = record_fallback(
            store,
            args.policy_id,
            result=args.result,
            reason=args.reason,
            candidate_id=args.candidate_id,
            priority=args.priority,
            task_id=args.task_id,
            source_url=args.source_url,
            now=time.time(),
        )
    else:
        value = fallback_status(store, args.policy_id, now=time.time())
    print(json.dumps(value, ensure_ascii=False))


if __name__ == "__main__":
    main()
