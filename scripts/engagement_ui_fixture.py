"""Synthetic effects fixture and worker-free UI server; never opens a platform."""

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from auto_promotion.models import Policy, canonical
from auto_promotion.statistics import record_publication
from auto_promotion.store import Store


def seed(path):
    store = Store(path)
    now = time.time()
    policy = Policy(
        starts_at=datetime.fromtimestamp(now - 14 * 86400, UTC).isoformat(),
        ends_at=datetime.fromtimestamp(now + 86400, UTC).isoformat(),
    ).model_dump(mode="json")
    with store.connect(write=True) as db:
        for key, platform, label in (
            ("fx-x", "x", "示例 X 账号"),
            ("fx-tg", "telegram", "示例 Telegram"),
            ("fx-ig", "instagram", "示例 Instagram"),
        ):
            db.execute(
                "INSERT INTO accounts(id,platform,label,backend_label,mode,daily_limit,hourly_limit,min_interval) VALUES(?,?,?,?,?,5,3,0)",
                (key, platform, label, key, "live"),
            )
        for key in ("fx-task-a", "fx-task-b"):
            db.execute(
                "INSERT INTO tasks(id,name,mode,account_ids,account_snapshot,policy,policy_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    "内容测试 " + key[-1].upper(),
                    "live",
                    "[]",
                    "[]",
                    canonical(policy),
                    "fixture",
                    now,
                    now,
                ),
            )
        for index in range(60):
            key = f"fx-item-{index:03d}"
            account = "fx-x" if index < 58 else "fx-tg" if index == 58 else "fx-ig"
            text = (
                ' =SUM(1,1)\n测试特殊字符 <img src=x onerror="window.effectsInjected=1">'
                if index == 0
                else f"第 {index + 1} 条示例互动：分享工具使用心得，交流实际效果。"
            )
            published = now - (9 * 86400 if index == 14 else 500 + index * 60)
            reference = f"https://x.com/fixture/status/{100000 + index}"
            db.execute(
                "INSERT INTO items(id,task_id,account_id,position,payload,payload_hash,due_at,status,external_reference,result) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    "fx-task-a" if index < 50 else "fx-task-b",
                    account,
                    index,
                    canonical({"action": "reply", "text": text, "target": "示例讨论"}),
                    "fixture",
                    published,
                    "confirmed",
                    reference,
                    canonical({"verified": True}),
                ),
            )
            if index != 15:
                record_publication(db, key, {"published_at": published}, now)
            if index in (10, 59):
                continue
            samples = 105 if index == 0 else 2
            for sample in reversed(range(samples)):
                metrics = {
                    "views": index * 10 + sample,
                    "likes": index % 3,
                    "replies": index % 4,
                    "reposts": None,
                    "reactions": None,
                }
                state = "complete"
                if index in (11, 58):
                    metrics["views"], state = None, "partial"
                if index == 12 and sample == 0:
                    metrics = dict.fromkeys(metrics)
                    state = "unavailable"
                db.execute(
                    "INSERT INTO engagement_snapshots(item_id,observed_at,external_reference,source,status,metrics,estimated,reason) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        key,
                        now - sample * 10800,
                        reference,
                        "synthetic_fixture",
                        state,
                        canonical(metrics),
                        canonical(["views"] if index == 13 else []),
                        None,
                    ),
                )
    return {"total": 60, "recent_total": 58, "history_total": 105}


if __name__ == "__main__":
    path = Path(sys.argv[1]).resolve()
    if "--serve" in sys.argv:
        import uvicorn

        from auto_promotion.api import create_app

        uvicorn.run(
            create_app(db_path=path, worker=False, report_worker=False, token="fixture"),
            host="127.0.0.1",
            port=0,
        )
    else:
        print(json.dumps(seed(path)))
