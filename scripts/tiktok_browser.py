"""TikTok desktop executor entrypoint; the browser UI is operated by Codex.

No command in this script clicks, logs in, starts a task, or changes live switches.
"""

import argparse
import json
import os
from pathlib import Path

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.config import database_path, load_environment, workspace_path
from auto_promotion.core import Engine
from auto_promotion.store import Store
from auto_promotion.tiktok_browser import TikTokBrowser, TikTokBrowserConnect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "status",
            "connect",
            "jobs",
            "observe",
            "begin",
            "guard",
            "result",
            "feedback",
            "dm-collect",
            "dm-inbox",
            "dm-messages",
            "dm-enable",
        ],
    )
    parser.add_argument("--db")
    parser.add_argument("--input", help="UTF-8 JSON containing method arguments")
    args = parser.parse_args()
    load_environment(".env")
    engine = Engine(
        Store(args.db or database_path()),
        AdapterRegistry(workspace_path()),
        live_enabled=os.getenv("AUTO_PROMOTION_LIVE_WRITES") == "1",
    )
    manager = TikTokBrowser(engine)
    data = json.loads(Path(args.input).read_text(encoding="utf-8-sig")) if args.input else {}
    if args.action == "connect":
        result = manager.connect(TikTokBrowserConnect.model_validate(data))
    elif args.action in {"status", "jobs"}:
        result = (manager.status if args.action == "status" else manager.pending)()
    elif args.action.startswith("dm-"):
        action = {
            "dm-collect": "collect",
            "dm-inbox": "inbox",
            "dm-messages": "messages",
            "dm-enable": "enable",
        }[args.action]
        result = getattr(manager.messages, action)(**data)
    else:
        result = getattr(manager, args.action)(**data)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
