"""Read-only, metadata-only counts for explicitly registered scheduled DM plans.

Plans are not executed or imported into the sender. Message text is used only
locally to validate the existing reviewed manifest hash and is never projected.
"""

import hashlib
import json
import os
import re
import tomllib
from pathlib import Path

from .alert_summary import is_tiktok_direct_message
from .models import epoch

KIND = "tiktok_dm_reply_plan_v1"
PENDING = {"pending", "pending_acceptance", "not_attempted", "draft_empty"}
MANIFEST_FIELDS = (
    "rank",
    "recipient_id",
    "conversation_id",
    "parent_message_id",
    "text",
    "task_name",
)


def read_json(path, limit=2 * 1024 * 1024):
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("plan_too_large")
    return json.loads(raw.decode("utf-8-sig"))


def plan_path(source, root):
    relative = Path(source["path"])
    path = (root / relative).resolve()
    if relative.is_absolute() or not path.is_relative_to((root / "private").resolve()):
        raise ValueError("plan_path_not_allowed")
    return path


def describe_plan(path, root):
    root, path = Path(root).resolve(), Path(path).resolve()
    if not path.is_relative_to((root / "private").resolve()):
        raise ValueError("plan_path_not_allowed")
    data = read_json(path)
    source = {
        "id": data["batch_id"],
        "kind": KIND,
        "platform": "tiktok",
        "account_id": data["account_id"],
        "batch_id": data["batch_id"],
        "automation_id": data["automation_id"],
        "path": path.relative_to(root).as_posix(),
    }
    validate_source(source)
    return source


def validate_source(source):
    if (
        not isinstance(source, dict)
        or source.get("kind") != KIND
        or source.get("platform") != "tiktok"
    ):
        raise ValueError("planned_queue_source_invalid")
    for key in ("id", "account_id", "batch_id", "automation_id"):
        if not isinstance(source.get(key), str) or not re.fullmatch(
            r"[A-Za-z0-9:_-]{1,100}", source[key]
        ):
            raise ValueError("planned_queue_source_invalid")
    if not isinstance(source.get("path"), str):
        raise ValueError("planned_queue_source_invalid")


def load_plan(db, source, root):
    validate_source(source)
    account = db.execute(
        "SELECT platform,mode FROM accounts WHERE id=?", (source["account_id"],)
    ).fetchone()
    if not account or account["platform"] != "tiktok" or account["mode"] != "live":
        raise ValueError("plan_account_mismatch")
    data = read_json(plan_path(source, root))
    if data.get("version") != 1 or any(
        data.get(k) != source[k] for k in ("account_id", "batch_id", "automation_id")
    ):
        raise ValueError("plan_identity_mismatch")
    if (
        data.get("execution_authorized") is not True
        or data.get("automation_status") != "ACTIVE"
        or data.get("paused")
        or data.get("stopped")
        or data.get("completed")
        or data.get("execution_state") not in {"ready", "running", "active", "wait"}
    ):
        return data, False
    home = Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex")))
    automation_file = home / "automations" / source["automation_id"] / "automation.toml"
    with automation_file.open("rb") as stream:
        automation = tomllib.load(stream)
    if automation.get("id") != source["automation_id"] or automation.get("kind") != "heartbeat":
        raise ValueError("plan_automation_mismatch")
    if automation.get("status") != "ACTIVE":
        return data, False
    entries = data.get("entries")
    if (
        not isinstance(entries, list)
        or len(entries) > 500
        or any(not isinstance(e, dict) for e in entries)
    ):
        raise ValueError("plan_entries_invalid")
    manifest = [{k: e[k] for k in MANIFEST_FIELDS} for e in entries]
    actual = hashlib.sha256(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    if data.get("manifest_sha256") != actual:
        raise ValueError("plan_manifest_mismatch")
    return data, True


def native_entries(db, account_id, now):
    rows = db.execute(
        "SELECT i.id,i.task_id,i.status,i.attempt_id,t.state task_state,t.phase,"
        "json_extract(t.policy,'$.ends_at') ends_at,"
        "a.platform,json_extract(i.payload,'$.action') action,"
        "json_extract(i.payload,'$.interaction_channel') interaction_channel,"
        "json_extract(i.payload,'$.tiktok_conversation_id') conversation_id,"
        "json_extract(i.payload,'$.tiktok_message_id') parent_message_id,"
        "EXISTS(SELECT 1 FROM attempts x WHERE x.item_id=i.id) attempted,"
        "EXISTS(SELECT 1 FROM execution_jobs j WHERE j.kind='send' AND j.reference_id=i.id "
        "AND j.task_id=i.task_id AND j.account_id=i.account_id "
        "AND j.state IN ('pending','leased') AND j.expires_at>?) queued "
        "FROM items i JOIN tasks t ON t.id=i.task_id JOIN accounts a ON a.id=i.account_id "
        "WHERE i.account_id=? AND t.mode='live' AND a.mode='live'",
        (now, account_id),
    )
    return [dict(r) for r in rows if is_tiktok_direct_message(r)]


def pending_count(db, source, root, now, seen):
    data, active = load_plan(db, source, root)
    if not active:
        return 0, "inactive"
    native = native_entries(db, source["account_id"], now)
    by_id = {r["id"]: r for r in native}
    count = 0
    for entry in data["entries"]:
        if entry.get("status") not in PENDING:
            continue
        if (
            entry.get("requires_acceptance")
            and data.get("request_acceptance_authorized") is not True
        ):
            continue
        if entry.get("request_acceptance_state") in {"unknown", "acceptance_unknown", "attempting"}:
            continue
        acceptance = entry.get("acceptance_intent")
        if isinstance(acceptance, dict) and acceptance.get("state", acceptance.get("status")) in {
            "unknown",
            "attempting",
            "submitting",
        }:
            continue
        conversation, parent = entry.get("conversation_id"), entry.get("parent_message_id")
        if (
            not isinstance(conversation, str)
            or not conversation
            or not isinstance(parent, str)
            or not parent
        ):
            raise ValueError("plan_event_missing")
        event = (source["account_id"], conversation, parent)
        if event in seen:
            continue
        seen.add(event)
        bound = entry.get("item_id")
        if bound and bound not in by_id:
            raise ValueError("plan_item_binding_missing")
        matches = [
            r
            for r in native
            if r["conversation_id"] == conversation and r["parent_message_id"] == parent
        ]
        if bound and by_id[bound] not in matches:
            raise ValueError("plan_item_binding_mismatch")
        bound_task = entry.get("task_id")
        if bound_task:
            task = db.execute(
                "SELECT state,mode,account_ids,json_extract(policy,'$.ends_at') ends_at FROM tasks WHERE id=?",
                (bound_task,),
            ).fetchone()
            if (
                not task
                or task["mode"] != "live"
                or source["account_id"] not in json.loads(task["account_ids"])
            ):
                raise ValueError("plan_task_binding_missing")
            if task["state"] not in {"draft", "active"} or (
                task["ends_at"] and epoch(task["ends_at"]) <= now
            ):
                continue
            if matches and not any(r["task_id"] == bound_task for r in matches):
                raise ValueError("plan_task_binding_mismatch")
        if matches:
            if any(
                r["status"] != "not_attempted"
                or r["attempt_id"]
                or r["attempted"]
                or r["task_state"] not in {"draft", "active"}
                for r in matches
            ):
                continue
            if any(r["ends_at"] and epoch(r["ends_at"]) <= now for r in matches):
                continue
            if any(
                r["queued"] and r["task_state"] == "active" and r["phase"] == "ready"
                for r in matches
            ):
                continue  # Already included in the native queue count.
        count += 1
    return count, "active"


def merge_plan_queues(db, summary, sources, root, now):
    if not isinstance(sources, list) or len(sources) > 50:
        raise ValueError("planned_queue_registry_invalid")
    platforms = {p["platform"]: p for p in summary["platforms"]}
    for p in platforms.values():
        p["queued_native"], p["queued_planned"] = p["queued"], 0
    seen, diagnostics = set(), []
    for source in sources:
        validate_source(source)
        p = platforms[source["platform"]]
        try:
            extra, state = pending_count(db, source, Path(root).resolve(), now, seen)
        except Exception:
            # An unreadable registered plan is unknown, never a false zero.
            p["queued"] = p["queued_planned"] = None
            diagnostics.append(
                {
                    "source": source["id"],
                    "platform": "tiktok",
                    "state": "unavailable",
                    "reason": "planned_queue_unavailable",
                }
            )
            continue
        if p["queued"] is not None:
            p["queued"] += extra
            p["queued_planned"] += extra
        diagnostics.append(
            {"source": source["id"], "platform": "tiktok", "state": state, "queued": extra}
        )
    summary["queue_sources"] = diagnostics
    return summary
