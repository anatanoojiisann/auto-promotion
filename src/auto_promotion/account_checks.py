"""Read-only account evidence used by task admission and the workbench."""

from .execution_diagnostics import safe_code
from .health_checks import RESTRICTED, VERIFICATION, category

MESSAGES = {
    "healthy": "账号状态已核验，启动时会刷新检查",
    "verification_required": "需要人工完成人机验证，完成后重新检查并启动",
    "restricted": "账号受到平台限制，请先在平台处理并重新检查",
    "cooldown": "账号处于冷却期，请等待冷却结束后重新启动",
    "disabled": "账号已停用，请先处理账号问题并启用",
    "authorization_required": "登录身份或授权已失效，请重新连接账号",
    "navigation_blocked": "平台页面访问受阻，请处理连接后重新检查",
    "temporary_unavailable": "暂时无法确认账号状态，请稍后重新检查",
    "busy": "账号正在执行其他操作，请稍后重新启动",
    "unverified": "尚未确认账号状态，请检查账号连接",
}


def normalize_health(result):
    """A positive identity observation cannot override a platform safety signal."""
    result = dict(result)
    reason = result.get("reason")
    if (
        result.get("verification_required")
        or result.get("challenge_required")
        or result.get("captcha_required")
        or reason in VERIFICATION
    ):
        result.update(healthy=False, reason="BrowserVerificationRequired")
    elif result.get("restricted") or result.get("risk_warning") or reason in RESTRICTED:
        result.update(healthy=False, reason=reason if reason in RESTRICTED else "AccountRestricted")
    elif result.get("rate_limited"):
        result.update(healthy=False, reason=reason or "AccountCooldown")
    elif result.get("service_ready") is False:
        result["healthy"] = False
    return result


def summary(account, now):
    block = account.get("outbound_block")
    if block:
        state = {
            "x_challenge": "verification_required",
            "x_login_required": "authorization_required",
            "x_account_restricted": "restricted",
            "telegram_speaking_restricted": "restricted",
        }.get(block["reason"], "temporary_unavailable")
        return {
            "account_id": account["id"],
            "label": account["label"],
            "platform": account["platform"],
            "ready": False,
            "state": state,
            "reason": block["reason"],
            "message": MESSAGES[state],
            "checked_at": account["checked_at"],
            "retry_at": block.get("until"),
            "pacing_until": account.get("next_at", 0),
        }
    health = normalize_health(account["health"])
    reason = safe_code(health.get("reason"), "unverified")
    state = account["connection_state"]
    if state not in {"disabled", "cooldown", "busy", "temporary_unavailable", "unverified"}:
        if reason in VERIFICATION:
            state = "verification_required"
        elif reason in RESTRICTED:
            state = "restricted"
    if state == "healthy" and not (
        health.get("healthy") is True and health.get("identity_verified") is True
    ):
        state = "unverified"
    retry_at = max(account["cooldown_until"], health.get("next_check_at", 0))
    return {
        "account_id": account["id"],
        "label": account["label"],
        "platform": account["platform"],
        "ready": state == "healthy",
        "state": state,
        "reason": reason,
        "message": MESSAGES.get(state, MESSAGES.get(category(reason), MESSAGES["unverified"])),
        "checked_at": account["checked_at"],
        "retry_at": retry_at if state != "healthy" and retry_at > now else None,
        "pacing_until": account.get("next_at", 0),
    }
