"""Small authoritative control reads; connection evidence is separate from account stops."""

import json


def snapshot(engine, account_id, *, task_id=None):
    from .core import NotFound

    now = engine.clock()
    with engine.store.connect() as db:
        row = db.execute(
            "SELECT id,platform,mode,disabled,cooldown_until FROM accounts WHERE id=?",
            (account_id,),
        ).fetchone()
        if row is None:
            raise NotFound("账号不存在")
        account = dict(row)
        stopped = db.execute("SELECT value FROM meta WHERE key='global_stop'").fetchone()
        global_stop = stopped is None or stopped[0] != "0"
        task = (
            db.execute(
                "SELECT id,state,phase,policy,account_ids,manifest_hash FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            if task_id
            else None
        )
    reason = (
        "global_stop"
        if global_stop
        else "account_disabled"
        if account["disabled"]
        else "account_cooldown"
        if account["cooldown_until"] > now
        else None
    )
    service = getattr(engine.adapters, "phone_social", None)
    binding = service.config.bindings.get(account_id) if service else None
    connection = {"method": "phone", "bound": bool(binding), "identity_verified": False}
    if binding:
        connection.update(
            device_id=binding.device_id,
            platform=binding.platform,
            identity_verified=service.fresh(account),
            identity_check="cached_then_native_before_submit",
        )
    control = {"state": "read_only", "authorization_granted": False}
    if task_id:
        if task is None:
            reason = reason or "task_missing"
        elif account_id not in json.loads(task["account_ids"]):
            reason = reason or "task_account_not_bound"
        elif task:
            try:
                from .task_channels import control_plan, control_snapshot

                plan = control_plan(engine, task_id, account_id, json.loads(task["policy"]))
                with engine.store.connect() as db:
                    control_snapshot(engine, db, plan)
                control.update(state="ready", channel=plan["channel"], local_only=plan["no_cloud"])
            except Exception as error:
                from .cloud_control import CloudUnavailable

                if not isinstance(error, CloudUnavailable):
                    raise
                reason = reason or str(error)
                control["state"] = "waiting_control"
    return {
        "account_id": account_id,
        "platform": account["platform"],
        "global_stop": global_stop,
        "disabled": bool(account["disabled"]),
        "cooldown_until": account["cooldown_until"],
        "allowed_read": reason is None,
        "reason": reason,
        "connection": connection,
        "control": control,
        "observed_at": now,
        "authorization_granted": False,
    }


def account_check(engine, task, account):
    """Select connection evidence without changing the canonical account or desktop login."""
    from .account_checks import summary
    from .health_checks import RESTRICTED, VERIFICATION

    account = dict(account)
    if isinstance(account.get("health"), str):
        account["health"] = json.loads(account["health"])
    policy = task.get("policy") or {}
    if isinstance(policy, str):
        policy = json.loads(policy)
    phone = getattr(engine.adapters, "phone_social", None)
    if (
        policy.get("execution_method") != "phone"
        or not phone
        or account["id"] not in getattr(phone.config, "bindings", {})
        or not callable(getattr(phone, "fresh", None))
    ):
        return summary({**engine.account(account["id"]), **account}, engine.clock())
    reason = (
        "account_busy"
        if account.get("connection_state") == "busy"
        else "account_disabled"
        if account.get("disabled")
        else "account_cooldown"
        if account.get("cooldown_until", 0) > engine.clock()
        else None
    )
    with engine.store.connect() as db:
        from .account_blocks import active_block

        if active_block(db, account["id"], engine.clock()):
            reason = reason or "account_outbound_blocked"
    health_reason = (account.get("health") or {}).get("reason")
    if health_reason in RESTRICTED | VERIFICATION:
        reason = reason or health_reason
    fresh = phone.fresh(account)
    from .phone_visual_preparation import trusted_ready

    prepared = bool(task.get("id") and trusted_ready(phone, account["id"], task["id"]))
    reason = reason or (None if fresh or prepared else "PhoneSocialFreshIdentityRequired")
    return {
        "account_id": account["id"],
        "label": account.get("label", account["id"]),
        "platform": account["platform"],
        "ready": reason is None,
        "state": ("healthy" if fresh else "prepared")
        if reason is None
        else "connection_check_required",
        "reason": reason,
        "message": (
            "手机身份已核验，提交前将再次核对" if fresh else "已准备，发送前将重新核对手机身份"
        )
        if reason is None
        else reason,
        "checked_at": account.get("checked_at"),
        "identity_verified": fresh,
        "native_identity_check_pending": bool(prepared and not fresh),
        "execution_method": "phone",
        "pacing_until": account.get("next_at", 0),
    }


def task_state(task):
    """Business effect never doubles as a device-lease state."""
    items = task.get("items", [])
    values = []
    for item in items:
        result = item.get("result") or {}
        values.append(
            {
                "item_id": item["id"],
                "status": item["status"],
                "submission_started": result.get("submission_started"),
                "native_id": result.get("native_id") or result.get("message_id"),
                "external_reference": item.get("external_reference")
                or result.get("external_reference"),
                "verified": result.get("verified") is True,
                "resource_state": result.get(
                    "resource_state", "busy" if item["status"] == "executing" else "unreported"
                ),
                "receipt_pending": bool(result.get("receipt_pending")),
                "confirmed_not_submitted": result.get("confirmed_not_submitted") is True,
            }
        )
    return {
        "task_id": task["id"],
        "state": task["state"],
        "phase": task["phase"],
        "manifest_hash": task.get("manifest_hash"),
        "items": values,
    }
