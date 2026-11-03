"""Read one existing X publication, with no sender or outbound operation."""

import argparse
import json

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.config import database_path, workspace_path
from auto_promotion.core import Engine
from auto_promotion.engagement import Engagement
from auto_promotion.store import Store

parser = argparse.ArgumentParser()
parser.add_argument("--account-id", required=True)
args = parser.parse_args()
store = Store(database_path())
engine = Engine(store, AdapterRegistry(workspace_path()), live_enabled=False)
engagement = Engagement(engine)
row = next(r for r in engagement.publications() if r["account_id"] == args.account_id)
account = engine.account(row["account_id"])
result = engine.call(
    account,
    "engagement",
    {
        "item_id": row["item_id"],
        "payload": row["payload"],
        "result": row["result"],
        "external_reference": row["external_reference"],
        "read_only": True,
    },
    row["task_id"],
)
saved = engagement.save(row, result)
print(
    json.dumps(
        {"read_only": True, "external_reference": row["external_reference"], **saved},
        ensure_ascii=False,
    )
)
