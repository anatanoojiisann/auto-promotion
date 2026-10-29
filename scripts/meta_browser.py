"""Local entry point for the existing Codex desktop browser executor."""

import argparse
import json
import os
from pathlib import Path

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.config import database_path, load_environment, workspace_path
from auto_promotion.core import Engine
from auto_promotion.meta_browser import MetaBrowser
from auto_promotion.meta_migration import import_existing
from auto_promotion.store import Store


def main():
    load_environment(".env")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=[
            "import-existing",
            "status",
            "jobs",
            "observe",
            "begin",
            "guard",
            "result",
            "feedback",
            "requeue-unsubmitted",
            "skip-unknown",
        ],
    )
    parser.add_argument("--db")
    parser.add_argument("--input", help="UTF-8 JSON file containing method arguments")
    args = parser.parse_args()
    engine = Engine(
        Store(args.db or database_path()),
        AdapterRegistry(workspace_path()),
        live_enabled=os.getenv("AUTO_PROMOTION_LIVE_WRITES") == "1",
    )
    browser = MetaBrowser(engine)
    data = json.loads(Path(args.input).read_text(encoding="utf-8-sig")) if args.input else {}
    if args.action == "import-existing":
        result = import_existing(browser)
    elif args.action == "status":
        result = browser.status()
    elif args.action == "jobs":
        result = browser.pending()
    elif args.action == "requeue-unsubmitted":
        from auto_promotion.meta_requeue import requeue_unsubmitted

        result = requeue_unsubmitted(browser, **data)
    elif args.action == "skip-unknown":
        from auto_promotion.meta_skip import skip_unknown

        result = skip_unknown(browser, **data)
    else:
        result = getattr(browser, args.action)(**data)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
