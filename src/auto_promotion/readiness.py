"""Local execution blockers; these checks never contact or authorize a platform."""

import json

from .models import epoch

DELIVERY_GRACE_SECONDS = 900

REASONS = {
    "task_not_active": "任务未激活或已暂停",
    "discovery_approval_missing": "缺少有效的发现与入群审批",
    "outbound_approval_missing": "缺少有效的最终外发审批",
    "global_stop": "全局执行已停止",
    "live_writes_disabled": "真实发布未开启",
    "backend_not_commissioned": "平台执行器未启用",
    "account_disabled": "账号已停用",
    "account_unhealthy": "账号健康检查未通过",
    "account_cooldown": "账号仍在冷却期",
    "account_busy": "账号或平台并发槽位被占用，稍后继续未尝试项目",
    "platform_policy_blocked": "此平台的真实自动互动未开放，请为人工清单创建新任务",
    "pacing_wait": "尚未满足账号或任务最小间隔",
    "scheduled_time_pending": "尚未到计划发送时间",
    "delivery_window_elapsed": "计划时间或执行窗口已过期，请创建新任务",
    "earlier_item_pending": "前序执行项尚未结束",
    "backend_gate_blocked": "平台停止、冷却或限额尚未允许执行",
    "activity_limit": "已达到操作次数限额",
    "join_limit": "已达到本轮入群次数限额",
    "join_already_attempted": "该账号已经尝试过此目标，不能重复入群",
    "delivery_limit": "已达到计划投递日限额",
    "candidate_rejected": "目标未通过本轮候选筛选",
    "candidate_missing": "本轮尚未记录该目标的候选资格",
    "membership_unverified": "尚无已核验的成功入群回执",
    "target_cannot_send": "群内文字发送权限尚未核实或不允许发送",
    "join_wait_unverified": "入群等待截止时间尚未核实",
    "join_wait_pending": "尚未满足入群后等待时间",
    "approval_integrity_failed": "审批或清单完整性检查未通过",
}


def task_workflow(task, blockers, item_blockers, now):
    """Describe the next human or scheduler step using local evidence only."""
    pending = [i for i in task["items"] if i["status"] == "not_attempted"]
    counts = {
        "planned": len(task["items"]),
        "not_attempted": len(pending),
        "verified_sent": sum(
            i["status"] == "confirmed"
            and i["result"].get("verified") is True
            and bool(i["external_reference"])
            for i in task["items"]
        ),
        "membership_verified": sum(
            c["state"] in {"joined", "already_joined"}
            and c["result"].get("verified") is True
            and bool(c["result"].get("external_reference"))
            for c in task["candidates"]
        ),
    }

    def step(status, label, message, action=None, next_at=None):
        return {
            "status": status,
            "label": label,
            "message": message,
            "next_action": action,
            "next_at": next_at,
            "can_prepare": status == "prepare_manifest",
            "counts": counts,
        }

    if task["state"] in {"cancelled", "completed"}:
        return step(
            task["state"],
            "已取消" if task["state"] == "cancelled" else "已结束",
            "查看逐项结果和已核验回执。",
        )
    if task["state"] == "paused":
        return step("paused", "已暂停", "恢复任务后继续处理尚未尝试的项目。")
    expired = now >= epoch(task["policy"]["ends_at"]) or (
        not task["manifest_hash"]
        and any(
            now - i["due_at"] > DELIVERY_GRACE_SECONDS
            or (
                i["payload"]["action"] in {"schedule_message", "schedule_file"}
                and i["due_at"] <= now
            )
            or i["status"] == "missed"
            for i in task["items"]
        )
    )
    if expired:
        return step("expired", "执行时间已过期", REASONS["delivery_window_elapsed"])
    if "discovery_approval_missing" in blockers:
        return step(
            "awaiting_discovery_approval",
            "等待发现审批",
            "审核发现与入群范围，完成第一阶段审批。",
            "approve_discovery",
        )
    if not task["items"]:
        return step(
            "prepare_items",
            "等待准备清单",
            "保存每项的账号、目标、完整正文和计划时间。",
            "edit_items",
        )
    if item_blockers:
        reasons = {i["reason"] for i in item_blockers}
        membership = reasons <= {"membership_unverified"}
        if membership and task["policy"]["max_joins"] and now < epoch(task["policy"]["starts_at"]):
            return step(
                "waiting_join_window",
                "等待入群时间",
                "到达已批准的入群窗口后，调度器核验所选账号的群成员身份。",
                next_at=epoch(task["policy"]["starts_at"]),
            )
        if membership:
            uncertain = any(
                c["state"] in {"unknown", "pending_admin_approval"} for c in task["candidates"]
            )
            return step(
                "waiting_membership",
                "等待入群核验",
                "逐项只读核对待确认的成员身份，不重复入群。"
                if uncertain
                else "调度器按已批准的窗口和间隔处理入群；核验完成后才能冻结清单。",
                "verify_membership" if uncertain else None,
            )
        return step(
            "membership_blocked",
            "外发条件未满足",
            "查看执行前检查中的具体目标和原因；冻结前须全部通过。",
        )
    if not task["manifest_hash"]:
        return step(
            "prepare_manifest",
            "可以准备外发审批",
            "入群与计划条件已通过。冻结并预览完整清单，再完成第二阶段审批。",
            "prepare_manifest",
        )
    if "outbound_approval_missing" in blockers:
        return step(
            "awaiting_outbound_approval",
            "等待外发审批",
            "清单已冻结。核对正文和北京时间，输入页面显示的最终外发确认短语。",
            "approve_outbound",
        )
    for reason in ("global_stop", "live_writes_disabled"):
        if reason in blockers:
            return step(reason, REASONS[reason], REASONS[reason])
    if pending:
        next_at = max(
            epoch(task["policy"]["starts_at"]),
            min(
                now
                if i["payload"]["action"] in {"schedule_message", "schedule_file"}
                else i["due_at"]
                for i in pending
            ),
            task["next_at"],
        )
        if next_at > now:
            return step(
                "waiting_schedule",
                "等待计划时间",
                "最终外发已批准，到达计划时间并满足间隔后执行。",
                next_at=next_at,
            )
        return step("ready", "已批准 · 待执行", "按已批准的清单执行并逐项核验回执。")
    return step("verifying", "等待结果核验", "核对已提交项目的投递或排期回执。")


def telegram_blocker(db, task, payload, at):
    if payload["platform"] != "telegram":
        return None
    policy = json.loads(task["policy"])
    candidate = db.execute(
        "SELECT * FROM candidates WHERE task_id=? AND account_id=? AND lower(target)=lower(?) "
        "ORDER BY CASE WHEN decision='qualified' THEN 1 ELSE 0 END,rowid DESC LIMIT 1",
        (task["id"], payload["account_id"], payload["target"]),
    ).fetchone()
    if candidate and candidate["decision"] != "qualified":
        return "candidate_rejected"
    requires_join = bool(policy["max_joins"])
    if requires_join and not candidate:
        return "candidate_missing"
    if not candidate:
        candidate = db.execute(
            "SELECT * FROM candidates WHERE account_id=? AND lower(target)=lower(?) "
            "ORDER BY rowid DESC LIMIT 1",
            (payload["account_id"], payload["target"]),
        ).fetchone()
    if not candidate:
        return None
    joined = json.loads(candidate["result"])
    if requires_join or candidate["state"] != "not_attempted":
        if (
            candidate["state"] not in {"joined", "already_joined"}
            or joined.get("verified") is not True
            or not joined.get("external_reference")
        ):
            return "membership_unverified"
        if joined.get("can_send") is not True:
            return "target_cannot_send"
        eligible_at = joined.get("outbound_eligible_at_utc")
        if not isinstance(eligible_at, str):
            return "join_wait_unverified"
        try:
            eligible = epoch(eligible_at)
        except (KeyError, TypeError, ValueError):
            return "join_wait_unverified"
        if eligible > at:
            return "join_wait_pending"
    return None
