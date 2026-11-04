"""Filtered publication effects and Excel-compatible exports of saved counters only."""

from __future__ import annotations

import csv
import io
from datetime import date, datetime

from .engagement import METRICS, SUPPORTED
from .statistics import ZONE, day_bounds

STATES = {
    "pending": "等待采集",
    "complete": "已采集",
    "partial": "部分缺失",
    "failed": "采集未成功",
    "unavailable": "平台暂不支持",
    "not_applicable": "不适用",
}
SORTS = {"published_at", "observed_at", "views", "likes", "replies"}


class EngagementEffects:
    def __init__(self, engagement):
        self.engagement = engagement

    def _select(
        self,
        *,
        platform="",
        account_id="",
        task_id="",
        q="",
        date_from="",
        date_to="",
        publication_time="all",
        status="",
        sort="published_at",
        direction="desc",
    ):
        if sort not in SORTS or direction not in {"asc", "desc"}:
            raise ValueError("InvalidEngagementSort")
        if status not in {*STATES, "", "stale"} or publication_time not in {"all", "unknown"}:
            raise ValueError("InvalidEngagementFilter")
        if any(
            not isinstance(value, str) or len(value) > maximum
            for value, maximum in ((platform, 30), (account_id, 160), (task_id, 160), (q, 500))
        ):
            raise ValueError("InvalidEngagementFilter")
        try:
            for value in (date_from, date_to):
                if value and date.fromisoformat(value).isoformat() != value:
                    raise ValueError()
            start = day_bounds(date_from)[0] if date_from else None
            end = day_bounds(date_to)[1] if date_to else None
            if start is not None and end is not None and start >= end:
                raise ValueError()
        except (ValueError, TypeError, OverflowError):
            raise ValueError("InvalidEngagementDate") from None
        all_items = self.engagement.items()
        accounts, tasks = {}, {}
        for item in all_items:
            accounts[item["account_id"]] = {
                "id": item["account_id"],
                "label": item["account_label"],
                "platform": item["platform"],
            }
            tasks[item["task_id"]] = {"id": item["task_id"], "name": item["task_name"]}
            item["collection_state"] = (
                item["capability"]
                if item["capability"] != "supported"
                else ("failed" if item["status"] == "unavailable" else item["status"])
            )
        selected = []
        needle = q.strip().casefold()
        for item in all_items:
            published = item["published_at"]
            if platform and item["platform"] != platform:
                continue
            if account_id and item["account_id"] != account_id:
                continue
            if task_id and item["task_id"] != task_id:
                continue
            if publication_time == "unknown" and published is not None:
                continue
            if start is not None and (published is None or published < start):
                continue
            if end is not None and (published is None or published >= end):
                continue
            if status == "stale" and not item["stale"]:
                continue
            if status and status != "stale" and item["collection_state"] != status:
                continue
            if (
                needle
                and needle
                not in " ".join(
                    str(item[key])
                    for key in (
                        "text",
                        "title",
                        "target",
                        "task_name",
                        "task_id",
                        "account_label",
                        "external_reference",
                    )
                ).casefold()
            ):
                continue
            selected.append(item)
        # Stable ties and missing-last ordering for both directions, including numeric zero.
        selected.sort(key=lambda item: item["item_id"])

        def value(item):
            return item["metrics"].get(sort) if sort in METRICS else item[sort]

        known = [item for item in selected if value(item) is not None]
        missing = [item for item in selected if value(item) is None]
        known.sort(key=value, reverse=direction == "desc")
        return known + missing, {
            "all_total": len(all_items),
            "unknown_publication_total": sum(i["published_at"] is None for i in all_items),
            "options": {
                "platforms": sorted({i["platform"] for i in all_items}),
                "accounts": sorted(accounts.values(), key=lambda a: (a["label"], a["id"])),
                "tasks": sorted(tasks.values(), key=lambda t: (t["name"], t["id"])),
            },
        }

    def snapshot(self, *, limit=50, offset=0, **filters):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ValueError("InvalidEngagementPage")
        items, metadata = self._select(**filters)
        return {
            **metadata,
            "items": items[offset : offset + limit],
            "total": len(items),
            "totals": self.engagement.totals(items),
            "limit": limit,
            "offset": offset,
            "observed_at": self.engagement.clock(),
            "supported_platforms": sorted(SUPPORTED),
            "scope": "filtered_verified_publications_latest_cumulative",
            "timezone": "Asia/Shanghai",
        }

    def detail(self, item_id, *, limit=100):
        item = next((i for i in self.engagement.items() if i["item_id"] == item_id), None)
        if item is None:
            return None
        samples = self.engagement.history(item_id, limit=limit)
        total = self.engagement.store.one(
            "SELECT COUNT(*) total FROM engagement_snapshots WHERE item_id=?", (item_id,)
        )["total"]
        return {"item": item, "samples": samples, "sample_total": total}

    def export(self, **filters):
        items, _ = self._select(**filters)
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "互动ID",
                "平台",
                "账号",
                "任务ID",
                "任务名称",
                "互动类型",
                "标题",
                "正文",
                "目标",
                "发布时间(北京时间)",
                "观看累计",
                "点赞累计",
                "回复累计",
                "观看较上次变化",
                "点赞较上次变化",
                "回复较上次变化",
                "上次采集(北京时间)",
                "最近采集(北京时间)",
                "采集状态",
                "数据待刷新",
                "约数指标",
                "原文链接",
            ]
        )

        def timestamp(value):
            return datetime.fromtimestamp(value, ZONE).isoformat() if value is not None else ""

        def cell(value):
            # Only text is escaped; real negative deltas remain numbers in spreadsheets.
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                return "'" + value
            return "" if value is None else value

        for item in items:
            writer.writerow(
                [
                    cell(value)
                    for value in [
                        item["item_id"],
                        item["platform"],
                        item["account_label"],
                        item["task_id"],
                        item["task_name"],
                        item["action"],
                        item["title"],
                        item["text"],
                        item["target"],
                        timestamp(item["published_at"]),
                        *[
                            f"约 {item['metrics'][key]}"
                            if key in item["estimated"]
                            else item["metrics"][key]
                            for key in ("views", "likes", "replies")
                        ],
                        *[item["delta"][key] for key in ("views", "likes", "replies")],
                        timestamp(item["previous_observed_at"]),
                        timestamp(item["observed_at"]),
                        STATES[item["collection_state"]],
                        "是" if item["stale"] else "否",
                        ",".join(item["estimated"]),
                        item["external_reference"],
                    ]
                ]
            )
        return buffer.getvalue().encode("utf-8-sig")
