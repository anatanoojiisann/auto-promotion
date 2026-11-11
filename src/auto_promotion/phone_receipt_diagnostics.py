"""Local-only task-to-native-receipt metadata, using exact indexed item lookups."""
import time


def chain(db, items, *, budget_seconds=2):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = {"phone_social_operations", "phone_social_authorizations", "phone_social_receipt_jobs"}
    if not required <= tables:
        return []
    deadline = time.monotonic() + budget_seconds
    result = []
    db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    try:
        for item in items:
            if time.monotonic() >= deadline:
                raise TimeoutError("PhoneEvidenceQueryBudgetExceeded")
            operation = db.execute(
                "SELECT id,execution_id,phase,json_extract(result,'$.command_id') command_id,"
                "json_extract(result,'$.status') runtime_status FROM phone_social_operations "
                "WHERE account_id=? AND kind='social_submit' AND item_id=? ORDER BY created_at DESC LIMIT 1",
                (item["account_id"], item["id"])).fetchone()
            if not operation:
                continue
            auth = db.execute("SELECT id,native_id,used_at FROM phone_social_authorizations WHERE item_id=?", (item["id"],)).fetchone()
            job = db.execute(
                "SELECT check_id,state,next_at,expires_at,"
                "json_extract(result,'$.observations[#-1].state') reader_state,"
                "json_extract(result,'$.observations[#-1].reason') reason,"
                "json_extract(result,'$.observations[#-1].receipt.native_id') comment_id "
                "FROM phone_social_receipt_jobs WHERE item_id=?", (item["id"],)).fetchone()
            result.append({"item_id": item["id"], "account_id": item["account_id"],
                "attempt_id": item["attempt_id"], "item_status": item["status"],
                "operation_id": operation["id"], "execution_id": operation["execution_id"],
                "command_id": operation["command_id"], "original_runtime_status": operation["runtime_status"],
                "authorization_id": auth["id"] if auth else None,
                "native_command_id": auth["native_id"] if auth else None,
                "submission_authorized_at": auth["used_at"] if auth else None,
                "receipt": dict(job) if job else None,
                "next_checkpoint": "read_original_receipt" if job and job["state"] == "pending" else "explicit_read_resume_required"})
        return result
    finally:
        db.set_progress_handler(None, 0)
