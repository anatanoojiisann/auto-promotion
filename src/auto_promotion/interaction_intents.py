"""Cross-account intent locks shared by old tasks and the high-frequency strategy."""

import json
import re


def scope_id(db, project):
    row = db.execute(
        "SELECT scope_id FROM hf_dedupe_projects WHERE project_id=?", (project,)
    ).fetchone()
    return row[0] if row else project


def reserve_task_intents(db, task, now):
    """Legacy starts and joint sender creation share the same atomic reservation."""
    policy = json.loads(task["policy"])
    project = scope_id(db, policy.get("project_id", "default"))
    for item in db.execute(
        "SELECT i.*,a.platform FROM items i JOIN accounts a ON a.id=i.account_id WHERE task_id=?",
        (task["id"],),
    ).fetchall():
        payload = json.loads(item["payload"])
        event = event_id(item["platform"], payload)
        if not event or payload.get("execution_mode") == "manual":
            continue
        db.execute(
            "INSERT OR IGNORE INTO interaction_intents(project_id,platform,source_event_id,action_kind,item_id,state,created_at) VALUES(?,?,?,'reply',?,'reserved',?)",
            (project, item["platform"], event, item["id"], now),
        )


def historical_sender(db, scope, platform, event):
    for item in db.execute(
        "SELECT i.*,t.policy FROM items i JOIN tasks t ON t.id=i.task_id JOIN accounts a ON a.id=i.account_id WHERE a.platform=? AND (i.attempt_id IS NOT NULL OR (t.state='active' AND i.status='not_attempted') OR i.status IN ('confirmed','executing','unknown','scheduled')) ORDER BY i.rowid",
        (platform,),
    ).fetchall():
        if (
            scope_id(db, json.loads(item["policy"]).get("project_id", "default")) == scope
            and event_id(platform, json.loads(item["payload"])) == event
        ):
            if json.loads(item["result"]).get("confirmed_not_submitted") is True:
                continue
            return item
    return None


def transfer_reserved_task_intents(db, old_task_id, new_task_id, now):
    """Move only unsent reservations for an explicitly rescheduled exact copy.

    Both tasks must be stopped. Attempts, receipts, claimed intents and frozen
    payloads are never reset or modified. The caller owns the write transaction.
    """
    old = db.execute("SELECT * FROM tasks WHERE id=?", (old_task_id,)).fetchone()
    new = db.execute("SELECT * FROM tasks WHERE id=?", (new_task_id,)).fetchone()
    if (
        not old
        or not new
        or old["state"] not in {"paused", "cancelled", "completed"}
        or new["state"] not in {"draft", "paused"}
    ):
        raise ValueError("ReservationTransferRequiresStoppedTasks")
    if old["mode"] != new["mode"] or json.loads(old["account_ids"]) != json.loads(
        new["account_ids"]
    ):
        raise ValueError("ReservationTransferAccountMismatch")
    projects = [
        scope_id(db, json.loads(t["policy"]).get("project_id", "default")) for t in (old, new)
    ]
    if projects[0] != projects[1]:
        raise ValueError("ReservationTransferScopeMismatch")
    rows = [
        db.execute("SELECT * FROM items WHERE task_id=? ORDER BY position", (t,)).fetchall()
        for t in (old_task_id, new_task_id)
    ]
    if not rows[0] or len(rows[0]) != len(rows[1]):
        raise ValueError("ReservationTransferCountMismatch")
    if old["state"] == "completed" and any(item["status"] != "missed" for item in rows[0]):
        raise ValueError("ReservationTransferCompletedTaskNotMissed")
    if db.execute(
        "SELECT 1 FROM execution_jobs WHERE task_id IN (?,?) AND state='leased' AND lease_until>?",
        (old_task_id, new_task_id, now),
    ).fetchone():
        raise ValueError("ReservationTransferLeaseActive")
    if db.execute(
        "SELECT 1 FROM cloud_receipt_outbox r JOIN execution_jobs j ON j.id=r.job_id WHERE j.task_id IN (?,?)",
        (old_task_id, new_task_id),
    ).fetchone():
        raise ValueError("ReservationTransferReceiptPending")
    changes = []
    for previous, replacement in zip(*rows):
        for item in (previous, replacement):
            if (
                item["attempt_id"]
                or item["external_reference"]
                or item["status"] not in {"not_attempted", "missed", "cancelled"}
                or db.execute("SELECT 1 FROM attempts WHERE item_id=?", (item["id"],)).fetchone()
            ):
                raise ValueError("ReservationTransferAttemptedItem")
        if replacement["status"] != "not_attempted":
            raise ValueError("ReservationTransferReplacementNotReady")
        payloads = [json.loads(i["payload"]) for i in (previous, replacement)]
        comparable = [
            {
                k: v
                for k, v in p.items()
                if k not in {"scheduled_at", "position", "account_label", "platform"}
            }
            for p in payloads
        ]
        if comparable[0] != comparable[1]:
            raise ValueError("ReservationTransferPayloadMismatch")
        account = db.execute(
            "SELECT platform FROM accounts WHERE id=?", (previous["account_id"],)
        ).fetchone()
        event = event_id(account[0], payloads[0])
        if not event:
            raise ValueError("ReservationTransferUnsupportedEvent")
        key = (projects[0], account[0], event, "reply")
        intent = db.execute(
            "SELECT * FROM interaction_intents WHERE project_id=? AND platform=? AND source_event_id=? AND action_kind=?",
            key,
        ).fetchone()
        if (
            not intent
            or intent["state"] != "reserved"
            or intent["item_id"] not in {previous["id"], replacement["id"]}
        ):
            raise ValueError("ReservationTransferIntentNotReserved")
        changes.append((key, previous["id"], replacement["id"]))
    for key, previous_id, replacement_id in changes:
        db.execute(
            "UPDATE interaction_intents SET item_id=? WHERE project_id=? AND platform=? AND source_event_id=? AND action_kind=? AND item_id=? AND state='reserved'",
            (replacement_id, *key, previous_id),
        )
    from .store import Store

    Store.audit(
        db,
        "interaction.reservations_transferred",
        {
            "source_task_id": old_task_id,
            "replacement_task_id": new_task_id,
            "count": len(changes),
            "attempts_changed": False,
        },
        new_task_id,
        now,
    )
    return len(changes)


def authorize_known_unsent_retry(db, old_item_id, new_item_id, now, *, source):
    """Transfer a claimed intent only after an explicit, proven no-submit retry request."""
    from .models import digest
    from .store import Store

    if source != "explicit_user_known_unsent_local_retry":
        raise ValueError("RetryRequiresExplicitUserInstruction")
    old = db.execute(
        "SELECT i.*,t.state task_state,t.mode task_mode,t.policy FROM items i JOIN tasks t ON t.id=i.task_id WHERE i.id=?",
        (old_item_id,),
    ).fetchone()
    new = db.execute(
        "SELECT i.*,t.state task_state,t.mode task_mode,t.policy FROM items i JOIN tasks t ON t.id=i.task_id WHERE i.id=?",
        (new_item_id,),
    ).fetchone()
    if not old or not new or old["task_state"] != "completed" or new["task_state"] != "draft":
        raise ValueError("RetryRequiresStoppedOriginalAndDraft")
    if old["account_id"] != new["account_id"] or old["task_mode"] != new["task_mode"]:
        raise ValueError("RetryAccountMismatch")
    policy_old, policy_new = json.loads(old["policy"]), json.loads(new["policy"])
    if (
        scope_id(db, policy_old.get("project_id", "default"))
        != scope_id(db, policy_new.get("project_id", "default"))
        or policy_new.get("execution_channel") != "local"
    ):
        raise ValueError("RetryScopeOrChannelMismatch")
    old_payload, new_payload = json.loads(old["payload"]), json.loads(new["payload"])
    ignored = {"scheduled_at", "position", "account_label", "platform"}
    if {k: v for k, v in old_payload.items() if k not in ignored} != {
        k: v for k, v in new_payload.items() if k not in ignored
    }:
        raise ValueError("RetryPayloadMismatch")
    if (
        new["status"] != "not_attempted"
        or new["attempt_id"]
        or new["external_reference"]
        or db.execute("SELECT 1 FROM attempts WHERE item_id=?", (new_item_id,)).fetchone()
    ):
        raise ValueError("RetryReplacementNotUnattempted")
    result = json.loads(old["result"])
    if (
        old["status"] != "failed_known"
        or not old["attempt_id"]
        or old["external_reference"]
        or result.get("status") != "failed_known"
        or result.get("verified") is not False
        or result.get("confirmed_not_submitted") is not True
        or result.get("submission_started") is not False
    ):
        raise ValueError("RetrySubmissionNotDisproved")
    attempts = db.execute("SELECT * FROM attempts WHERE item_id=?", (old_item_id,)).fetchall()
    if (
        len(attempts) != 1
        or attempts[0]["id"] != old["attempt_id"]
        or attempts[0]["outcome"] != "failed_known"
        or not attempts[0]["finished_at"]
    ):
        raise ValueError("RetryAttemptEvidenceMismatch")
    old_job = db.execute(
        "SELECT * FROM execution_jobs WHERE reference_id=? AND kind='send'", (old_item_id,)
    ).fetchone()
    if (
        not old_job
        or old_job["state"] != "failed_known"
        or old_job["owner"]
        or old_job["lease_until"] > now
    ):
        raise ValueError("RetryPriorJobNotSettled")
    if db.execute(
        "SELECT 1 FROM cloud_receipt_outbox WHERE job_id IN (?,?)",
        (old_job["id"], "send:" + new_item_id),
    ).fetchone():
        raise ValueError("RetryReceiptPending")
    receipts = db.execute(
        "SELECT outcome FROM cloud_receipt_history WHERE job_id=?", (old_job["id"],)
    ).fetchall()
    evidence_source = "channel_receipt_history"
    if not receipts:
        # Offline-only executions have no online receipt history. Accept only
        # the account-local FINAL envelope, revalidated against the frozen
        # manifest, attempt, identity and job by the existing proof routine.
        # A progress checkpoint or caller-supplied evidence is never enough.
        from .execution_channels import independent_scope
        from .final_receipts import proof
        from .store import Store

        state_row = db.execute("SELECT value FROM meta WHERE key='execution_channel'").fetchone()
        state = json.loads(state_row[0]) if state_row else {}
        scope = independent_scope(db)
        channel = db.execute(
            "SELECT channel FROM task_channel_claims WHERE job_id=?", (old_job["id"],)
        ).fetchone()
        local_audit = db.execute(
            "SELECT 1 FROM audit WHERE task_id=? AND kind='execution.job_channel' "
            "AND json_extract(details,'$.job_id')=? AND json_extract(details,'$.channel')='local' LIMIT 1",
            (old["task_id"], old_job["id"]),
        ).fetchone()
        database = db.execute("PRAGMA database_list").fetchone()[2]
        if not (
            state.get("active") == "local"
            and state.get("offline_only") is True
            and state.get("authorization") == "local_queue"
            and state.get("until", 0) > now
            and old["account_id"] in state.get("account_ids", [])
            and scope
            and sorted(scope.get("account_ids", [])) == sorted(state.get("account_ids", []))
            and account_is_x(db, old["account_id"])
            and channel
            and channel[0] == "local"
            and local_audit
        ):
            raise ValueError("RetryReceiptEvidenceMissing")
        local_proof = proof(Store(database, initialize=False), old)
        if not local_proof or local_proof[2] != result:
            raise ValueError("RetryReceiptEvidenceMissing")
        evidence_source = "native_final_receipt"
    for row in receipts:
        receipt = json.loads(row[0])
        detail = receipt.get("result", {})
        if (
            receipt.get("state") != "failed_known"
            or receipt.get("attempt_id") != old["attempt_id"]
            or detail.get("confirmed_not_submitted") is not True
            or detail.get("submission_started") is not False
            or detail.get("external_reference")
        ):
            raise ValueError("RetryReceiptEvidenceMismatch")
    account = db.execute(
        "SELECT platform FROM accounts WHERE id=?", (old["account_id"],)
    ).fetchone()
    event = event_id(account[0], old_payload) if account else None
    project = scope_id(db, policy_old.get("project_id", "default"))
    if not event or historical_sender(db, project, account[0], event):
        raise ValueError("RetryOtherHistoryUnresolved")
    intent = db.execute(
        "SELECT * FROM interaction_intents WHERE project_id=? AND platform=? AND source_event_id=? AND action_kind='reply'",
        (project, account[0], event),
    ).fetchone()
    if (
        not intent
        or intent["item_id"] != old_item_id
        or intent["state"] != "claimed"
        or intent["candidate_id"]
    ):
        raise ValueError("RetryOriginalClaimNotOwned")
    changed = db.execute(
        "UPDATE interaction_intents SET item_id=?,state='reserved' WHERE project_id=? AND platform=? AND source_event_id=? AND action_kind='reply' AND item_id=? AND state='claimed'",
        (new_item_id, project, account[0], event, old_item_id),
    ).rowcount
    if changed != 1:
        raise ValueError("RetryClaimChanged")
    Store.audit(
        db,
        "interaction.explicit_unsent_retry_authorized",
        {
            "old_item_id": old_item_id,
            "new_item_id": new_item_id,
            "account_id": old["account_id"],
            "source_event_id": event,
            "attempt_id": old["attempt_id"],
            "result_digest": digest(result),
            "source": source,
            "evidence_source": evidence_source,
            "attempts_changed": False,
        },
        new["task_id"],
        now,
    )
    return {"old_item_id": old_item_id, "new_item_id": new_item_id, "source_event_id": event}


def account_is_x(db, account_id):
    row = db.execute("SELECT platform FROM accounts WHERE id=?", (account_id,)).fetchone()
    return bool(row and row[0] == "x")


def event_id(platform, payload):
    if platform != "x" or payload.get("action") != "reply":
        return None
    match = re.fullmatch(
        r"https://(?:www\.)?(?:x|twitter)\.com/(?:[\w]+|i/web)/status/(\d+)/?", payload["target"]
    )
    return match[1] if match else None


def explicit_unsent_retry_source(db, account, task, payload, item_id):
    """Return only the exact audited no-submit attempt underlying this retry."""
    from .models import digest

    marker = db.execute(
        "SELECT details FROM audit WHERE task_id=? AND kind='interaction.explicit_unsent_retry_authorized' AND json_extract(details,'$.new_item_id')=? ORDER BY id DESC LIMIT 1",
        (task["id"], item_id),
    ).fetchone()
    if not marker:
        return None
    proof = json.loads(marker[0])
    if (
        proof.get("source") != "explicit_user_known_unsent_local_retry"
        or proof.get("account_id") != account["id"]
        or proof.get("source_event_id") != event_id(account["platform"], payload)
    ):
        return None
    old = db.execute("SELECT * FROM items WHERE id=?", (proof.get("old_item_id"),)).fetchone()
    if (
        not old
        or old["account_id"] != account["id"]
        or old["status"] != "failed_known"
        or old["external_reference"]
        or event_id(account["platform"], json.loads(old["payload"])) != proof["source_event_id"]
    ):
        return None
    result = json.loads(old["result"])
    if (
        result.get("confirmed_not_submitted") is not True
        or result.get("submission_started") is not False
        or proof.get("result_digest") != digest(result)
        or proof.get("attempt_id") != old["attempt_id"]
    ):
        return None
    attempt = db.execute(
        "SELECT * FROM attempts WHERE id=? AND item_id=?", (old["attempt_id"], old["id"])
    ).fetchone()
    if (
        not attempt
        or attempt["account_id"] != old["account_id"]
        or attempt["task_id"] != old["task_id"]
        or attempt["outcome"] != "failed_known"
        or not attempt["finished_at"]
    ):
        return None
    policy = json.loads(task["policy"]) if isinstance(task["policy"], str) else task["policy"]
    intent = db.execute(
        "SELECT item_id,state FROM interaction_intents WHERE project_id=? AND platform=? AND source_event_id=? AND action_kind='reply'",
        (
            scope_id(db, policy.get("project_id", "default")),
            account["platform"],
            proof["source_event_id"],
        ),
    ).fetchone()
    if not intent or intent["item_id"] != item_id or intent["state"] not in {"reserved", "claimed"}:
        return None
    return {
        "account": old["account_id"],
        "task": old["task_id"],
        "attempt_at": attempt["started_at"],
        "due_at": old["due_at"],
        "target": attempt["target_key"],
        "user": attempt["user_key"],
    }


def intent_blocker(db, account, task, payload, item_id):
    if payload.get("execution_mode") == "manual":
        return None  # A handoff has not contacted the platform; confirmed manual evidence is read below.
    source_id = event_id(account["platform"], payload)
    if not source_id:
        return None
    policy = json.loads(task["policy"]) if isinstance(task["policy"], str) else task["policy"]
    project = scope_id(db, policy.get("project_id", "default"))
    key = (project, account["platform"], source_id, "reply")
    row = db.execute(
        "SELECT item_id FROM interaction_intents WHERE project_id=? AND platform=? AND source_event_id=? AND action_kind=?",
        key,
    ).fetchone()
    if row and row[0] != item_id:
        owner = db.execute("SELECT task_id FROM items WHERE id=?", (row[0],)).fetchone()
        return {
            "reason": "interaction_already_claimed",
            "blocked_by_item_id": row[0],
            "blocked_by_task_id": owner[0] if owner else None,
        }
    # Include receipts created before this additive migration, and unresolved manual evidence.
    for old in db.execute(
        "SELECT i.id,i.task_id,i.account_id,i.status,i.attempt_id,i.external_reference,i.result,i.payload,t.policy FROM items i JOIN tasks t ON t.id=i.task_id "
        "JOIN accounts a ON a.id=i.account_id WHERE a.platform=? AND i.id<>? AND "
        "(i.attempt_id IS NOT NULL OR i.status IN ('confirmed','executing','unknown','scheduled'))",
        (account["platform"], item_id),
    ):
        previous = json.loads(old["result"])
        if (
            old["status"] == "manual_attention"
            and json.loads(old["payload"]).get("execution_mode") == "manual"
            and previous.get("reason") == "manual_publication_required"
            and not old["external_reference"]
            and previous.get("submission_started") is not True
        ):
            continue
        # Only the same reserved candidate/account may follow a definitive no-submit receipt.
        proven_not_submitted = json.loads(old["result"]).get("confirmed_not_submitted") is True
        retry = db.execute(
            "SELECT c.id FROM hf_candidates c JOIN hf_publish_retries r ON r.candidate_id=c.id WHERE c.item_id=? AND r.item_id=? AND c.owner_account_id=?",
            (item_id, old["id"], old["account_id"]),
        ).fetchone()
        if proven_not_submitted and retry:
            continue
        if (
            old["status"] == "failed_known"
            and not old["external_reference"]
            and old["account_id"] == account["id"]
            and previous.get("submission_started") is False
            and proven_not_submitted
        ):
            from .models import digest

            marker = db.execute(
                "SELECT details FROM audit WHERE task_id=? AND kind='interaction.explicit_unsent_retry_authorized' AND json_extract(details,'$.old_item_id')=? AND json_extract(details,'$.new_item_id')=? ORDER BY id DESC LIMIT 1",
                (task["id"], old["id"], item_id),
            ).fetchone()
            if marker:
                evidence = json.loads(marker[0])
                attempt = db.execute(
                    "SELECT outcome,finished_at FROM attempts WHERE id=? AND item_id=?",
                    (old["attempt_id"], old["id"]),
                ).fetchone()
                if (
                    evidence.get("source") == "explicit_user_known_unsent_local_retry"
                    and evidence.get("account_id") == account["id"]
                    and evidence.get("source_event_id") == source_id
                    and evidence.get("attempt_id") == old["attempt_id"]
                    and evidence.get("result_digest") == digest(previous)
                    and attempt
                    and attempt["outcome"] == "failed_known"
                    and attempt["finished_at"]
                ):
                    continue
        if (
            scope_id(db, json.loads(old["policy"]).get("project_id", "default")) == project
            and event_id(account["platform"], json.loads(old["payload"])) == source_id
        ):
            return {
                "reason": "interaction_history_unresolved_or_published",
                "blocked_by_item_id": old["id"],
                "blocked_by_task_id": old["task_id"],
            }
    return None


def claim_intent(db, account, task, payload, item_id, now):
    """Final atomic check and claim; the early read-only gate never reserves."""
    blocked = intent_blocker(db, account, task, payload, item_id)
    if blocked:
        return blocked["reason"]
    source_id = event_id(account["platform"], payload)
    if not source_id or payload.get("execution_mode") == "manual":
        return None
    policy = json.loads(task["policy"]) if isinstance(task["policy"], str) else task["policy"]
    key = (
        scope_id(db, policy.get("project_id", "default")),
        account["platform"],
        source_id,
        "reply",
    )
    db.execute(
        "INSERT OR IGNORE INTO interaction_intents(project_id,platform,source_event_id,action_kind,item_id,state,created_at) VALUES(?,?,?,?,?,'claimed',?)",
        (*key, item_id, now),
    )
    db.execute(
        "UPDATE interaction_intents SET state='claimed' WHERE project_id=? AND platform=? AND source_event_id=? AND action_kind=? AND item_id=?",
        (*key, item_id),
    )
    return None


def candidate_guard(db, item_id, now):
    """Rechecked by both Engine._claim and the native backend immediately before submit."""
    row = db.execute(
        "SELECT c.*,p.config,p.version policy_version FROM hf_candidates c JOIN hf_policies p ON p.id=c.policy_id WHERE c.item_id=?",
        (item_id,),
    ).fetchone()
    if not row:
        return None
    from .interaction_models import InteractionConfig

    model = InteractionConfig.model_validate_json(row["config"])
    config = model.model_dump()
    if not config["enabled"] or config["run_mode"] != "live":
        return "strategy_paused_or_dry_run"
    if row["expires_at"] <= now:
        return "candidate_expired"
    timing = db.execute(
        "SELECT not_before FROM hf_enqueue_times WHERE candidate_id=?", (row["id"],)
    ).fetchone()
    if timing and now < timing[0]:
        return "candidate_waiting_enqueue"
    if row["state"] not in {"READY", "PUBLISHING", "VERIFYING"}:
        return "candidate_not_ready"
    if row["priority"] not in {"P0", "P1"} and row["selected_version"] != row["version"]:
        return "manual_selection_required"
    execution = row["execution_account_id"] or row["owner_account_id"]
    sender = db.execute("SELECT account_id FROM items WHERE id=?", (item_id,)).fetchone()
    if not sender or sender[0] != execution:
        return "execution_account_mismatch"
    allowed_ids = config["account_ids"]
    pool_id = config.get("account_pool_id")
    if model.joint:
        product = next(
            (p for p in model.products if p.enabled and p.product_id == row["primary_product_id"]),
            None,
        )
        allowed_ids = product.execution_account_ids if product else []
        pool_id = product.execution_pool_id if product else None
        if row["priority"] == "P0" and execution != row["conversation_owner_account_id"]:
            return "conversation_owner_mismatch"
        if row["priority"] not in {"P0", "P1"}:
            return "joint_priority_not_dispatched"
    if execution not in allowed_ids:
        return "account_authorization_revoked"
    # Recheck actual dispatch and profile at the existing final submit boundary.
    from .backends.common import x_api_channel

    account = db.execute("SELECT * FROM accounts WHERE id=?", (row["owner_account_id"],)).fetchone()
    channel = x_api_channel(dict(account))
    if channel["blockers"]:
        return channel["blockers"][0]
    if (
        pool_id
        and not db.execute(
            "SELECT 1 FROM pool_members WHERE pool_id=? AND account_id=?",
            (pool_id, execution),
        ).fetchone()
    ):
        return "account_authorization_revoked"
    if row["config_version"] != row["policy_version"]:
        return "candidate_requires_current_rules"
    classification = json.loads(row["classification"])
    facts = {f.id for f in model.active_facts(now, row["primary_product_id"])}
    if (
        not classification.get("verified_fact_ids")
        or not set(classification["verified_fact_ids"]) <= facts
    ):
        return "facts_unverified"
    profile = db.execute(
        "SELECT payload FROM account_profiles WHERE account_id=?", (row["owner_account_id"],)
    ).fetchone()
    if profile:
        profile = json.loads(profile[0])
        if classification.get("language") not in profile["languages"] or (
            profile["allowed_targets"]
            and json.loads(row["source"])["url"] not in profile["allowed_targets"]
        ):
            return "account_profile_mismatch"
    limits = config["publishing"]
    from .account_daily_local import enabled as local_daily_unlimited
    from .account_daily_local import item_uses_local_channel

    for seconds, limit in (
        (3600, limits["max_per_account_hour"]),
        (86400, limits["max_per_account_day"]),
    ):
        if (
            seconds == 86400
            and local_daily_unlimited(db, account["platform"])
            and item_uses_local_channel(db, item_id, execution, now)
        ):
            continue
        if limit is None:
            continue
        start = now - 3600 if seconds == 3600 else now - now % 86400
        used = db.execute(
            "SELECT count(*) FROM attempts WHERE account_id=? AND item_id<>? AND started_at>=? AND outcome<>'failed_known'",
            (row["owner_account_id"], item_id, start),
        ).fetchone()[0]
        if used >= limit:
            return "activity_limit"
    from datetime import datetime
    from zoneinfo import ZoneInfo

    start, end = config["publish_hours"]
    hour = datetime.fromtimestamp(now, ZoneInfo(config["timezone"])).hour
    allowed = start == end or (start <= hour < end if start < end else hour >= start or hour < end)
    if profile:
        work_hour = datetime.fromtimestamp(now, ZoneInfo(profile["timezone"])).hour
        allowed = allowed and profile["work_start_hour"] <= work_hour < profile["work_end_hour"]
    return None if allowed else "outside_publish_hours"
