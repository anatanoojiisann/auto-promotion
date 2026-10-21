"""Cross-account intent locks shared by old tasks and the high-frequency strategy."""

import json
import re


def event_id(platform, payload):
    if platform != "x" or payload.get("action") != "reply":
        return None
    match = re.fullmatch(
        r"https://(?:www\.)?(?:x|twitter)\.com/(?:[\w]+|i/web)/status/(\d+)/?", payload["target"]
    )
    return match[1] if match else None


def claim_intent(db, account, task, payload, item_id, now):
    if payload.get("execution_mode") == "manual":
        return None  # A handoff has not contacted the platform; confirmed manual evidence is read below.
    source_id = event_id(account["platform"], payload)
    if not source_id:
        return None
    policy = json.loads(task["policy"]) if isinstance(task["policy"], str) else task["policy"]
    project = policy.get("project_id", "default")
    key = (project, account["platform"], source_id, "reply")
    row = db.execute(
        "SELECT item_id FROM interaction_intents WHERE project_id=? AND platform=? AND source_event_id=? AND action_kind=?",
        key,
    ).fetchone()
    if row and row[0] != item_id:
        return "interaction_already_claimed"
    # Include receipts created before this additive migration, and unresolved manual evidence.
    for old in db.execute(
        "SELECT i.id,i.account_id,i.status,i.external_reference,i.result,i.payload,t.policy FROM items i JOIN tasks t ON t.id=i.task_id "
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
            json.loads(old["policy"]).get("project_id", "default") == project
            and event_id(account["platform"], json.loads(old["payload"])) == source_id
        ):
            return "interaction_history_unresolved_or_published"
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
    config = json.loads(row["config"])
    if not config["enabled"] or config["run_mode"] != "live":
        return "strategy_paused_or_dry_run"
    if row["expires_at"] <= now:
        return "candidate_expired"
    if row["state"] not in {"READY", "PUBLISHING", "VERIFYING"}:
        return "candidate_not_ready"
    if row["priority"] not in {"P0", "P1"} and row["selected_version"] != row["version"]:
        return "manual_selection_required"
    if row["owner_account_id"] not in config["account_ids"]:
        return "account_authorization_revoked"
    if (
        config.get("account_pool_id")
        and not db.execute(
            "SELECT 1 FROM pool_members WHERE pool_id=? AND account_id=?",
            (config["account_pool_id"], row["owner_account_id"]),
        ).fetchone()
    ):
        return "account_authorization_revoked"
    if row["config_version"] != row["policy_version"]:
        return "candidate_requires_current_rules"
    classification = json.loads(row["classification"])
    facts = {f["id"] for f in config["facts"] if f["verified_at"] <= now < f["valid_until"]}
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
    for seconds, limit in (
        (3600, limits["max_per_account_hour"]),
        (86400, limits["max_per_account_day"]),
    ):
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
