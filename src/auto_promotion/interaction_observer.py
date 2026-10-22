"""Human-started fallback for a read-only joint observation policy."""

import json
import re
import time

from .core import Conflict
from .models import canonical, digest
from .store import Store

FALLBACK_WAIT_SECONDS = 7200


def fallback_status(store: Store, policy_id: str, *, now: float | None = None):
    now = time.time() if now is None else now
    policy = store.one("SELECT * FROM hf_policies WHERE id=?", (policy_id,))
    if not policy:
        raise Conflict("策略不存在")
    config = json.loads(policy["config"])
    if (
        config.get("run_mode") != "discovery_only"
        or config.get("strategy_profile") != "joint_p0_p1"
    ):
        raise Conflict("fallback_requires_joint_read_only_policy")
    last = store.one(
        "SELECT * FROM hf_notices WHERE policy_id=? AND notice_type='readonly_fallback' "
        "ORDER BY created_at DESC LIMIT 1",
        (policy_id,),
    )
    last_high = store.one(
        "SELECT MAX(updated_at) AS at FROM hf_candidates WHERE policy_id=? "
        "AND priority IN ('P0','P1') AND classification_status='classified'",
        (policy_id,),
    )
    anchor = max(
        policy["created_at"],
        last["created_at"] if last else 0,
        (last_high or {}).get("at") or 0,
    )
    rows = store.rows(
        "SELECT id,source,source_event_at,priority,first_seen_at,matched_product_ids "
        "FROM hf_candidates WHERE policy_id=? AND state='DISCOVERED' "
        "AND (priority IS NULL OR priority IN ('P2','P3')) "
        "AND source_event_at>=? ORDER BY first_seen_at DESC LIMIT 20",
        (policy_id, now - 86400),
    )
    return {
        "policy_id": policy_id,
        "due": bool(config.get("enabled")) and now >= anchor + FALLBACK_WAIT_SECONDS,
        "due_at": anchor + FALLBACK_WAIT_SECONDS,
        "anchor_at": anchor,
        "coverage_incomplete": True,
        "last_result": last["body"] if last else None,
        "shortlist": [
            {
                "candidate_id": row["id"],
                "priority": row["priority"],
                "source": json.loads(row["source"]),
                "source_event_at": row["source_event_at"],
                "product_ids": json.loads(row["matched_product_ids"]),
            }
            for row in rows
        ],
    }


def record_fallback(
    store: Store,
    policy_id: str,
    *,
    result: str,
    reason: str,
    candidate_id: str | None = None,
    priority: str | None = None,
    task_id: str | None = None,
    source_url: str | None = None,
    now: float | None = None,
):
    now = time.time() if now is None else now
    state = fallback_status(store, policy_id, now=now)
    if not state["due"]:
        raise Conflict("two_hour_fallback_not_due")
    if result not in {"prepared", "no_match"} or not reason.strip():
        raise Conflict("invalid_fallback_result")
    if result == "prepared" and (priority not in {"P2", "P3"} or not task_id or not source_url):
        raise Conflict("prepared_fallback_requires_priority_source_and_task")
    if result == "no_match" and (candidate_id or priority or task_id or source_url):
        raise Conflict("no_match_has_no_task")
    key = "readonly-fallback:" + policy_id + ":" + str(int(state["anchor_at"]))
    with store.connect(write=True) as db:
        if db.execute("SELECT 1 FROM hf_notices WHERE dedupe_key=?", (key,)).fetchone():
            raise Conflict("fallback_round_already_recorded")
        if result == "prepared":
            task = db.execute("SELECT state,policy FROM tasks WHERE id=?", (task_id,)).fetchone()
            policy = db.execute(
                "SELECT project_id FROM hf_policies WHERE id=?", (policy_id,)
            ).fetchone()
            if (
                not task
                or task["state"] != "draft"
                or json.loads(task["policy"])["project_id"] != policy["project_id"]
            ):
                raise Conflict("fallback_task_must_be_unstarted_in_same_project")
        source = None
        if candidate_id:
            row = db.execute(
                "SELECT * FROM hf_candidates WHERE id=? AND policy_id=? AND state='DISCOVERED' "
                "AND (priority IS NULL OR priority IN ('P2','P3'))",
                (candidate_id, policy_id),
            ).fetchone()
            if not row:
                raise Conflict("fallback_candidate_unavailable")
            source = json.loads(row["source"])
            if source["url"] != source_url:
                raise Conflict("fallback_source_mismatch")
            db.execute(
                "UPDATE hf_candidates SET priority=?,classification_status='manual_review',"
                "classification=?,block_reason='manual_reply_start_required',updated_at=? WHERE id=?",
                (
                    priority,
                    canonical({"priority_reason": reason, "manual_review": True}),
                    now,
                    candidate_id,
                ),
            )
            db.execute(
                "INSERT INTO hf_decisions(candidate_id,version,action,actor,at,details) "
                "VALUES(?,?,?,?,?,?)",
                (
                    candidate_id,
                    row["version"],
                    "fallback_prepare",
                    "operator",
                    now,
                    canonical({"priority": priority, "task_id": task_id, "reason": reason}),
                ),
            )
        snapshot = (
            [
                {
                    "priority": priority,
                    "url": source_url,
                    "need": source["text"] if source else "已单独核查的公开帖子",
                    "reason": reason,
                    "draft": "任务已准备，等待逐条明确启动",
                    "expires_at": None,
                }
            ]
            if result == "prepared"
            else []
        )
        db.execute(
            "INSERT INTO hf_notices(id,policy_id,dedupe_key,channel,notice_type,state,snapshot,body,"
            "created_at,delivered_at,message_id) VALUES(?,?,?,'workbench','readonly_fallback','sent',?,?,?,?,?)",
            (
                "fallback-" + digest(key)[:24],
                policy_id,
                key,
                canonical(snapshot),
                reason,
                now,
                now,
                task_id or "workbench://interactions/" + policy_id,
            ),
        )
        Store.audit(
            db,
            "interaction.readonly_fallback_recorded",
            {
                "policy_id": policy_id,
                "result": result,
                "candidate_id": candidate_id,
                "task_id": task_id,
            },
            now=now,
        )
    return fallback_status(store, policy_id, now=now)


def account_review_status(store: Store, policy_id: str):
    policy = store.one("SELECT * FROM hf_policies WHERE id=?", (policy_id,))
    if not policy:
        raise Conflict("策略不存在")
    config = json.loads(policy["config"])
    if (
        config.get("run_mode") != "discovery_only"
        or not config.get("policy_key", "").endswith("_observer")
        or len(config.get("account_ids", [])) != 1
    ):
        raise Conflict("account_review_requires_single_account_observer")
    run = store.one(
        "SELECT id,seq,state,settled_at,search_account_id FROM hf_scan_runs "
        "WHERE policy_id=? ORDER BY seq DESC LIMIT 1",
        (policy_id,),
    )
    notice = (
        store.one(
            "SELECT id,body,message_id FROM hf_notices WHERE dedupe_key=?",
            ("readonly-account-review:" + policy_id + ":" + run["id"],),
        )
        if run
        else None
    )
    rows = (
        store.rows(
            "SELECT c.id,c.source,c.priority,c.state,d.product_ids FROM hf_scan_candidates sc "
            "JOIN hf_candidates c ON c.id=sc.candidate_id "
            "LEFT JOIN hf_discoveries d ON d.candidate_id=c.id AND d.policy_id=? "
            "WHERE sc.run_id=? AND c.state='DISCOVERED' ORDER BY c.first_seen_at DESC LIMIT 30",
            (policy_id, run["id"]),
        )
        if run
        else []
    )
    return {
        "policy_id": policy_id,
        "account_id": config["account_ids"][0],
        "enabled": config["enabled"],
        "run": run,
        "needs_review": bool(
            config["enabled"]
            and run
            and run["settled_at"]
            and run["state"] in {"partial", "complete"}
            and not notice
        ),
        "recorded": notice,
        "observed_candidates": [
            {
                "candidate_id": row["id"],
                "source": json.loads(row["source"]),
                "priority": row["priority"],
                "product_ids": json.loads(row["product_ids"] or "[]"),
            }
            for row in rows
        ],
    }


def record_account_review(
    store: Store,
    policy_id: str,
    *,
    result: str,
    reason: str,
    priority: str | None = None,
    task_id: str | None = None,
    source_url: str | None = None,
    candidate_id: str | None = None,
    now: float | None = None,
):
    now = time.time() if now is None else now
    status = account_review_status(store, policy_id)
    if not status["needs_review"]:
        raise Conflict("account_round_not_ready_or_already_reviewed")
    if result not in {"prepared", "no_match"} or not reason.strip():
        raise Conflict("invalid_account_review_result")
    if result == "prepared" and (
        priority not in {"P0", "P1", "P2"} or not task_id or not source_url
    ):
        raise Conflict("prepared_account_review_requires_priority_source_and_task")
    if result == "no_match" and (priority or task_id or source_url or candidate_id):
        raise Conflict("no_match_has_no_task")
    key = "readonly-account-review:" + policy_id + ":" + status["run"]["id"]
    with store.connect(write=True) as db:
        if db.execute("SELECT 1 FROM hf_notices WHERE dedupe_key=?", (key,)).fetchone():
            raise Conflict("account_round_already_recorded")
        source = None
        if result == "prepared":
            match = re.search(r"/status/(\d+)(?:\D|$)", source_url)
            if not source_url.startswith(("https://x.com/", "https://www.x.com/")) or not match:
                raise Conflict("account_review_source_must_be_x_post")
            task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            items = db.execute(
                "SELECT * FROM items WHERE task_id=? ORDER BY position", (task_id,)
            ).fetchall()
            if (
                not task
                or task["state"] != "draft"
                or json.loads(task["account_ids"]) != [status["account_id"]]
                or len(items) != 1
                or items[0]["account_id"] != status["account_id"]
            ):
                raise Conflict("account_review_task_must_be_one_unstarted_reply")
            item = json.loads(items[0]["payload"])
            target = re.search(r"/status/(\d+)(?:\D|$)", item.get("target", ""))
            if item.get("action") != "reply" or not target or target[1] != match[1]:
                raise Conflict("account_review_task_target_mismatch")
            for previous in db.execute(
                "SELECT i.task_id,i.attempt_id,i.payload,t.state FROM items i "
                "JOIN tasks t ON t.id=i.task_id WHERE i.task_id<>? "
                "AND json_extract(i.payload,'$.target') LIKE ?",
                (task_id, "%/status/" + match[1] + "%"),
            ).fetchall():
                previous_target = re.search(
                    r"/status/(\d+)(?:\D|$)",
                    json.loads(previous["payload"]).get("target", ""),
                )
                if (
                    previous_target
                    and previous_target[1] == match[1]
                    and (previous["state"] != "cancelled" or previous["attempt_id"])
                ):
                    raise Conflict("account_review_source_already_has_task")
            if candidate_id:
                row = db.execute(
                    "SELECT c.* FROM hf_candidates c JOIN hf_scan_candidates sc "
                    "ON sc.candidate_id=c.id WHERE c.id=? AND sc.run_id=? AND c.state='DISCOVERED'",
                    (candidate_id, status["run"]["id"]),
                ).fetchone()
                if not row:
                    raise Conflict("account_review_candidate_unavailable")
                source = json.loads(row["source"])
                if not re.search(r"/status/" + match[1] + r"(?:\D|$)", source["url"]):
                    raise Conflict("account_review_candidate_source_mismatch")
                db.execute(
                    "UPDATE hf_candidates SET priority=?,classification_status='manual_review',"
                    "classification=?,block_reason='manual_reply_start_required',updated_at=? "
                    "WHERE id=?",
                    (
                        priority,
                        canonical({"priority_reason": reason, "manual_review": True}),
                        now,
                        candidate_id,
                    ),
                )
                db.execute(
                    "INSERT INTO hf_decisions(candidate_id,version,action,actor,at,details) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        candidate_id,
                        row["version"],
                        "account_review_prepare",
                        "operator",
                        now,
                        canonical({"priority": priority, "task_id": task_id, "reason": reason}),
                    ),
                )
        snapshot = (
            [
                {
                    "priority": priority,
                    "url": source_url,
                    "need": source["text"] if source else "已单独核查的公开帖子",
                    "reason": reason,
                    "draft": "草稿任务已准备，等待逐条启动",
                    "expires_at": None,
                }
            ]
            if result == "prepared"
            else []
        )
        db.execute(
            "INSERT INTO hf_notices(id,policy_id,dedupe_key,channel,notice_type,state,snapshot,body,"
            "created_at,delivered_at,message_id) VALUES(?,?,?,'workbench','readonly_account_review','sent',?,?,?,?,?)",
            (
                "review-" + digest(key)[:24],
                policy_id,
                key,
                canonical(snapshot),
                reason,
                now,
                now,
                task_id or "workbench://interactions/" + policy_id,
            ),
        )
        Store.audit(
            db,
            "interaction.readonly_account_review_recorded",
            {
                "policy_id": policy_id,
                "run_id": status["run"]["id"],
                "result": result,
                "task_id": task_id,
            },
            now=now,
        )
    return account_review_status(store, policy_id)
