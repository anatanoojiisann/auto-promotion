"""Bounded readiness metadata and reviewed candidates. Never navigates or submits."""

import json
import re
from concurrent.futures import ThreadPoolExecutor

from .account_blocks import active_block
from .core import Conflict
from .models import digest, epoch
from .phone_protocol import capability_status
from .phone_rounds import TOTAL_SECONDS


def _fleet_capabilities(service):
    try:
        return capability_status(service.request("GET", "/capabilities"))
    except Exception as exc:
        reason = getattr(exc, "safe_code", "")
        if not isinstance(reason, str) or not re.fullmatch(r"Phone[A-Za-z0-9_]{1,90}", reason):
            reason = "PhoneFleetReadinessUnavailable"
        return {"software_ready": False, "device_ready": False, "reason": reason}


def preflight(rounds, round_id):
    engine, store, now = rounds.engine, rounds.store, rounds.clock()
    snapshot = rounds.get(round_id)
    common = []
    if not engine.live_enabled:
        common.append("LiveWritesDisabled")
    if store.one("SELECT value FROM meta WHERE key='global_stop'")["value"] != "0":
        common.append("GlobalStop")
    runtime = store.one("SELECT value FROM meta WHERE key='execution_runtime'")
    if not runtime or runtime["value"] != "pooled":
        common.append("PooledRuntimeRequired")
    sources, platform_services = {}, {}
    for member in snapshot["members"]:
        service = getattr(
            engine.adapters,
            "phone_reddit" if member["platform"] == "reddit" else "phone_social",
            None,
        )
        platform_services[member["platform"]] = service
        if service is not None:
            sources[id(service)] = service
    # A social binding may cover five apps. Fetch once per authenticated service, not per app.
    # The established request method bounds token retrieval and /capabilities HTTP timeouts.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {key: pool.submit(_fleet_capabilities, value) for key, value in sources.items()}
        fleet = {key: future.result() for key, future in futures.items()}
    members = []
    for member in snapshot["members"]:
        reasons = []
        item = store.one("SELECT * FROM items WHERE id=?", (member["item_id"],))
        task = store.one("SELECT * FROM tasks WHERE id=?", (member["task_id"],))
        account = engine.account(item["account_id"])
        with store.connect() as db:
            try:
                policy = engine._integrity(db, task, "outbound")
            except Conflict:
                reasons.append("ManifestChanged")
                policy = {}
            if active_block(db, account["id"], now):
                reasons.append("AccountOutboundBlocked")
        if account["disabled"] or account["status"] != "healthy":
            reasons.append("AccountNotHealthy")
        if max(account["cooldown_until"], account["next_at"]) > now:
            reasons.append("AccountWaitRequired")
        if not policy.get("ends_at") or epoch(policy["ends_at"]) <= now:
            reasons.append("TaskWindowElapsed")
        service = platform_services[member["platform"]]
        if service is None or account["id"] not in service.config.bindings:
            reasons.append("PhoneBindingRequired")
        else:
            remote = fleet[id(service)]
            if not remote.get("software_ready"):
                reasons.append(remote.get("reason") or "PhoneFleetReadinessUnavailable")
            elif not remote.get("device_ready"):
                reasons.append("PhoneFleetDeviceNotReady")
            elif (
                remote.get("operations", {}).get(
                    "reddit_submit" if member["platform"] == "reddit" else "social_submit"
                )
                != "supported"
            ):
                reasons.append("PhoneFleetSubmissionUnsupported")
            if not service.config.submit_enabled:
                reasons.append("PhoneSubmissionDisabled")
            if task["id"] not in service.config.authorized_task_ids:
                reasons.append("PhoneTaskOutsideAuthorizedScope")
            try:
                if not service.fresh(account):
                    reasons.append("FreshPhoneIdentityRequired")
            except Exception:
                reasons.append("FreshPhoneIdentityRequired")
            if member["platform"] == "reddit":
                if not service.config.reddit_read_access_token:
                    reasons.append("OfficialReadAuthorizationRequired")
            else:
                binding = service.config.bindings[account["id"]]
                if member["platform"] == "telegram" and binding.receipt_reader == "telegram_api":
                    if account["id"] not in service.config.telegram_observers:
                        reasons.append("OfficialReadAuthorizationRequired")
                else:
                    try:
                        _, _, observation = service.preparation(
                            account["id"],
                            json.loads(item["payload"]),
                            item_id=item["id"],
                            task_id=task["id"],
                        )
                    except Exception:
                        observation = None
                    proof = ((observation or {}).get("result") or {}).get("social") or {}
                    if not (
                        observation
                        and observation.get("status") == "succeeded"
                        and (observation.get("result") or {}).get("simulated") is False
                        and proof.get("receipt_capable") is True
                    ):
                        reasons.append("IndependentReceiptCommissioningRequired")
        members.append(
            {
                "platform": member["platform"],
                "task_id": task["id"],
                "ready": not reasons and not common,
                "blockers": common + list(dict.fromkeys(reasons)),
            }
        )
    return {
        "round_id": round_id,
        "observed_at": now,
        "read_only": True,
        "readiness_scope": "local_configuration_and_authenticated_fleet_capabilities",
        "capability_sources_checked": len(sources),
        "ready_count": sum(m["ready"] for m in members),
        "members": members,
        "submission_authorized": False,
    }


def shortlist(engine, account_id, *, limit=3):
    """Reuse existing reviewed content; original planner and sender still recheck every gate."""
    if type(limit) is not int or not 1 <= limit <= 3:
        raise Conflict("PhoneRoundCandidateLimitInvalid")
    account, now = engine.account(account_id), engine.clock()
    if account["mode"] != "live":
        raise Conflict("PhoneRoundLiveAccountRequired")
    found, excluded, targets = [], {}, set()
    with engine.store.connect() as db:
        rows = db.execute(
            "SELECT * FROM opportunities WHERE platform=? ORDER BY updated_at DESC,id LIMIT 2000",
            (account["platform"],),
        ).fetchall()
        for row in rows:
            value = json.loads(row["payload"])
            reason = None
            target = value.get("target", "").casefold()
            try:
                expires = epoch(value["expires_at"]) if value.get("expires_at") else 0
                published = epoch(value["published_at"]) if value.get("published_at") else 0
            except (ValueError, TypeError, OverflowError):
                expires = published = 0
            if not (
                0 <= now - row["updated_at"] <= 900
                and 0 <= now - published <= 72 * 3600
                and expires >= now + TOTAL_SECONDS
            ):
                reason = "stale_or_short_window"
            elif (
                value.get("permission") != "allowed"
                or not value.get("permission_evidence", "").strip()
                or account_id not in value.get("permission_account_ids", [])
            ):
                reason = "account_permission_required"
            elif not value.get("draft_text", "").strip() or not value.get("context", "").strip():
                reason = "contextual_draft_required"
            elif not db.execute(
                "SELECT 1 FROM audit WHERE kind='growth.content_reviewed' AND json_extract(details,'$.opportunity_id')=? AND json_extract(details,'$.revision')=? LIMIT 1",
                (row["id"], row["revision"]),
            ).fetchone():
                reason = "current_revision_review_required"
            elif db.execute(
                "SELECT 1 FROM attempts WHERE target_key=? LIMIT 1",
                (digest([account["platform"], target]),),
            ).fetchone():
                # Includes unknown outcomes, regardless of local account aliases.
                reason = "existing_attempt_query_only"
            elif (
                target in targets
                or db.execute(
                    "SELECT 1 FROM items i JOIN tasks t ON t.id=i.task_id JOIN accounts a ON a.id=i.account_id WHERE a.platform=? AND lower(json_extract(i.payload,'$.target'))=? AND t.state='active' LIMIT 1",
                    (account["platform"], target),
                ).fetchone()
            ):
                reason = "target_already_scheduled"
            if reason:
                excluded[reason] = excluded.get(reason, 0) + 1
                continue
            targets.add(target)
            found.append(
                {
                    "opportunity_id": row["id"],
                    "revision": row["revision"],
                    "content_key": row["content_key"],
                    "payload_hash": digest(value),
                    "target": value["target"],
                    "expires_at": value["expires_at"],
                }
            )
            if len(found) >= limit:
                break
    return {
        "account_id": account_id,
        "platform": account["platform"],
        "observed_at": now,
        "read_only": True,
        "submission_authorized": False,
        "candidates": found,
        "blockers": [] if found else ["ReviewedCandidatePoolEmpty"],
        "refresh_required": not bool(found),
        "next_actions": []
        if found
        else ["collect_fresh_content", "review_account_permission", "prepare_contextual_draft"],
        "excluded": excluded,
        "scan_limit": 2000,
    }
