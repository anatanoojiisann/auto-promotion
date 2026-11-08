"""Explicit, content-identical recovery with no changes to the original outcome or quota."""

import json

from .models import digest


def evidence(db, old_id, new_id):
    old = db.execute("SELECT i.*,t.state task_state FROM items i JOIN tasks t ON t.id=i.task_id WHERE i.id=?", (old_id,)).fetchone()
    new = db.execute("SELECT i.*,t.state task_state,t.policy FROM items i JOIN tasks t ON t.id=i.task_id WHERE i.id=?", (new_id,)).fetchone()
    if not old or not new or old["task_state"] != "completed" or new["status"] != "not_attempted" or new["attempt_id"]:
        raise ValueError("PhoneRetryRequiresSettledOriginalAndUnattemptedReplacement")
    result = json.loads(old["result"])
    if (old["status"] != "failed_known" or old["external_reference"] or result.get("verified") is not False
            or result.get("confirmed_not_submitted") is not True or result.get("submission_started") is not False):
        raise ValueError("PhoneRetryRequiresProvenNoSubmission")
    ignored = {"scheduled_at", "position", "account_label", "platform"}
    previous, replacement = json.loads(old["payload"]), json.loads(new["payload"])
    if (old["account_id"] != new["account_id"] or previous.get("platform") != "telegram"
            or {k:v for k,v in previous.items() if k not in ignored} != {k:v for k,v in replacement.items() if k not in ignored}
            or json.loads(new["policy"]).get("execution_channel") != "local"
            or json.loads(new["policy"]).get("execution_method") != "phone"):
        raise ValueError("PhoneRetryRequiresIdenticalLocalPhoneItem")
    attempts = db.execute("SELECT * FROM attempts WHERE item_id=?", (old_id,)).fetchall()
    job = db.execute("SELECT * FROM execution_jobs WHERE reference_id=? AND kind='send'", (old_id,)).fetchone()
    if (len(attempts) != 1 or attempts[0]["id"] != old["attempt_id"] or attempts[0]["outcome"] != "failed_known"
            or not attempts[0]["finished_at"] or not job or job["state"] != "failed_known" or job["owner"]):
        raise ValueError("PhoneRetryOriginalAttemptNotSettled")
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "phone_social_authorizations" in tables and db.execute("SELECT 1 FROM phone_social_authorizations WHERE item_id=?", (old_id,)).fetchone():
        raise ValueError("PhoneRetryCannotHaveAnyOriginalSubmitAuthorization")
    if "cloud_receipt_outbox" in tables and db.execute("SELECT 1 FROM cloud_receipt_outbox WHERE job_id=?", (job["id"],)).fetchone():
        raise ValueError("PhoneRetryOriginalReceiptUnsettled")
    if "phone_social_operations" in tables:
        for operation in db.execute("SELECT kind,result FROM phone_social_operations WHERE item_id=?", (old_id,)):
            value = json.loads(operation["result"])
            native = value.get("result") or {}
            if (operation["kind"] == "social_submit" or value.get("status") not in {"failed", "succeeded", "cancelled"}
                    or native.get("external_effect") != "none" or native.get("quiescent") is not True):
                raise ValueError("PhoneRetryNativePreparationUnsettled")
    return {"old_item_id": old_id, "new_item_id": new_id, "account_id": old["account_id"],
            "old_result_sha256": digest(result), "old_payload_sha256": old["payload_hash"],
            "new_payload_sha256": new["payload_hash"]}


def register(engine, old_id, new_id, instruction):
    from .core import Conflict
    from .store import Store

    if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 2000:
        raise Conflict("PhoneRetryExplicitInstructionRequired")
    with engine.store.connect(write=True) as db:
        new = db.execute("SELECT t.state FROM items i JOIN tasks t ON t.id=i.task_id WHERE i.id=?", (new_id,)).fetchone()
        if not new or new[0] != "draft":
            raise Conflict("PhoneRetryNewDraftRequired")
        try:
            proof = evidence(db, old_id, new_id)
        except ValueError as error:
            raise Conflict(str(error)) from None
        task_id = db.execute("SELECT task_id FROM items WHERE id=?", (new_id,)).fetchone()[0]
        Store.audit(db, "phone.explicit_unsent_retry_authorized", {**proof, "instruction": instruction}, task_id, engine.clock())
    return {"authorized": True, **proof, "original_result_preserved": True}


def authorized(db, task, payload, old_id):
    rows = db.execute("SELECT details FROM audit WHERE task_id=? AND kind='phone.explicit_unsent_retry_authorized' "
                      "AND json_extract(details,'$.old_item_id')=? ORDER BY id DESC LIMIT 1", (task["id"],old_id)).fetchall()
    if not rows:
        return False
    marker = json.loads(rows[0][0])
    try:
        current = evidence(db, old_id, marker["new_item_id"])
    except (KeyError, ValueError):
        return False
    return (current == {k:marker[k] for k in current} and current["new_payload_sha256"] == digest(payload))
