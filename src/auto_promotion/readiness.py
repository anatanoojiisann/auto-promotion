"""Local execution blockers; these checks never contact or authorize a platform."""

import json
import math

from .models import epoch

DELIVERY_GRACE_SECONDS = 900

REASONS = {
    "tiktok_video_evidence_invalid": "TikTok 视频目标证据已失效或变化，请检查原候选与授权并重新准备新任务",
    "account_outbound_blocked": "账号外发已暂停，请在账号中心处理原因并检查恢复",
    "meta_source_unknown": "旧流程状态或回执无法确认，先只读核验",
    "meta_source_busy": "该账号的旧互动或回复观察仍未结束",
    "meta_outcome_unknown": "该账号存在未决提交，先核对回执",
    "meta_duplicate_target": "该原帖已被其他任务互动或占用，已阻止重复提交",
    "execution_account_mismatch": "发送清单账号与当前执行归属不一致，已阻止提交",
    "conversation_owner_mismatch": "P0 执行账号必须与原会话账号一致",
    "joint_priority_not_dispatched": "联合策略仅处理已确认的 P0/P1 需求",
    "candidate_waiting_enqueue": "尚未到达首次入队等待截止时间，保持未尝试。",
    "facts_unverified": "当前草稿依赖的产品事实尚未核实或已经失效",
    "account_profile_mismatch": "语言或目标不符合发送账号资料",
    "interaction_already_claimed": "同一项目、目标事件的互动已由另一个任务占用",
    "interaction_history_unresolved_or_published": "历史互动已发布或结果未决，禁止重复发送",
    "strategy_paused_or_dry_run": "高频互动策略已停用或切换为 dry-run",
    "candidate_expired": "候选已超过有效期",
    "candidate_not_ready": "候选状态已变化，暂不能发布",
    "manual_selection_required": "P2/P3 需要有效的人工选择",
    "account_authorization_revoked": "账号已不在策略授权范围",
    "candidate_requires_current_rules": "候选需要按当前配置重新检查",
    "outside_publish_hours": "当前时间不在策略发布时段内",
    "task_not_active": "任务未激活或已暂停",
    "task_not_started": "任务尚未开始发送",
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
    "manifest_integrity_failed": "任务配置或发送清单完整性检查未通过",
    "interaction_dependencies_pending": "前置互动尚未全部核验实际发布，暂不开始计时",
    "interaction_dependencies_failed": "前置任务已失败、取消或错过窗口，不能满足发布依赖",
    "interaction_dependency_deadline_elapsed": "等待前置任务已超过截止时间，请重新安排",
    "interaction_dependencies_invalid": "前置互动任务或账号范围不匹配",
    "interaction_schedule_elapsed": "互动完成后的目标时间已过，需要创建新排期",
    "interaction_schedule_outside_window": "互动完成后的目标时间超出本任务窗口",
    "interaction_schedule_pacing_blocked": "现有账号间隔或冷却晚于目标时间，需要重新排期",
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

    schedule_at = (
        max(
            epoch(task["policy"]["starts_at"]),
            min(
                now
                if i["payload"]["action"] in {"schedule_message", "schedule_file"}
                else i["due_at"]
                for i in pending
            ),
            task["next_at"],
        )
        if pending
        else None
    )
    pools = task.get("execution", {}).get("pools", {})
    cloud, channel = pools.get("cloud", {}), pools.get("channel", {})
    scope = cloud.get("task_scope") or {}

    def timestamp(value):
        return value if type(value) in {int, float} and math.isfinite(value) else None

    observed_at = timestamp(scope.get("at"))
    expires_at = timestamp(scope.get("expires_at"))
    global_at = timestamp(cloud.get("at"))
    bound_scope = bool(
        scope.get("scope") == "task"
        and bool(task.get("id"))
        and scope.get("task_id") == task["id"]
        and scope.get("valid_for_task") is True
        and observed_at is not None
        and 0 <= now - observed_at <= 300
        and expires_at is not None
        and expires_at > now
    )
    scoped = bool(
        bound_scope and scope.get("state") == "connected" and scope.get("execution_ready") is True
    )
    scope_failed = bool(
        bound_scope
        and scope.get("state")
        and (scope.get("state") != "connected" or scope.get("execution_ready") is False)
    )
    # A newer, bound sync can explain an older full-history conflict. It is
    # presentation evidence only; claim and final authorization still recheck.
    historical_conflict = bool(
        scoped
        and cloud.get("state") == "FrozenJobChanged"
        and global_at is not None
        and global_at < observed_at
    )
    fallback = channel.get("valid_for_task") is True

    def step(status, label, message, action=None, next_at=None, next_at_kind=None):
        return {
            "status": status,
            "label": label,
            "message": message,
            "next_action": action,
            "next_at": next_at,
            "next_at_kind": next_at_kind,
            "schedule_next_at": schedule_at if schedule_at and schedule_at > now else None,
            "channel_decision": task.get("channel", {}),
            "execution_location": "local_platform_executor",
            "wait_scope": "item_or_account"
            if status not in {"completed", "cancelled", "completed_pending_verification"}
            else "receipt",
            "service_observation": {
                "global_state": cloud.get("state"),
                "global_at": global_at,
                "task_state": scope.get("state"),
                "task_at": observed_at,
                "task_scope_usable": scoped,
                "task_scope_failed": scope_failed,
                "task_retry_at": timestamp(scope.get("retry_at")) if scope_failed else None,
                "historical_conflict": historical_conflict,
            }
            if task["mode"] == "live" and cloud
            else None,
            "blocking_items": item_blockers,
            "blocked_accounts": [
                a["account_id"] for a in task.get("account_checks", []) if not a["ready"]
            ],
            "can_prepare": bool(task["items"] and not item_blockers and not task["manifest_hash"]),
            "counts": counts,
        }

    if task["state"] == "completed" and any(
        i["status"] in {"unknown", "scheduled", "manual_attention"} for i in task["items"]
    ):
        return step(
            "completed_pending_verification",
            "执行已结束 · 回执待处理",
            "执行批次已结束；未知结果只读核验，平台排期仍需核对，人工项目保留待处理状态。",
        )
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
            (
                now - i["due_at"] > DELIVERY_GRACE_SECONDS
                and not (
                    task["phase"] == "starting"
                    and (
                        task["policy"].get("outbound_after_service_recovery_seconds") is not None
                        or task["policy"].get("outbound_after_task_ids")
                    )
                )
            )
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
    if task["phase"] in {"starting", "ready"}:
        for reason in (
            "global_stop",
            "live_writes_disabled",
            "account_disabled",
            "account_outbound_blocked",
            "account_unhealthy",
            "account_cooldown",
            "activity_limit",
            "delivery_limit",
        ):
            if reason in blockers:
                retry = max(
                    (
                        a.get("retry_at") or 0
                        for a in task.get("account_checks", [])
                        if not a["ready"]
                    ),
                    default=0,
                )
                return step(
                    reason,
                    REASONS[reason],
                    REASONS[reason],
                    next_at=retry if retry > now else None,
                    next_at_kind="account_retry" if retry > now else None,
                )
    if not task["items"]:
        return step(
            "prepare_items",
            "等待准备清单",
            "保存每项的账号、目标、完整正文和计划时间。",
            "edit_items",
        )
    if task["phase"] == "starting" and task["policy"].get("outbound_after_task_ids"):
        reasons = {i["reason"] for i in item_blockers}
        if reasons:
            return step(
                "waiting_interactions",
                "等待互动核验",
                "；".join(REASONS[r] for r in sorted(reasons)),
            )
    if (
        task["phase"] == "starting"
        and task["policy"].get("outbound_after_service_recovery_seconds") is not None
    ):
        return step(
            "waiting_execution_service",
            "等待执行服务恢复",
            "执行服务恢复后，按账号节奏与入群满一小时的条件安排互动。",
        )
    if task["phase"] != "ready" and task["phase"] != "starting":
        return step(
            "ready_to_start",
            "等待开始发送",
            "点击开始发送，或在 Codex 对话中明确指令开始互动；系统自动固定清单并执行。",
            "start_task",
        )
    if item_blockers:
        reasons = {i["reason"] for i in item_blockers}
        membership = reasons <= {"membership_unverified"}
        if membership and task["policy"]["max_joins"] and now < epoch(task["policy"]["starts_at"]):
            return step(
                "waiting_join_window",
                "等待入群时间",
                "到达任务入群窗口后，调度器核验所选账号的群成员身份。",
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
                else "调度器按任务窗口和间隔处理入群；核验完成后自动继续发送。",
                "verify_membership" if uncertain else None,
            )
        return step(
            "membership_blocked",
            "外发条件未满足",
            "查看执行前检查中的具体目标和原因；发送前须全部通过。",
        )
    if not task["manifest_hash"]:
        return step("starting", "正在准备发送", "任务已启动，调度器将自动固定清单并继续发送。")
    if pending:
        choice = task.get("channel", {})
        if choice.get("local_authority_expired"):
            return step(
                "waiting_local_authority",
                "本机授权已到期",
                "本任务保持本机选择，等待续期；不会自动改用线上。",
            )
        waits = [a for a in choice.get("accounts", []) if a.get("blocking_reason")]
        if waits:
            wait = waits[0]
            return step(
                "waiting_channel_authority",
                "等待账号通道核验",
                wait["blocking_reason"],
                next_at=wait.get("retry_at") or None,
                next_at_kind="service_retry",
            )
        if (
            task["mode"] == "live"
            and not fallback
            and not historical_conflict
            and cloud.get("state")
            and (cloud["state"] != "connected" or cloud.get("execution_ready") is False)
        ):
            if cloud["state"] == "LocalChannelCheckpointRequired":
                return step(
                    "waiting_execution_service",
                    "等待备用通道初始化",
                    "双通道已配置；首次检查需要等待服务器额度恢复，之后额度耗尽时会自动使用本地通道。",
                    next_at=cloud.get("retry_at"),
                    next_at_kind="service_retry",
                )
            return step(
                "waiting_execution_service",
                "等待执行服务恢复",
                "云端数据库当日写入额度已用尽，等待额度重置后重新检查；已冻结的时间窗仍然有效。"
                if cloud["state"] == "CloudWriteQuotaExceeded"
                else "云端授权服务暂不可用，调度器恢复连接后检查原有时间窗与尚未尝试的项目。",
                next_at=cloud.get("retry_at"),
                next_at_kind="service_retry",
            )
        if task["mode"] == "live" and not fallback and scope_failed:
            return step(
                "waiting_execution_service",
                "等待本任务同步恢复",
                "本任务同步未完成；全局连接状态不能证明本任务已就绪，原有排期保持不变。",
                next_at=timestamp(scope.get("retry_at")),
                next_at_kind="service_retry",
            )
        next_at = max(
            schedule_at,
            max((a.get("pacing_until", 0) for a in task.get("account_checks", [])), default=0),
        )
        if next_at > now:
            return step(
                "waiting_schedule",
                "等待计划时间",
                "任务已启动，到达计划时间并满足间隔后执行。",
                next_at=next_at,
                next_at_kind="schedule",
            )
        return step(
            "ready",
            "已启动 · 本地通道" if fallback else "已启动 · 待执行",
            (
                "服务器写入额度耗尽，按已授权备用本机通道执行。"
                if channel.get("reason") == "CloudWriteQuotaExceeded"
                else "按用户选择及有效授权使用本机通道；去重、限速和回执核验继续生效。"
            )
            if fallback
            else "按启动时固定的清单执行并逐项核验回执。",
        )
    if any(i["status"] == "missed" and not i.get("attempt_id") for i in task["items"]) and not any(
        i["status"] in {"executing", "unknown", "scheduled", "manual_attention"}
        for i in task["items"]
    ):
        return step(
            "expired",
            "有项目的发送窗口已过",
            "未尝试的项目没有发布；请查看逐项结果和原任务的后续安排。",
        )
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
    requires_membership = (
        requires_join or policy.get("outbound_after_membership_seconds") is not None
    )
    if requires_join and not candidate:
        return "candidate_missing"
    if not candidate:
        candidate = db.execute(
            "SELECT * FROM candidates WHERE account_id=? AND lower(target)=lower(?) "
            "ORDER BY rowid DESC LIMIT 1",
            (payload["account_id"], payload["target"]),
        ).fetchone()
    if not candidate:
        return "membership_unverified" if requires_membership else None
    joined = json.loads(candidate["result"])
    if requires_membership or candidate["state"] != "not_attempted":
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
            # Membership receipts retain the legacy one-hour deadline. An
            # explicitly configured task delay is measured from that same
            # verified observation, consistently with schedule resolution.
            delay = policy.get("outbound_after_membership_seconds")
            eligible = epoch(eligible_at) + (delay - 3600 if delay is not None else 0)
        except (KeyError, TypeError, ValueError):
            return "join_wait_unverified"
        if eligible > at:
            return "join_wait_pending"
    return None
