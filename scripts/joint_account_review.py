"""Inspect or record one human-prepared reply per account and scan round."""

import argparse
import json

from auto_promotion.config import database_path
from auto_promotion.interaction_observer import account_review_status, record_account_review
from auto_promotion.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("status", "record"))
    parser.add_argument("--db", default=str(database_path()))
    parser.add_argument("--policy-id", required=True)
    parser.add_argument("--result", choices=("prepared", "no_match"))
    parser.add_argument("--reason")
    parser.add_argument("--priority", choices=("P0", "P1", "P2"))
    parser.add_argument("--task-id")
    parser.add_argument("--source-url")
    parser.add_argument("--candidate-id")
    args = parser.parse_args()
    store = Store(args.db)
    if args.action == "record":
        if not args.result or not args.reason:
            parser.error("record requires --result and --reason")
        result = record_account_review(
            store,
            args.policy_id,
            result=args.result,
            reason=args.reason,
            priority=args.priority,
            task_id=args.task_id,
            source_url=args.source_url,
            candidate_id=args.candidate_id,
        )
    else:
        result = account_review_status(store, args.policy_id)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
