"""Chinese templates using projected operational fields, never user message text."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Asia/Shanghai")
PLATFORMS = {
    "telegram": "Telegram",
    "x": "X",
    "reddit": "Reddit",
    "instagram": "Instagram",
    "facebook": "Facebook",
    "discord": "Discord",
    "tiktok": "TikTok",
}
STATES = {
    "confirmed": "核验成功",
    "executing": "执行中",
    "scheduled": "已排程待核验",
    "unknown": "结果未知",
    "failed_known": "明确失败",
    "missed": "错过",
    "cancelled": "取消",
    "manual_attention": "待人工",
    "not_attempted": "未尝试",
    "unverified": "未核验",
}
REASONS = {
    "restricted": "平台限制或风控",
    "identity_mismatch": "登录身份与登记账号不符",
    "authorization_required": "登录或授权需要重新连接",
    "verification_required": "需要人工完成验证码",
    "cooldown": "账号处于平台冷却期",
    "temporary_unavailable": "连接暂时不可用",
    "navigation_blocked": "页面访问受阻",
    "permission_denied": "平台或目标权限不足",
    "request_rejected": "平台请求被拒绝，具体原因待核实",
    "stale": "账号状态缺少新核验证据",
    "unverified": "账号状态待核验",
    "disabled": "账号已停用",
    "busy": "账号资源占用中",
    "healthy": "账号最近一次检查通过",
}


def clean(value, limit=100):
    value = re.sub(r"[\x00-\x1f\x7f]", " ", str(value))
    value = re.sub(r"https?://\S+", "[链接省略]", value, flags=re.I)
    value = re.sub(r"(?<![\w])\+?\d[\d ()-]{8,}\d(?![\w])", "[号码隐藏]", value)
    value = re.sub(
        r"(?i)(bearer|token|cookie|session|api[_ -]?key|password)\s*[:= ]\s*\S+",
        "[敏感信息隐藏]",
        value,
    )
    return value[:limit].strip()


def when(at):
    return datetime.fromtimestamp(at, ZONE).strftime("%m-%d %H:%M") + "（北京时间）"


def account_notice(account, now, *, recovered=False, duration=0):
    title = (
        "账号恢复"
        if recovered
        else "账号严重预警"
        if account["severity"] == "critical"
        else "账号警告"
    )
    lines = [
        f"【{title}】{account['label']}",
        f"平台：{PLATFORMS[account['platform']]}｜时间：{when(now)}",
    ]
    if recovered:
        lines.append(f"新的账号核验证据已确认恢复；异常持续约 {int(duration // 60)} 分钟。")
    else:
        lines.append("原因：" + REASONS[account["issue"]])
        if account["cooldown_until"] > now:
            lines.append("最早重新检查：" + when(account["cooldown_until"]))
        lines.append(f"影响：{account['tasks']} 个活动任务，{account['pending']} 条未尝试项目。")
        lines.append(
            "处理："
            + (
                "在平台处理限制或核对账号身份，再重新检查。"
                if account["severity"] == "critical"
                else "等待冷却并重新核验；结果未知的投递仅核对原回执。"
                if account["issue"] == "cooldown"
                else "刷新账号检查；按平台提示处理连接或验证。"
            )
        )
    return "\n".join(lines)


def task_notice(task, now, title):
    c, n = task["counts"], task["total"]
    ended = sum(c.get(k, 0) for k in ("confirmed", "failed_known", "missed", "cancelled"))
    parts = [f"{label} {c[k]}" for k, label in STATES.items() if c.get(k)]
    lines = [
        f"【{title}】{task['name']}",
        f"任务：{task['id']}｜截至 {when(now)}",
        f"清单 {n} 条：" + ("，".join(parts) or "等待准备清单。"),
    ]
    if n:
        lines.append(f"核验成功率 {c.get('confirmed', 0) / n:.0%}；处理结束率 {ended / n:.0%}。")
    lines.extend(task["account_lines"][:12])
    if len(task["account_lines"]) > 12:
        lines.append(f"另有 {len(task['account_lines']) - 12} 个账号，详见工作台。")
    lines.append("当前步骤：" + task["waiting"])
    if task.get("next_at", 0) > now:
        lines.append("最早可继续时间：" + when(task["next_at"]))
    if c.get("scheduled") or c.get("unknown"):
        lines.append("排程和未知结果仍待核验；本地暂停或结束不撤回平台已接受的排程。")
    return "\n".join(lines)


def report_notice(report):
    titles = {"interval": "3 小时播报", "daily_20": "20 点当日播报", "daily_final": "全天日报"}
    daily = report["daily"]
    lines = [f"【{titles[report['kind']]}】{report['day']}", f"数据截至 {when(report['cutoff'])}"]
    sections = (
        [("本期", report["period"]), ("当日累计", daily)]
        if report["kind"] == "interval"
        else [("当日累计", daily)]
    )
    for heading, data in sections:
        lines.append(heading + "：")
        for p in data["platforms"]:
            if not p["live_accounts"] and not p["planned_items"]:
                continue
            counts = p["planned_statuses"]
            lines.append(
                f"{PLATFORMS[p['platform']]}：实际发布 {p['published_success']}，"
                f"本期计划 {p['planned_items']}，明确失败 {counts.get('failed_known', 0)}，"
                f"待人工 {counts.get('manual_attention', 0)}，"
                f"排程或未知存量 {p['pending_verification']}。"
            )
        t = data["totals"]
        lines.append(
            f"反馈：收到 {t['received_replies']} 条回复。"
            if t["received_replies"] is not None
            else f"反馈：已观察到 {t['observed_replies']} 条回复，覆盖不完整，回复率暂不可确认。"
        )
        if t["confirmed_without_publication_time"]:
            lines.append(
                f"另有 {t['confirmed_without_publication_time']} 条核验成功但缺少平台发布时间，未计入实际发布。"
            )
    return "\n".join(lines)
