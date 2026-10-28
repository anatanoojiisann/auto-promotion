"""Resolve native video URLs from bounded, account-scoped local evidence only."""

import json
import re
from urllib.parse import urlparse

from .backends.common import BackendError
from .backends.tiktok_desktop import video_target
from .models import digest, epoch


def resolve_video_target(
    engine,
    account,
    video_id,
    *,
    task_id="",
    opportunity_id="",
    action="comment",
    parent_comment_id="",
):
    # Import lazily: core calls this during draft preparation, never during send.
    from .core import Conflict

    if account["platform"] != "tiktok" or not re.fullmatch(r"[1-9][0-9]{0,29}", video_id):
        raise Conflict("TikTokVideoRequired")
    records = []
    if task_id:
        task = engine.task(task_id)
        if account["id"] not in task["account_ids"]:
            raise Conflict("TikTokVideoEvidenceAccountMismatch")
        rows = engine.store.rows(
            "SELECT * FROM candidates WHERE task_id=? AND account_id=? AND decision='qualified' ORDER BY id LIMIT 2001",
            (task_id, account["id"]),
        )
        if len(rows) > 2000:
            raise Conflict("TikTokVideoEvidenceBudgetExceeded")
        for row in rows:
            match = re.fullmatch(
                r"/@[A-Za-z0-9_.]+/(?:video|photo)/([1-9][0-9]{0,29})/?",
                urlparse(row["target"]).path,
            )
            if match and match[1] == video_id:
                records.append((row["target"], "task_candidate", row["id"], candidate_hash(row)))
    rows = engine.store.rows(
        "SELECT id,payload FROM opportunities WHERE platform='tiktok' "
        "AND json_extract(payload,'$.tiktok_video_id')=? "
        "AND EXISTS(SELECT 1 FROM json_each(json_extract(payload,'$.permission_account_ids')) WHERE value=?) "
        "ORDER BY id LIMIT 201",
        (video_id, account["id"]),
    )
    if len(rows) > 200:
        raise Conflict("TikTokVideoEvidenceBudgetExceeded")
    for row in rows:
        value = json.loads(row["payload"])
        if opportunity_id and row["id"] != opportunity_id:
            continue
        if (
            value.get("permission") != "allowed"
            or not value.get("permission_evidence", "").strip()
            or not value.get("is_public")
            or value.get("action") != action
            or (action == "reply" and value.get("tiktok_comment_id") != parent_comment_id)
            or value.get("expires_at")
            and epoch(value["expires_at"]) <= engine.clock()
        ):
            continue
        records.append((value["target"], "account_opportunity", row["id"], digest(value)))
    if opportunity_id:
        records = [r for r in records if r[1] == "account_opportunity" and r[2] == opportunity_id]
    resolved = {}
    for target, kind, identifier, source_hash in records:
        try:
            canonical = video_target(target, video_id)
        except (BackendError, ValueError):
            raise Conflict("TikTokVideoEvidenceInvalid") from None
        resolved.setdefault(canonical, []).append((target, kind, identifier, source_hash))
    if not resolved:
        raise Conflict("TikTokVideoEvidenceRequired：先登记此账号的原生视频候选链接")
    if len(resolved) != 1:
        raise Conflict("TikTokVideoEvidenceConflict：同一视频ID有冲突原生链接")
    target, matches = next(iter(resolved.items()))
    # Explicit opportunity selection retains its exact immutable content-key URL.
    chosen = matches[0]
    if chosen[1] == "account_opportunity":
        target = chosen[0]
    return {
        "target_evidence": {"kind": chosen[1], "source_id": chosen[2], "source_hash": chosen[3]},
        "target": target,
        "video_id": video_id,
        "evidence_sources": [
            {"kind": kind, "id": identifier} for _, kind, identifier, _ in matches
        ],
        "platform_probe_performed": False,
    }


def candidate_hash(row):
    return digest(
        {key: row[key] for key in ("task_id", "account_id", "target", "payload", "decision")}
    )


def validate_target_evidence(db, account, item, task_id, now):
    """Recheck the frozen source at preparation, claim and final native authorization."""
    evidence = item.get("tiktok_target_evidence")
    if not evidence:
        return
    if account["platform"] != "tiktok":
        raise BackendError("TikTokVideoEvidenceAccountMismatch")
    kind = evidence.get("kind")
    if kind == "task_candidate":
        row = db.execute(
            "SELECT * FROM candidates WHERE id=?", (evidence.get("source_id"),)
        ).fetchone()
        if (
            not row
            or row["task_id"] != task_id
            or row["account_id"] != account["id"]
            or row["decision"] != "qualified"
        ):
            raise BackendError("TikTokVideoEvidenceRevoked")
        target, source_hash = row["target"], candidate_hash(row)
    elif kind == "account_opportunity":
        row = db.execute(
            "SELECT platform,payload FROM opportunities WHERE id=?", (evidence.get("source_id"),)
        ).fetchone()
        if not row or row["platform"] != "tiktok":
            raise BackendError("TikTokVideoEvidenceRevoked")
        value = json.loads(row["payload"])
        if (
            account["id"] not in value.get("permission_account_ids", [])
            or value.get("permission") != "allowed"
            or not value.get("permission_evidence", "").strip()
            or not value.get("is_public")
            or value.get("action") != item["action"]
            or value.get("tiktok_video_id") != item.get("tiktok_video_id")
            or (
                item["action"] == "reply"
                and value.get("tiktok_comment_id") != item.get("tiktok_comment_id")
            )
            or (value.get("expires_at") and epoch(value["expires_at"]) <= now)
        ):
            raise BackendError("TikTokVideoEvidenceRevoked")
        target, source_hash = value["target"], digest(value)
    else:
        raise BackendError("TikTokVideoEvidenceInvalid")
    if source_hash != evidence.get("source_hash"):
        raise BackendError("TikTokVideoEvidenceChanged")
    if video_target(target, item.get("tiktok_video_id", "")) != video_target(
        item["target"], item.get("tiktok_video_id", "")
    ):
        raise BackendError("TikTokVideoEvidenceInvalid")
