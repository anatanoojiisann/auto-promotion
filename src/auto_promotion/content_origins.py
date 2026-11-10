"""Version-bound readback from content batches; no dispatch, retry or provider calls."""

import json
from collections import defaultdict

from .models import ItemInput, digest


def bindings(db, *, candidate_id=None, task_id=None, item_id=None):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='hf_candidates'").fetchone():
        return []
    where, args = ["l.source_kind='hf_candidate'"], []
    for key, value in (("c.id", candidate_id), ("i.task_id", task_id), ("i.id", item_id)):
        if value is not None:
            where.append(key + "=?")
            args.append(value)
    return db.execute(
        "SELECT c.*,p.config origin_config,l.source_revision,e.id content_id,e.platform content_platform,"
        "e.payload content_payload,e.context origin_context,e.content_hash,"
        "b.request batch_request,b.request_hash,b.id binding_batch_id,"
        "i.id binding_item_id,i.task_id binding_task_id,i.account_id binding_account_id,"
        "i.payload binding_payload,i.payload_hash binding_payload_hash,i.status item_status,"
        "i.result item_result,i.external_reference item_reference,i.claimed_at item_claimed_at,"
        "i.attempt_id binding_attempt_id,t.mode task_mode,t.state task_state,pe.published_at evidence_at "
        "FROM content_producer_links l JOIN content_queue_entries e ON e.id=l.content_id "
        "JOIN hf_candidates c ON c.id=l.source_id JOIN hf_policies p ON p.id=c.policy_id "
        "JOIN content_queue_members m ON m.content_id=e.id "
        "JOIN content_queue_batches b ON b.id=m.batch_id "
        "JOIN items i ON i.task_id=b.task_id AND i.position=m.position "
        "JOIN tasks t ON t.id=i.task_id LEFT JOIN publication_evidence pe ON pe.item_id=i.id "
        "WHERE " + " AND ".join(where) + " ORDER BY e.created_at,e.id",
        args,
    ).fetchall()


def object_value(value):
    """Malformed optional provenance must not prevent a native receipt commit."""
    try:
        result = json.loads(value)
    except (ValueError, TypeError):
        return {}
    return result if isinstance(result, dict) else {}


def verified_binding(row):
    try:
        payload, context = object_value(row["content_payload"]), object_value(row["origin_context"])
        request, actual = object_value(row["batch_request"]), object_value(row["binding_payload"])
        if (
            digest(request) != row["request_hash"]
            or digest(actual) != row["binding_payload_hash"]
            or digest({"platform": row["content_platform"], "payload": payload, "context": context})
            != row["content_hash"]
            or context.get("producer") != "hf_candidate"
            or context.get("source_id") != row["id"]
            or context.get("policy_id") != row["policy_id"]
        ):
            return False
        assignment = next(a for a in request["assignments"] if a["content_id"] == row["content_id"])
        if (
            assignment["account_id"] != row["binding_account_id"]
            or actual.get("platform") != row["content_platform"]
        ):
            return False
        expected = ItemInput(
            **payload, account_id=assignment["account_id"], scheduled_at=assignment["scheduled_at"]
        )
        actual = ItemInput(
            **{
                k: v
                for k, v in actual.items()
                if k not in {"position", "platform", "account_label"}
            }
        )
        return expected.model_dump() == actual.model_dump()
    except (KeyError, ValueError, TypeError, StopIteration):
        return False


def current_version(row):
    context, payload = object_value(row["origin_context"]), object_value(row["content_payload"])
    if (
        context.get("source_hash") != row["source_hash"]
        or payload.get("text") != row["draft"]
        or context.get("source_event_id") != row["source_event_id"]
    ):
        return False
    if "candidate_version" in context and context["candidate_version"] != row["version"]:
        return False
    args = [row["version"], row["source_hash"], row["draft"]]
    return row["source_revision"] in {digest(args), digest(["hf-content/2", *args])}


def projection(row):
    result = object_value(row["item_result"])
    state = row["item_status"]
    if state == "confirmed":
        if result.get("verified") is True and row["item_reference"]:
            if row["task_mode"] == "simulation" or row["item_reference"].startswith(
                "simulation://"
            ):
                return "SIMULATED", None, None
            if (
                result.get("native_id")
                or result.get("message_id")
                or result.get("native_comment_id")
            ):
                return "PUBLISHED", None, None
        return "VERIFYING", "content_queue_receipt_unverified", None
    if state in {"unknown", "scheduled"} or (
        state == "cancelled"
        and (
            result.get("submission_started")
            or (row["binding_attempt_id"] and result.get("confirmed_not_submitted") is not True)
        )
    ):
        return "VERIFYING", "publication_outcome_unknown", None
    if state in {"executing", "claimed"}:
        return "PUBLISHING", None, None
    if state == "not_attempted":
        return (
            "READY",
            "content_queue_task_draft" if row["task_state"] == "draft" else "content_queue_queued",
            None,
        )
    return "CLOSED", result.get("reason"), state


def sync_content_origins(db, now, *, task_id=None, item_id=None):
    groups = defaultdict(list)
    for row in bindings(db, task_id=task_id, item_id=item_id):
        if (
            object_value(row["origin_config"]).get("content_queue_only")
            and verified_binding(row)
            and current_version(row)
        ):
            groups[row["id"]].append(row)
    count = 0
    for rows in groups.values():
        first = rows[0]
        if first["task_id"] or first["item_id"]:
            rows = [
                r
                for r in rows
                if (r["binding_task_id"], r["binding_item_id"]) == (r["task_id"], r["item_id"])
            ]
        if len(rows) != 1:
            continue  # Never pick a different execution or an ambiguous binding.
        row = rows[0]
        state, reason, closed = projection(row)
        changes = {
            "task_id": row["binding_task_id"],
            "item_id": row["binding_item_id"],
            "execution_account_id": row["binding_account_id"],
            "state": state,
            "block_reason": reason,
            "closed_reason": closed,
            "external_reference": row["item_reference"]
            if state in {"PUBLISHED", "SIMULATED"}
            else row["external_reference"],
            "published_at": row["evidence_at"]
            if state in {"PUBLISHED", "SIMULATED"}
            else row["published_at"],
            "verification_started_at": row["verification_started_at"]
            or (row["item_claimed_at"] or now if state in {"VERIFYING", "PUBLISHING"} else None),
        }
        if any(row[k] != v for k, v in changes.items()):
            db.execute(
                "UPDATE hf_candidates SET "
                + ",".join(k + "=?" for k in changes)
                + ",updated_at=? WHERE id=? AND version=?",
                (*changes.values(), now, row["id"], row["version"]),
            )
            count += 1
    return count


def content_origin_history(db, candidate_id):
    result = []
    for row in bindings(db, candidate_id=candidate_id):
        if not object_value(row["origin_config"]).get("content_queue_only"):
            continue
        valid = verified_binding(row)
        context = object_value(row["origin_context"])
        result.append(
            {
                "content_id": row["content_id"],
                "source_revision": row["source_revision"],
                "candidate_version": context.get("candidate_version"),
                "task_id": row["binding_task_id"],
                "item_id": row["binding_item_id"],
                "verified_binding": valid,
                "state": projection(row)[0] if valid else "UNVERIFIED_BINDING",
                "external_reference": row["item_reference"] if valid else None,
            }
        )
    return result
