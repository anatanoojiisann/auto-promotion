"""Idempotent projections of explicitly registered legacy workflows.

Sources stay read-only. Historical items are never queued, and missing evidence
never erases an earlier success or uncertain submission.
"""

import json
import random
from datetime import datetime, timezone
from pathlib import Path

from .backends.common import BackendError, read_profile, save_profile
from .completion_sources import external_evidence, timestamp
from .meta_browser import event_key
from .models import AccountInput, ItemInput, Policy, canonical, digest
from .statistics import publication_time, record_publication
from .store import Store


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


def register(browser, registrations):
    """Enroll only the user's selected accounts and existing browser identities."""
    result = []
    for data in registrations:
        platform = data["platform"]
        if platform not in {"facebook", "instagram"}:
            continue
        external_id = data["account_id"]
        details = data["details"]
        connector = details["connector"]
        family = {"edge_browser_use": "edge", "chrome_browser_use": "chrome", "tabbit": "tabbit"}[
            connector
        ]
        identity = external_id.split(":", 1)[1]
        label = data["label"]
        backend_label = "meta-desktop-" + identity
        existing = browser.store.one(
            "SELECT account_id FROM meta_browser_accounts WHERE external_id=?", (external_id,)
        )
        account = browser.engine.account(existing["account_id"]) if existing else None
        # Keep prior stable-identity registrations instead of creating a second account.
        if not account:
            current = browser.store.one(
                "SELECT id FROM accounts WHERE platform=? AND mode='live' "
                "AND (identity_hash=? OR backend_label=?)",
                (platform, digest([platform, identity]), backend_label),
            )
            account = (
                browser.engine.account(current["id"])
                if current
                else browser.engine.add_account(
                    AccountInput(
                        platform=platform,
                        label=label,
                        backend_label=backend_label,
                        daily_limit=5,
                        hourly_limit=1,
                        min_interval=1800,
                    )
                )
            )
        profile = {
            "identity": identity,
            "connection_type": "meta_desktop",
            "browser_channel": "tabbit" if family == "tabbit" else "browser_use",
            "browser_family": family,
            "automatic_actions": ["comment", "reply"],
        }
        # Only metadata is saved. There are no cookies, tokens or credentials here.
        accounts_dir = Path(browser.store.path).parent / "accounts"
        try:
            previous_profile = read_profile(
                platform, account["backend_label"], accounts_dir=accounts_dir
            )
        except BackendError:
            previous_profile = {}
        if previous_profile and previous_profile.get("connection_type") != "meta_desktop":
            raise ValueError("已有平台接口连接不会被浏览器连接覆盖")
        if any(previous_profile.get(k) != v for k, v in profile.items()):
            save_profile(
                platform,
                account["backend_label"],
                {**previous_profile, **profile},
                accounts_dir=accounts_dir,
            )
        with browser.store.connect(write=True) as db:
            previous = db.execute(
                "SELECT details FROM meta_browser_accounts WHERE account_id=?", (account["id"],)
            ).fetchone()
            saved = json.loads(previous[0]) if previous else {}
            saved.update(details)
            saved.setdefault("minimum_interval", 1800)
            saved.setdefault("maximum_interval", 4800 if platform == "instagram" else 4200)
            db.execute(
                "INSERT INTO meta_browser_accounts VALUES(?,?,?,?,?,?,?,'{}',?) "
                "ON CONFLICT(account_id) DO UPDATE SET details=excluded.details,updated_at=excluded.updated_at",
                (
                    account["id"],
                    external_id,
                    identity,
                    details["profile_url"],
                    connector,
                    family,
                    canonical(saved),
                    browser.clock(),
                ),
            )
            Store.audit(
                db,
                "meta_browser.enrolled",
                {
                    "account_id": account["id"],
                    "external_id": external_id,
                    "connector": connector,
                },
                now=browser.clock(),
            )
        result.append(account["id"])
    browser.sync()
    return result


def import_existing(browser):
    policy = Path(browser.store.path).parent / "completion-cycle-policy.json"
    data = json.loads(policy.read_text(encoding="utf-8-sig"))
    registrations = data.get("external_accounts", [])
    for registration in registrations:
        if registration.get("platform") != "instagram":
            continue
        details = registration["details"]
        primary_paths = {str(Path(p).resolve()) for p in details.get("workflow_paths", [])}
        linked = {
            str(Path(p).resolve()) for p in details.get("coordination_paths", [])
        } - primary_paths
        for raw in details.get("workflow_paths", []):
            source = Path(raw)
            try:
                state = json.loads(source.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            root = next((p for p in source.parents if p.name.startswith("g-p-")), None)
            if root:
                for relative in state.get("linked_account_state_paths", []):
                    candidate = (root / relative).resolve()
                    if candidate.is_relative_to(root) and str(candidate) not in primary_paths:
                        linked.add(str(candidate))
        details["coordination_paths"] = sorted(linked)
    return {"account_ids": register(browser, registrations), "execution_started": False}


def legacy_items(data, label):
    rows = data.get("items", [])
    rows = list(rows.values()) if isinstance(rows, dict) else rows
    # Shared IG state contains older comments and finite replies as well.
    rows = [*rows, *data.get("historical_comments", []), *data.get("replies", [])]
    return [
        r
        for r in rows
        if isinstance(r, dict) and r.get("account", data.get("account", label)) == label
    ]


def project_item(row):
    target = (
        row.get("target_url")
        or (row.get("comment_url") if row.get("reply_url") else None)
        or row.get("post_url")
        or row.get("url")
        or row.get("comment_url", "")
    )
    reference = row.get("reply_url") or row.get("published_url") or row.get("comment_url", "")
    text = row.get("text") or row.get("final_text") or row.get("draft") or row.get("reply_text", "")
    status = row.get("status", "unknown")
    public = row.get("verified") is True or bool(
        row.get("public_verified_at") and row.get("publication_evidence")
    )
    attempted = bool(
        any(
            a.get("status") in {"sent", "submitting", "sending", "unknown"}
            for a in row.get("attempts", [])
            if isinstance(a, dict)
        )
        or row.get("sending_started_at")
        or row.get("sent_at")
        or status in {"sent", "sending", "submitting", "unknown", "public_verified"}
    )
    if status in {"sent", "confirmed", "public_verified"}:
        normalized = "confirmed" if public and reference else "manual_attention"
    elif status in {"sending", "submitting", "unknown", "pending_review", "scheduled"}:
        normalized = "unknown"
    elif status == "failed_known":
        normalized = "failed_known"
    else:
        # Never place unattempted historical content in a runnable state.
        normalized = "unknown" if attempted else "cancelled"
    return target, reference, text, normalized, attempted


def synchronize(browser):
    updated = 0
    now = browser.clock()
    for connection in browser.store.rows("SELECT * FROM meta_browser_accounts"):
        details = json.loads(connection["details"])
        account = browser.store.one(
            "SELECT * FROM accounts WHERE id=?", (connection["account_id"],)
        )
        paths = [(p, False) for p in details.get("workflow_paths", [])]
        paths += [
            (p, True)
            for p in details.get("coordination_paths", [])
            if p not in details.get("workflow_paths", [])
        ]
        for raw, coordination in paths:
            path = Path(raw).resolve()
            source_id = "meta-source-" + digest([account["id"], str(path)])[:24]
            existing = browser.store.one(
                "SELECT * FROM meta_browser_sources WHERE id=?", (source_id,)
            )
            try:
                content = path.read_text(encoding="utf-8-sig")
                data = json.loads(content)
                if not isinstance(data, dict):
                    raise ValueError
                fingerprint = digest(data)
            except (OSError, ValueError, TypeError):
                if existing:
                    with browser.store.connect(write=True) as db:
                        db.execute(
                            "UPDATE meta_browser_sources SET state='unknown',updated_at=? WHERE id=?",
                            (now, source_id),
                        )
                else:
                    _archive(
                        browser, connection, account, source_id, path, {}, [], now, "unknown", ""
                    )
                continue
            state, _ = external_evidence(
                data, account_label=details.get("scope", account["label"]), now=now
            )
            if coordination and state != "unknown":
                # This file provides shared pacing/history. Its original monitor
                # remains visible in details rather than becoming a new send task.
                state = "complete"
            if existing and existing["fingerprint"] == fingerprint and existing["state"] == state:
                continue
            rows = legacy_items(data, details.get("scope", account["label"]))
            _archive(
                browser, connection, account, source_id, path, data, rows, now, state, fingerprint
            )
            updated += 1
    return {"updated_sources": updated}


def _archive(browser, connection, account, source_id, path, data, rows, now, state, fingerprint):
    sources = browser.store
    prepared = []
    seen = set()
    for index, row in enumerate(rows):
        target, reference, text, status, attempted = project_item(row)
        external = str(row.get("id", row.get("plan_id", ""))) or digest([target, text])[:24]
        if external in seen:
            continue
        seen.add(external)
        try:
            key = event_key(account["platform"], target)
        except (ValueError, TypeError):
            # Missing target records still block their source through its unknown state.
            continue
        due = timestamp(row.get("not_before") or row.get("scheduled_at") or row.get("sent_at")) or (
            timestamp(data.get("created_at")) or now
        )
        payload = {
            **ItemInput(
                account_id=account["id"],
                action="reply" if "/c/" in target or "comment_id=" in target else "comment",
                target=target,
                text=text,
                scheduled_at=iso(due),
            ).model_dump(),
            "platform": account["platform"],
            "account_label": account["label"],
        }
        prepared.append((external, payload, due, reference, status, attempted, key, row))
    snapshot = [
        {
            k: account[k]
            for k in (
                "id",
                "platform",
                "label",
                "backend_label",
                "mode",
                "daily_limit",
                "hourly_limit",
                "min_interval",
            )
        }
    ]
    start = min([p[2] for p in prepared] or [timestamp(data.get("created_at")) or now])
    end = max(start + 1, max([p[2] for p in prepared] or [start]) + 86400)
    policy = Policy(
        starts_at=iso(start),
        ends_at=iso(end),
        execution_channel="local",
        min_interval=account["min_interval"],
    ).model_dump()
    new_items = [
        p
        for p in prepared
        if not sources.one(
            "SELECT 1 FROM meta_browser_history WHERE source_id=? AND external_id=?",
            (source_id, p[0]),
        )
    ]
    task_id = "AP-META-" + digest([source_id, [p[0] for p in new_items]])[:14].upper()
    source = sources.one("SELECT * FROM meta_browser_sources WHERE id=?", (source_id,))
    if not new_items and source:
        task_id = source["task_id"]
    legacy_status = data.get("status", "")
    stopped = legacy_status in {
        "stopped",
        "stopped_by_user",
        "user_stopped",
        "cancelled",
        "canceled",
    }
    task_state = "cancelled" if stopped else "completed" if state == "complete" else "paused"
    with sources.connect(write=True) as db:
        if not source or new_items:
            payloads = [{**p[1], "position": pos} for pos, p in enumerate(new_items)]
            manifest = {
                "task_id": task_id,
                "accounts": snapshot,
                "policy": policy,
                "items": payloads,
            }
            db.execute(
                "INSERT OR IGNORE INTO tasks(id,name,mode,account_ids,account_snapshot,policy,policy_hash,"
                "state,phase,manifest,manifest_hash,created_at,updated_at) VALUES(?,?,'live',?,?,?,?,?,'ready',?,?,?,?)",
                (
                    task_id,
                    "历史接入 · "
                    + str(data.get("name") or data.get("campaign") or path.parent.name)[:100]
                    + " · "
                    + account["label"],
                    canonical([account["id"]]),
                    canonical(snapshot),
                    canonical(policy),
                    digest({"accounts": snapshot, "policy": policy}),
                    task_state,
                    canonical(manifest),
                    digest(manifest),
                    timestamp(data.get("created_at")) or start,
                    now,
                ),
            )
        main_task = (
            source["task_id"]
            if source
            and db.execute(
                "SELECT 1 FROM meta_browser_history WHERE source_id=? LIMIT 1", (source_id,)
            ).fetchone()
            else task_id
        )
        db.execute(
            "INSERT INTO meta_browser_sources VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "task_id=excluded.task_id,fingerprint=excluded.fingerprint,state=excluded.state,details=excluded.details,updated_at=excluded.updated_at",
            (
                source_id,
                account["id"],
                str(path),
                main_task,
                fingerprint,
                state,
                canonical(
                    {
                        "legacy_status": data.get("status", "unknown"),
                        "legacy_phase": data.get("phase"),
                        "automation_id": data.get("automation_id"),
                        "ownership": "legacy",
                        "read_only": True,
                        "native_item_id": (data.get("coordination_inflight") or {}).get("item_id")
                        if (data.get("coordination_inflight") or {}).get("source")
                        == "auto_promotion"
                        else None,
                    }
                ),
                now,
            ),
        )
        for external, payload, due, reference, status, attempted, key, row in prepared:
            prior = db.execute(
                "SELECT h.item_id,i.status,i.result FROM meta_browser_history h JOIN items i ON i.id=h.item_id "
                "WHERE h.source_id=? AND h.external_id=?",
                (source_id, external),
            ).fetchone()
            item_id = (
                prior["item_id"] if prior else "meta-item-" + digest([source_id, external])[:24]
            )
            # Definite success and unresolved attempts cannot regress after a file rollback.
            if prior and prior["status"] == "confirmed":
                status = "confirmed"
            elif prior and prior["status"] == "unknown" and status not in {"confirmed", "unknown"}:
                status = "unknown"
            result = {
                "status": status,
                "verified": status == "confirmed",
                "reason": "legacy_read_only_projection",
                "legacy_status": row.get("status"),
                "legacy_followup_status": row.get("followup_status"),
                "external_reference": reference or None,
            }
            # Only explicit platform publication timestamps enter publication-time statistics.
            published = timestamp(row.get("published_at"))
            if publication_time(published, now) is not None:
                result["published_at"] = published
            if prior:
                old_result = json.loads(prior["result"])
                if prior["status"] == "confirmed":
                    result = {**result, **old_result}
                db.execute(
                    "UPDATE items SET status=?,external_reference=COALESCE(?,external_reference),result=? WHERE id=?",
                    (status, reference or None, canonical(result), item_id),
                )
            else:
                position = next(i for i, p in enumerate(new_items) if p[0] == external)
                payload["position"] = position
                db.execute(
                    "INSERT INTO items(id,task_id,account_id,position,payload,payload_hash,due_at,status,"
                    "external_reference,result) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        item_id,
                        task_id,
                        account["id"],
                        position,
                        canonical(payload),
                        digest(payload),
                        due,
                        status,
                        reference or None,
                        canonical(result),
                    ),
                )
            db.execute(
                "INSERT INTO meta_browser_history VALUES(?,?,?,?) ON CONFLICT(source_id,external_id) "
                "DO UPDATE SET details=excluded.details",
                (
                    source_id,
                    external,
                    item_id,
                    canonical(
                        {
                            "original_status": row.get("status"),
                            "followup_status": row.get("followup_status"),
                            "source": str(path),
                            "source_item_id": external,
                            "attempted": attempted,
                        }
                    ),
                ),
            )
            if status in {"confirmed", "unknown"} or attempted:
                db.execute(
                    "INSERT OR IGNORE INTO meta_browser_contacts VALUES(?,?,?,?,?)",
                    (account["platform"], key, item_id, account["id"], status),
                )
            if status == "confirmed":
                verified_at = timestamp(row.get("public_verified_at") or row.get("verified_at"))
                # Historical evidence must not generate a fresh success broadcast at migration time.
                verified_at = min(verified_at or timestamp(row.get("sent_at")) or start, now)
                record_publication(db, item_id, result, verified_at)
        old_tasks = db.execute(
            "SELECT DISTINCT i.task_id FROM meta_browser_history h JOIN items i ON i.id=h.item_id "
            "WHERE h.source_id=?",
            (source_id,),
        ).fetchall()
        for old_task in old_tasks:
            db.execute("UPDATE tasks SET state=? WHERE id=?", (task_state, old_task[0]))
        next_at = max(
            [
                timestamp(data.get(k)) or 0
                for k in ("next_not_before", "random_interval_until", "cooldown_until")
            ]
        )
        if next_at:
            db.execute(
                "UPDATE accounts SET next_at=max(next_at,?) WHERE id=?", (next_at, account["id"])
            )
        pacing = data.get("pacing", {})
        minimum = pacing.get("min_minutes_between_writes")
        if isinstance(minimum, (int, float)) and 0 < minimum <= 1440:
            info = json.loads(connection["details"])
            info["minimum_interval"] = max(info.get("minimum_interval", 1800), int(minimum * 60))
            info["maximum_interval"] = max(
                info.get("maximum_interval", 4800), info["minimum_interval"]
            )
            db.execute(
                "UPDATE meta_browser_accounts SET details=? WHERE account_id=?",
                (canonical(info), account["id"]),
            )
            db.execute(
                "UPDATE accounts SET min_interval=max(min_interval,?) WHERE id=?",
                (int(minimum * 60), account["id"]),
            )
        Store.audit(
            db,
            "meta_browser.source_synced",
            {
                "source_id": source_id,
                "state": state,
                "items": len(prepared),
                "fingerprint": fingerprint,
                "read_only": True,
            },
            task_id,
            now,
        )


def sample_next(browser, account_id, now):
    connection = browser.connection(account_id)
    settings = connection["details"]
    gap = random.SystemRandom().randint(settings["minimum_interval"], settings["maximum_interval"])
    with browser.store.connect(write=True) as db:
        planned = db.execute(
            "SELECT MIN(i.due_at) FROM items i JOIN tasks t ON t.id=i.task_id "
            "WHERE i.account_id=? AND i.status='not_attempted' AND t.state='active' "
            "AND i.due_at>=? AND i.due_at<=?",
            (
                account_id,
                now + settings["minimum_interval"] - 900,
                now + settings["maximum_interval"],
            ),
        ).fetchone()[0]
        if planned is not None:
            # An explicit frozen schedule is authoritative; any small execution
            # delay still respects the real account minimum and the normal grace.
            gap = max(settings["minimum_interval"], planned - now)
        db.execute("UPDATE accounts SET next_at=max(next_at,?) WHERE id=?", (now + gap, account_id))
    return now + gap
