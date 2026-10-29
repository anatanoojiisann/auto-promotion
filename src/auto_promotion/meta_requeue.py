"""Explicit recovery of a Meta item whose one-use submit guard was never used.

The old attempt and quota history remain terminal. A fresh normal task still
needs an explicit start and all normal begin/guard/receipt checks.
"""

import json

from .store import Store


def requeue_unsubmitted(browser, item_id, replacement_item_id, authorization):
    from .core import Conflict
    from .meta_browser import event_key

    if not isinstance(authorization, str) or not 1 <= len(authorization.strip()) <= 2000:
        raise Conflict("ExplicitRequeueAuthorizationRequired")
    now = browser.clock()
    with browser.store.connect(write=True) as db:
        old = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        new = db.execute("SELECT * FROM items WHERE id=?", (replacement_item_id,)).fetchone()
        if not old or not new or item_id == replacement_item_id:
            raise Conflict("RequeueRequiresDistinctExistingItems")
        account = db.execute("SELECT * FROM accounts WHERE id=?", (old["account_id"],)).fetchone()
        if not account or account["platform"] not in {"facebook", "instagram"}:
            raise Conflict("RequeueRequiresMetaAccount")
        previous = json.loads(old["result"])
        claim = db.execute(
            "SELECT * FROM meta_browser_claims WHERE item_id=?", (item_id,)
        ).fetchone()
        attempt = db.execute("SELECT * FROM attempts WHERE id=?", (old["attempt_id"],)).fetchone()
        if not (
            old["status"] == "failed_known"
            and not old["external_reference"]
            and previous.get("confirmed_not_submitted") is True
            and previous.get("reason") == "MetaBrowserNotSubmitted"
            and claim
            and claim["submission_started"] == 0
            and json.loads(claim["result"]) == previous
            and attempt
            and attempt["outcome"] == "failed_known"
            and attempt["finished_at"] is not None
            and json.loads(claim["claim"]).get("attempt_id") == old["attempt_id"]
        ):
            raise Conflict("RequeueRequiresDurableProofOfNoSubmission")
        for result in (previous, json.loads(claim["result"])):
            if any(
                result.get(k)
                for k in (
                    "external_reference",
                    "comment_id",
                    "message_id",
                    "published_at",
                    "submission_started",
                )
            ):
                raise Conflict("RequeueReceiptOrSubmissionExists")
        if db.execute("SELECT 1 FROM publication_evidence WHERE item_id=?", (item_id,)).fetchone():
            raise Conflict("RequeuePublicationEvidenceExists")
        if db.execute("SELECT 1 FROM meta_browser_history WHERE item_id=?", (item_id,)).fetchone():
            raise Conflict("RequeueCannotRestartImportedHistory")
        if db.execute(
            "SELECT 1 FROM execution_jobs WHERE reference_id=? AND (state='leased' OR lease_until>?)",
            (item_id, now),
        ).fetchone():
            raise Conflict("RequeueOldExecutionStillLeased")
        task = db.execute("SELECT * FROM tasks WHERE id=?", (new["task_id"],)).fetchone()
        old_task = db.execute("SELECT * FROM tasks WHERE id=?", (old["task_id"],)).fetchone()
        payloads = [json.loads(r["payload"]) for r in (old, new)]
        frozen = [{k: v for k, v in p.items() if k != "scheduled_at"} for p in payloads]
        if not (
            new["account_id"] == old["account_id"]
            and new["status"] == "not_attempted"
            and not new["attempt_id"]
            and not new["external_reference"]
            and task
            and task["state"] == "draft"
            and json.loads(task["policy"]).get("execution_channel") == "local"
            and json.loads(task["account_ids"]) == [old["account_id"]]
            and json.loads(task["policy"]).get("project_id")
            == json.loads(old_task["policy"]).get("project_id")
            and frozen[0] == frozen[1]
        ):
            raise Conflict("RequeueRequiresExactUnstartedReplacement")
        key = event_key(account["platform"], payloads[0]["target"])
        contact = db.execute(
            "SELECT * FROM meta_browser_contacts WHERE platform=? AND event_key=?",
            (account["platform"], key),
        ).fetchone()
        details = {
            "source_item_id": item_id,
            "replacement_item_id": replacement_item_id,
            "source_payload_hash": old["payload_hash"],
            "replacement_payload_hash": new["payload_hash"],
            "authorization": authorization,
            "attempts_changed": False,
            "quotas_changed": False,
        }
        if not contact:
            prior = db.execute(
                "SELECT details FROM audit WHERE kind='meta_browser.requeued_unsubmitted' AND task_id=? ORDER BY at DESC",
                (new["task_id"],),
            ).fetchall()
            if any(
                json.loads(r[0]).get("source_item_id") == item_id
                and json.loads(r[0]).get("replacement_item_id") == replacement_item_id
                for r in prior
            ):
                return {"status": "requeued_unsubmitted", **details, "already_registered": True}
            raise Conflict("RequeueContactReservationMissing")
        if (
            contact["item_id"] != item_id
            or contact["account_id"] != old["account_id"]
            or contact["state"] != "failed_known"
        ):
            raise Conflict("RequeueContactReservationChanged")
        changed = db.execute(
            "DELETE FROM meta_browser_contacts WHERE platform=? AND event_key=? AND item_id=? AND state='failed_known'",
            (account["platform"], key, item_id),
        ).rowcount
        if changed != 1:
            raise Conflict("RequeueContactReservationChanged")
        Store.audit(db, "meta_browser.requeued_unsubmitted", details, new["task_id"], now)
        return {"status": "requeued_unsubmitted", **details, "already_registered": False}
