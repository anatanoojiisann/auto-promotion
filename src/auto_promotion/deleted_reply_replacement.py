"""One explicitly requested correction after an exact garbled X reply was deleted.

Never resets old attempts or outcomes. All other history and normal send gates remain.
"""

import hashlib
import json
import math
import re
import time

from .models import digest, epoch
from .store import Store
from .text_integrity import validate_outbound_payload

KIND = "interaction.deleted_reply_replacement_authorized"


def _old_binding(item):
    return {
        k: item[k]
        for k in ("id", "task_id", "account_id", "payload_hash", "attempt_id", "external_reference")
    }


def _scope(db, old_id, new_id):
    old = db.execute("SELECT * FROM items WHERE id=?", (old_id,)).fetchone()
    new = db.execute("SELECT * FROM items WHERE id=?", (new_id,)).fetchone()
    if not old or not new or old_id == new_id or old["account_id"] != new["account_id"]:
        raise ValueError("DeletedReplyReplacementAccountMismatch")
    account = db.execute("SELECT * FROM accounts WHERE id=?", (new["account_id"],)).fetchone()
    tasks = [
        db.execute("SELECT * FROM tasks WHERE id=?", (i["task_id"],)).fetchone() for i in (old, new)
    ]
    original, corrected = [json.loads(i["payload"]) for i in (old, new)]
    if (
        not account
        or account["platform"] != "x"
        or tasks[0]["state"] not in {"completed", "paused", "cancelled"}
        or old["status"] not in {"unknown", "confirmed"}
        or not old["attempt_id"]
        or tasks[0]["mode"] != tasks[1]["mode"]
        or json.loads(tasks[1]["account_ids"]) != [new["account_id"]]
        or db.execute("SELECT count(*) FROM items WHERE task_id=?", (new["task_id"],)).fetchone()[0]
        != 1
        or json.loads(tasks[0]["policy"]).get("project_id", "default")
        != json.loads(tasks[1]["policy"]).get("project_id", "default")
        or any(digest(p) != i["payload_hash"] for p, i in zip((original, corrected), (old, new)))
        or original.get("action") != "reply"
        or corrected.get("action") != "reply"
        or corrected.get("execution_mode") != "automatic"
        or original["target"] != corrected["target"]
        or "\ufffd" not in original.get("text", "")
        or corrected.get("text") == original.get("text")
        or not corrected.get("text", "").strip()
        or corrected.get("media")
    ):
        raise ValueError("DeletedReplyReplacementScopeInvalid")
    validate_outbound_payload(corrected)
    match = re.fullmatch(
        r"https://x\.com/([A-Za-z0-9_]+)/status/([0-9]+)", old["external_reference"] or ""
    )
    if not match or not re.fullmatch(
        r"https://x\.com/[A-Za-z0-9_]+/status/[0-9]+", original["target"]
    ):
        raise ValueError("DeletedReplyReplacementReferenceMissing")
    return old, new, tasks[1], account, corrected, match


def replacement_proof(db, item_id, now=None, *, allow_expired=False):
    now = time.time() if now is None else now
    marker = db.execute(
        "SELECT details FROM audit WHERE kind=? AND json_extract(details,'$.new_item_id')=? ORDER BY id DESC LIMIT 1",
        (KIND, item_id),
    ).fetchone()
    if not marker:
        return None
    try:
        proof = json.loads(marker[0])
        latest = db.execute(
            "SELECT details FROM audit WHERE kind=? AND json_extract(details,'$.old_item_id')=? ORDER BY id DESC LIMIT 1",
            (KIND, proof["old_item_id"]),
        ).fetchone()
        if not latest or json.loads(latest[0])["new_item_id"] != item_id:
            return None
        old, new, task, account, payload, _ = _scope(db, proof["old_item_id"], item_id)
        if (
            proof["old_binding"] != _old_binding(old)
            or proof["new_payload_hash"] != new["payload_hash"]
            or proof["account_identity_hash"] != account["identity_hash"]
            or proof["new_task_id"] != task["id"]
            or (proof["until"] <= now and not allow_expired)
            or digest(proof["deletion_evidence"]) != proof["deletion_evidence_hash"]
            or any(
                not _negative_receipt_matches(db, p) for p in proof.get("prior_no_submit_items", [])
            )
        ):
            return None
        return proof
    except (KeyError, TypeError, ValueError):
        return None


def validate_replacement_claim(db, item_id, now):
    exists = db.execute(
        "SELECT 1 FROM audit WHERE kind=? AND json_extract(details,'$.new_item_id')=? LIMIT 1",
        (KIND, item_id),
    ).fetchone()
    if exists and not replacement_proof(db, item_id, now):
        raise ValueError("DeletedReplyReplacementProofChangedOrExpired")


def authorize_replacement(engine, old_item_id, new_item_id, *, source, deletion_evidence):
    if not isinstance(source, str) or not source.strip() or len(source) > 500:
        raise ValueError("ExplicitDeletedReplyReplacementInstructionRequired")
    now = engine.clock()
    with engine.store.connect(write=True) as db:
        prior = db.execute(
            "SELECT details FROM audit WHERE kind=? AND json_extract(details,'$.old_item_id')=? LIMIT 1",
            (KIND, old_item_id),
        ).fetchone()
        if prior:
            if json.loads(prior[0])["new_item_id"] == new_item_id and replacement_proof(
                db, new_item_id, now
            ):
                return {"new_item_id": new_item_id, "unchanged": True}
            raise ValueError("DeletedReplyReplacementAlreadyUsed")
        old, new, task, account, payload, match = _scope(db, old_item_id, new_item_id)
        if (
            task["state"] != "draft"
            or task["manifest_hash"]
            or new["status"] != "not_attempted"
            or new["attempt_id"]
            or new["external_reference"]
            or db.execute("SELECT 1 FROM attempts WHERE item_id=?", (new_item_id,)).fetchone()
        ):
            raise ValueError("DeletedReplyReplacementRequiresUnattemptedDraft")
        evidence = dict(deletion_evidence)
        stamp = evidence.get("observed_at")
        if (
            type(stamp) not in {int, float}
            or not math.isfinite(stamp)
            or not 0 <= now - stamp <= 900
            or evidence.get("verified") is not True
            or evidence.get("delete_response_accepted") is not True
            or evidence.get("reload_deleted") is not True
            or evidence.get("deleted_post_id") != match[2]
            or str(evidence.get("account_handle", "")).lower() != match[1].lower()
            or evidence.get("parent_target") != payload["target"]
            or evidence.get("new_text_sha256")
            != hashlib.sha256(payload["text"].encode("utf-8")).hexdigest()
        ):
            raise ValueError("DeletedReplyReplacementDeletionUnverified")
        from .interaction_intents import event_id, scope_id

        key = (
            scope_id(db, json.loads(task["policy"]).get("project_id", "default")),
            "x",
            event_id("x", payload),
            "reply",
        )
        intent = db.execute(
            "SELECT item_id,state FROM interaction_intents WHERE project_id=? AND platform=? AND source_event_id=? AND action_kind=?",
            key,
        ).fetchone()
        if not intent or intent["item_id"] != old_item_id:
            raise ValueError("DeletedReplyReplacementIntentChanged")
        proof = {
            "old_item_id": old_item_id,
            "new_item_id": new_item_id,
            "old_binding": _old_binding(old),
            "new_task_id": task["id"],
            "new_payload_hash": new["payload_hash"],
            "account_identity_hash": account["identity_hash"],
            "source": source.strip(),
            "deletion_evidence": evidence,
            "deletion_evidence_hash": digest(evidence),
            "authorized_at": now,
            "until": min(epoch(json.loads(task["policy"])["ends_at"]), now + 1800),
            "old_attempts_reset": False,
            "old_outcome_changed": False,
        }
        if proof["until"] <= now:
            raise ValueError("DeletedReplyReplacementWindowExpired")
        db.execute(
            "UPDATE interaction_intents SET item_id=?,state='reserved' WHERE project_id=? AND platform=? AND source_event_id=? AND action_kind=? AND item_id=?",
            (new_item_id, *key, old_item_id),
        )
        Store.audit(db, KIND, proof, task["id"], now)
    return {
        "old_item_id": old_item_id,
        "new_item_id": new_item_id,
        "until": proof["until"],
        "old_outcome_changed": False,
    }


def _negative_receipt_matches(db, saved):
    from .final_receipts import proof as final_proof

    item = db.execute("SELECT * FROM items WHERE id=?", (saved["item_id"],)).fetchone()
    if not item or item["status"] != "failed_known" or item["external_reference"]:
        return False
    result = json.loads(item["result"])
    if (
        item["attempt_id"] != saved["attempt_id"]
        or item["payload_hash"] != saved["payload_hash"]
        or digest(result) != saved["result_digest"]
        or result.get("submission_started") is not False
        or result.get("reason") != "TabbitPreparationFailedBeforeSubmission"
        or result.get("execution_step") != "reply_composer"
    ):
        return False
    job = db.execute(
        "SELECT * FROM execution_jobs WHERE kind='send' AND reference_id=?", (item["id"],)
    ).fetchone()
    attempt = db.execute(
        "SELECT * FROM attempts WHERE id=? AND item_id=?", (item["attempt_id"], item["id"])
    ).fetchone()
    if (
        not job
        or job["state"] != "failed_known"
        or job["owner"]
        or job["lease_until"] > time.time()
        or not attempt
        or attempt["outcome"] != "failed_known"
        or not attempt["finished_at"]
        or db.execute("SELECT 1 FROM cloud_receipt_outbox WHERE job_id=?", (job["id"],)).fetchone()
    ):
        return False
    database = db.execute("PRAGMA database_list").fetchone()[2]
    receipt = final_proof(Store(database, initialize=False), dict(item))
    return bool(receipt and receipt[2] == result)


def authorize_no_submit_retry(engine, previous_item_id, new_item_id, *, source):
    """Continue the same correction only after an immutable native final no-submit receipt."""
    if not isinstance(source, str) or not source.strip() or len(source) > 500:
        raise ValueError("ExplicitDeletedReplyReplacementInstructionRequired")
    now = engine.clock()
    with engine.store.connect(write=True) as db:
        # A new explicitly requested draft may follow an expired, completed
        # preparation only when its immutable final receipt proves no submit.
        # Old frozen tasks and their claim deadlines are never revived.
        prior = replacement_proof(db, previous_item_id, now, allow_expired=True)
        if not prior or len(prior.get("prior_no_submit_items", [])) >= 4:
            raise ValueError("DeletedReplyReplacementRetryScopeInvalid")
        old, new, task, account, payload, _ = _scope(db, prior["old_item_id"], new_item_id)
        previous = db.execute("SELECT * FROM items WHERE id=?", (previous_item_id,)).fetchone()
        negative = {
            "item_id": previous_item_id,
            "attempt_id": previous["attempt_id"],
            "payload_hash": previous["payload_hash"],
            "result_digest": digest(json.loads(previous["result"])),
        }
        ignored = {"scheduled_at", "position", "platform", "account_label"}
        if (
            not _negative_receipt_matches(db, negative)
            or task["state"] != "draft"
            or task["manifest_hash"]
            or new["status"] != "not_attempted"
            or new["attempt_id"]
            or new["external_reference"]
            or {k: v for k, v in payload.items() if k not in ignored}
            != {k: v for k, v in json.loads(previous["payload"]).items() if k not in ignored}
        ):
            raise ValueError("DeletedReplyReplacementNoSubmitProofMissing")
        from .interaction_intents import event_id, scope_id

        key = (
            scope_id(db, json.loads(task["policy"]).get("project_id", "default")),
            "x",
            event_id("x", payload),
            "reply",
        )
        changed = db.execute(
            "UPDATE interaction_intents SET item_id=?,state='reserved' WHERE project_id=? AND platform=? AND source_event_id=? AND action_kind=? AND item_id=? AND state='claimed'",
            (new_item_id, *key, previous_item_id),
        ).rowcount
        if changed != 1:
            raise ValueError("DeletedReplyReplacementIntentChanged")
        grant = {
            **prior,
            "new_item_id": new_item_id,
            "new_task_id": task["id"],
            "new_payload_hash": new["payload_hash"],
            "source": source.strip(),
            "authorized_at": now,
            "until": min(
                epoch(json.loads(task["policy"])["ends_at"]),
                max(now, epoch(payload["scheduled_at"])) + 900,
            ),
            "prior_no_submit_items": [*prior.get("prior_no_submit_items", []), negative],
        }
        Store.audit(db, KIND, grant, task["id"], now)
    return {"new_item_id": new_item_id, "old_outcome_changed": False, "prior_attempt_reset": False}
