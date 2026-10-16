"""Deterministic qualification, persona matching, template drafts and quota planning."""

import re
import string
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .core import RISK, Conflict
from .models import CAPABILITIES, ItemInput, digest, epoch
from .quotas import Calendar, event, events, scopes, settings


def content_key(value):
    platform = value["platform"]
    if platform == "telegram":
        return digest(
            [
                platform,
                value.get("native_target_id") or value["target"].casefold(),
                value.get("reply_to") or value["external_id"],
            ]
        )
    if platform == "discord":
        return digest(
            [platform, value["target"], value.get("discord_reply_to") or value["external_id"]]
        )
    if platform == "tiktok":
        return digest(
            [
                platform,
                value["target"],
                value.get("tiktok_video_id", ""),
                value.get("tiktok_comment_id") or value["external_id"],
            ]
        )
    path = urlparse(value["target"]).path
    if platform == "x":
        found = re.search(r"/status/(\d+)", path)
        identifier = found[1] if found else value["external_id"]
    elif platform == "reddit":
        found = re.search(r"/comments/([a-z0-9]+)(?:/[^/]+/([a-z0-9]+))?", path, re.I)
        identifier = (
            ("t1_" + found[2] if found[2] else "t3_" + found[1]) if found else value["external_id"]
        )
    else:
        found = re.search(r"/(?:p|reel)/(\w+)", path)
        identifier = found[1] if found else value["external_id"]
    return digest([platform, identifier.casefold()])


TEMPLATE_FIELDS = {"context", "title", "topic", "brand", "role", "facts", "author"}


def render_template(template, values):
    try:
        for _, field, spec, conversion in string.Formatter().parse(template):
            if field is not None and (field not in TEMPLATE_FIELDS or spec or conversion):
                raise Conflict("草稿模板仅支持：" + ", ".join(sorted(TEMPLATE_FIELDS)))
        return template.format_map({k: values.get(k, "") for k in TEMPLATE_FIELDS}).strip()
    except (ValueError, KeyError):
        raise Conflict("草稿模板格式无效") from None


def execution_mode(account, opportunity):
    if (
        opportunity["permission_account_ids"]
        and account["id"] not in opportunity["permission_account_ids"]
    ):
        return "disabled", "互动许可未授予此账号"
    if opportunity["permission"] == "denied":
        return "disabled", "目标不允许此互动"
    if account["mode"] == "simulation":
        return "automatic", "模拟执行"
    platform = account["platform"]
    if platform == "x":
        if opportunity["action"] == "like":
            return "disabled", "X 不开放自动点赞"
        if (
            opportunity["action"] == "reply"
            and "reply" in account.get("automatic_actions", [])
            and opportunity["is_public"]
        ):
            return "automatic", "账号已启用自动回复；提交前核对登录身份与目标回复权限"
        return "manual", "X 浏览器自动执行未开放，保留人工清单"
    if platform == "reddit":
        return "manual", "Reddit 首版保留人工社区互动"
    if platform == "instagram" and opportunity["action"] == "manual_comment":
        return "manual", "Instagram 第三方评论需人工处理"
    if opportunity["permission"] != "allowed" or not opportunity["permission_evidence"].strip():
        return "disabled", "缺少目标互动许可及证据"
    return "automatic", "目标许可已登记，发送前仍核对平台权限"


def work_time(at, profile):
    zone = ZoneInfo(profile["timezone"])
    local = datetime.fromtimestamp(at, zone)
    start, end = profile["work_start_hour"], profile["work_end_hour"]
    if local.hour < start:
        local = local.replace(hour=start, minute=0, second=0, microsecond=0)
    elif local.hour >= end:
        local = (local + timedelta(days=1)).replace(hour=start, minute=0, second=0, microsecond=0)
    return local.timestamp()


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


def build_plan(engine, campaign, pool, opportunities, profiles):
    config, now = campaign["config"], engine.clock()
    policy = config["policy"]
    accounts = pool["accounts"]
    modes = {a["mode"] for a in accounts}
    if len(modes) != 1:
        raise Conflict("活动号池必须包含同一种真实/模拟模式的账号")
    mode = next(iter(modes))
    with engine.store.connect() as db:
        calendar = Calendar(events(db, mode, delivery=True))
        snapshot = {"execution": settings(db), "pool": pool["limits"]}
        contacts = {
            r["content_key"]
            for r in db.execute("SELECT content_key FROM content_contacts WHERE mode=?", (mode,))
        }
        limits_by_account = {}
        for account in accounts:
            fake_task = {
                "id": "planning",
                "campaign_id": campaign["id"],
                "policy": {**policy, "quota_snapshot": snapshot},
            }
            limits_by_account[account["id"]] = scopes(
                db, account, fake_task, {"target": "", "recipient_id": ""}
            )
    assigned = Counter()
    next_at = {
        a["id"]: max(now + 60, epoch(policy["starts_at"]), a["next_at"], a["cooldown_until"])
        for a in accounts
    }
    results = []
    for opportunity in sorted(opportunities, key=lambda o: (-o["payload"]["value"], o["id"])):
        value = opportunity["payload"]
        rejected, choices = [], []
        content = (value["title"] + " " + value["context"] + " " + value["target"]).casefold()
        common = []
        if opportunity["content_key"] in contacts:
            common.append("该内容已被其他活动保留或尝试互动")
        if not value["is_public"]:
            common.append("内容不是公开目标")
        if RISK.search(content):
            common.append("内容包含禁止类别信号")
        if value["confidence"] < policy["min_confidence"]:
            common.append("内容证据置信度不足")
        if value["language"] not in policy["languages"]:
            common.append("内容语言不在活动范围")
        if value["members"] < policy["min_members"] or value["activity"] < policy["min_activity"]:
            common.append("成员数或活跃度不足")
        if any(k.casefold() not in content for k in policy["required_keywords"]) or any(
            k.casefold() in content for k in policy["blocked_keywords"]
        ):
            common.append("内容未通过关键词政策")
        if policy["queries"] and not any(
            any(w in content for w in q.casefold().split()) for q in policy["queries"]
        ):
            common.append("内容与活动主题不相关")
        if policy["target_allowlist"] and value["target"] not in policy["target_allowlist"]:
            common.append("目标超出活动白名单")
        end = min(
            epoch(policy["ends_at"]),
            epoch(value["expires_at"]) if value["expires_at"] else float("inf"),
        )
        for account in accounts if not common else []:
            profile_record = profiles[account["id"]]
            profile = profile_record["payload"]
            errors = []
            if (
                account["platform"] != value["platform"]
                or value["action"] not in CAPABILITIES[account["platform"]]
            ):
                continue
            if account["disabled"] or account["status"] != "healthy":
                errors.append("账号不可用")
            if not profile_record["version"]:
                errors.append("尚未配置账号人设")
            if value["language"] not in profile["languages"]:
                errors.append("人设语言不匹配")
            if profile["allowed_targets"] and value["target"] not in profile["allowed_targets"]:
                errors.append("目标不在人设允许范围")
            if any(k.casefold() in content for k in profile["blocked_keywords"]):
                errors.append("触及人设禁止话题")
            if (
                value["permission_account_ids"]
                and account["id"] not in value["permission_account_ids"]
            ):
                errors.append("互动许可未授予此账号")
            execution, note = execution_mode(account, value)
            if execution == "disabled":
                errors.append(note)
            text = value["draft_text"]
            if not text and profile["reply_template"]:
                text = render_template(
                    profile["reply_template"],
                    {
                        "context": value["context"],
                        "title": value["title"],
                        "topic": ", ".join(value["topics"]),
                        "brand": profile["brand"],
                        "role": profile["role"],
                        "facts": profile["facts"],
                        "author": value["author_id"],
                    },
                )
            no_text = {"like", "repost"}
            if account["platform"] == "tiktok":
                from .backends.tiktok import NO_TEXT_ACTIONS

                no_text |= NO_TEXT_ACTIONS
            if value["action"] in no_text:
                text = ""
            if value["action"] not in no_text and not (text.strip() or value["media"]):
                errors.append("缺少精确正文或人设草稿模板")
            if errors:
                rejected.append(account["label"] + "：" + "；".join(errors))
                continue
            batch_key = (
                f"planning-{account['id']}-{assigned[account['id']] // config['batch_size']}"
            )
            task = {
                "id": batch_key,
                "campaign_id": campaign["id"],
                "account_ids": [account["id"]],
                "policy": {**policy, "quota_snapshot": snapshot},
            }
            payload = ItemInput(
                account_id=account["id"],
                action=value["action"],
                target=value["target"],
                text=text,
                title=value["draft_title"],
                media=value["media"],
                scheduled_at=iso(next_at[account["id"]]),
                reply_to=value["reply_to"],
                discord_reply_to=value["discord_reply_to"],
                tiktok_video_id=value.get("tiktok_video_id", ""),
                tiktok_comment_id=value.get("tiktok_comment_id", ""),
                tiktok_options=value.get("tiktok_options"),
                recipient_id=value["author_id"],
                target_owned=value["target_owned"],
                execution_mode=execution,
                opportunity_id=opportunity["id"],
                content_key=opportunity["content_key"],
                profile_version=profile_record["version"],
            ).model_dump()
            try:
                engine.validate_item(
                    task, ItemInput(**payload), account=account, opportunity=opportunity
                )
            except Conflict as exc:
                rejected.append(account["label"] + "：" + str(exc))
                continue
            limits = []
            for scope, key, quota in limits_by_account[account["id"]]:
                if scope == "task":
                    key = batch_key
                elif scope == "target":
                    key = digest([account["platform"], payload["target"].casefold()])
                elif scope == "user":
                    key = digest(
                        [
                            account["platform"],
                            payload["recipient_id"] or payload["target"].casefold(),
                        ]
                    )
                limits.append((scope, key, quota))
            at = work_time(next_at[account["id"]], profile)
            while at < end:
                slot = calendar.slot(limits, at, end, mode)
                if slot is None:
                    at = end
                    break
                adjusted = work_time(slot, profile)
                if adjusted == slot:
                    at = slot
                    break
                at = adjusted
            if at >= end:
                rejected.append(account["label"] + "：有效期内没有可用容量")
                continue
            topic_matches = sum(
                t.casefold() in content or t.casefold() in {x.casefold() for x in value["topics"]}
                for t in profile["topics"]
            )
            audience_matches = len(
                {x.casefold() for x in profile["audience"]}
                & {x.casefold() for x in value["audience"]}
            )
            score = (
                0.4 * min(1, topic_matches / max(1, len(profile["topics"])))
                + 0.2 * min(1, audience_matches)
                + 0.2
                + 0.2 * value["value"]
            )
            choices.append(
                (score, at, assigned[account["id"]], account["id"], account, payload, task, note)
            )
        if not choices:
            results.append(
                {
                    "opportunity_id": opportunity["id"],
                    "account_id": None,
                    "score": 0,
                    "status": "unassigned",
                    "reasons": common or rejected or ["号池没有支持此平台与动作的账号"],
                    "payload": None,
                    "profile_snapshot": None,
                }
            )
            continue
        score, at, _, _, account, payload, task, note = sorted(
            choices, key=lambda c: (-c[0], c[2], c[1], c[3])
        )[0]
        payload["scheduled_at"] = iso(at)
        calendar.add(event(account, task, payload, at, pool_id=pool["id"]))
        assigned[account["id"]] += 1
        next_at[account["id"]] = at + max(1, account["min_interval"], policy["min_interval"])
        results.append(
            {
                "opportunity_id": opportunity["id"],
                "account_id": account["id"],
                "score": round(score, 4),
                "status": "planned",
                "reasons": [
                    "语言与权限匹配",
                    f"主题/受众匹配得分 {score:.2f}",
                    note,
                    "已按工作时段和各级额度排期",
                ],
                "payload": payload,
                "profile_snapshot": profiles[account["id"]],
            }
        )
    return results, snapshot
