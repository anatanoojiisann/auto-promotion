"""One commissioned native pass, using the normal queue, immutable item and one-use permit."""

import json

from .backends.common import BackendError


def adapter(service, account_id, task_id=None):
    if task_id is not None and task_id not in service.config.single_pass_task_ids:
        from .temporal_tasks import dynamically_authorized

        with service.store.connect() as db:
            if not dynamically_authorized(service, db, task_id, account_id):
                return None
    commissioned = service.config.single_pass_adapters.get(account_id)
    binding = service.config.bindings.get(account_id)
    return (
        commissioned
        if (
            commissioned
            and binding
            and binding.platform == "telegram"
            and binding.receipt_reader == "telegram_api"
        )
        else None
    )


def prepare_context(service, account, claim):
    from .execution_timing import stage

    commissioned = adapter(service, account["id"], claim["task_id"])
    if commissioned is None:
        raise BackendError("PhoneSocialSinglePassNotCommissioned")
    from .phone_visual_preparation import submit_context

    approved = submit_context(service, account["id"], claim["task_id"], claim["item_id"])
    _, binding = service.binding(account["id"])
    with stage(service.engine, claim["task_id"], "phone_target_baseline", account_id=account["id"]):
        baseline = service.observer_baseline(
            account["id"], binding.identity, claim["payload"]["target"]
        )
    context = {
        "target_url": claim["payload"]["target"],
        "target_id": baseline["peer_username"],
        "target_author": "@" + baseline["peer_username"],
        "target_text": baseline["peer_title"],
        "text": claim["payload"]["text"],
        "discard_after_prepare": False,
    }
    if approved is not None:
        if (
            approved["target_id"] != baseline["peer_username"]
            or approved["target_text"] != baseline["peer_title"]
        ):
            raise BackendError("PhoneVisualTargetChangedAfterReview")
        context = approved
    return context, commissioned.adapter_sha256, baseline


def preflight(service, account, payload, execution_job):
    """The native pass still verifies identity/target/body before consuming any permit."""
    from .queue_store import verify_job_claim

    if not execution_job or not service.config.submit_enabled or not service.engine.live_enabled:
        return False
    with service.store.connect() as db:
        job = db.execute(
            "SELECT * FROM execution_jobs WHERE id=?", (execution_job.get("id"),)
        ).fetchone()
        item = (
            db.execute("SELECT * FROM items WHERE id=?", (job["reference_id"],)).fetchone()
            if job
            else None
        )
        task = (
            db.execute("SELECT * FROM tasks WHERE id=?", (item["task_id"],)).fetchone()
            if item
            else None
        )
        if task is None or not adapter(service, account["id"], task["id"]):
            return False
        from .phone_visual_preparation import item_requires_review, trusted_ready

        prepared = (
            trusted_ready(service, account["id"], task["id"], item_id=item["id"]) if item else None
        )
        if (not service.fresh(account) and not prepared) or (
            item and item_requires_review(service, item["id"]) and not prepared
        ):
            return False
        if (
            not item
            or not task
            or item["account_id"] != account["id"]
            or item["status"] != "not_attempted"
            or json.loads(item["payload"]) != payload
            or task["state"] != "active"
            or task["phase"] != "ready"
            or not service.task_authorized(task["id"], account["id"], db=db)
        ):
            return False
        try:
            verify_job_claim(
                db,
                {
                    "platform": "telegram",
                    "account_id": account["id"],
                    "operation": "execute",
                    "execution_job": execution_job,
                    "payload": {"item_id": item["id"]},
                },
            )
        except ValueError:
            return False
    from .account_control_state import snapshot

    return snapshot(service.engine, account["id"], task_id=task["id"])["allowed_read"]
