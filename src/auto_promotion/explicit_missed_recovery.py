"""Audited, same-account transfer of explicitly requested and proven-missed replies."""

import json

from .cloud_control import CloudControl
from .execution_channels import offline_only
from .interaction_intents import event_id, historical_sender, scope_id
from .models import digest
from .store import Store

SOURCE = "explicit_user_missed_subset_recovery"


def recover(engine, old_task_id, new_task_id, pairs, *, source):
    """Transfer only the named, never-attempted items after a cloud terminal check.

    No attempt, receipt, old item or frozen manifest is modified. The caller must
    have an explicit human instruction for these exact items.
    """
    if source != SOURCE or not pairs or len(pairs) > 20:
        raise ValueError("MissedRecoveryRequiresExplicitScope")
    if len({p[0] for p in pairs}) != len(pairs) or len({p[1] for p in pairs}) != len(pairs):
        raise ValueError("MissedRecoveryDuplicateItem")
    old_task = engine.store.one("SELECT * FROM tasks WHERE id=?", (old_task_id,))
    new_task = engine.store.one("SELECT * FROM tasks WHERE id=?", (new_task_id,))
    if (
        not old_task
        or not new_task
        or old_task["state"] != "completed"
        or new_task["state"] != "draft"
    ):
        raise ValueError("MissedRecoveryRequiresCompletedAndDraft")
    if old_task["mode"] != new_task["mode"] or old_task["campaign_id"] or new_task["campaign_id"]:
        raise ValueError("MissedRecoveryModeOrCampaignMismatch")
    old_policy, new_policy = json.loads(old_task["policy"]), json.loads(new_task["policy"])
    if new_policy.get("execution_channel", "inherit") != "inherit":
        raise ValueError("MissedRecoveryMustPreserveDefaultRoute")
    if offline_only(engine.store):
        # Global offline mode may coexist with an unrelated account's cloud path.
        for old_id, _ in pairs:
            item = engine.store.one(
                "SELECT account_id FROM items WHERE id=? AND task_id=?", (old_id, old_task_id)
            )
            if not item or offline_only(engine.store, item["account_id"]):
                raise ValueError("MissedRecoveryCloudEvidenceUnavailable")
    cloud = CloudControl.configured()
    if cloud is None:
        raise ValueError("MissedRecoveryCloudEvidenceUnavailable")
    states = {}
    for old_id, _ in pairs:
        value = cloud.request("/v1/job", {"id": "send:" + old_id})
        job = value.get("job") if isinstance(value, dict) else None
        if not job or job.get("id") != "send:" + old_id or job.get("state") != "missed":
            raise ValueError("MissedRecoveryCloudNotTerminal")
        states[old_id] = job
    now = engine.clock()
    with engine.store.connect(write=True) as db:
        original = db.execute("SELECT * FROM tasks WHERE id=?", (old_task_id,)).fetchone()
        replacement = db.execute("SELECT * FROM tasks WHERE id=?", (new_task_id,)).fetchone()
        if (
            not original
            or not replacement
            or original["state"] != "completed"
            or replacement["state"] != "draft"
            or original["manifest_hash"] != old_task["manifest_hash"]
            or replacement["policy_hash"] != new_task["policy_hash"]
        ):
            raise ValueError("MissedRecoveryTaskChanged")
        engine._integrity(db, original, "outbound")
        engine._integrity(db, replacement, "discovery_join")
        project = scope_id(db, old_policy.get("project_id", "default"))
        if project != scope_id(db, new_policy.get("project_id", "default")):
            raise ValueError("MissedRecoveryScopeMismatch")
        if db.execute("SELECT COUNT(*) FROM items WHERE task_id=?", (new_task_id,)).fetchone()[
            0
        ] != len(pairs):
            raise ValueError("MissedRecoveryDraftScopeMismatch")
        changes = []
        for old_id, new_id in pairs:
            old = db.execute(
                "SELECT * FROM items WHERE id=? AND task_id=?", (old_id, old_task_id)
            ).fetchone()
            new = db.execute(
                "SELECT * FROM items WHERE id=? AND task_id=?", (new_id, new_task_id)
            ).fetchone()
            if not old or not new or old["account_id"] != new["account_id"]:
                raise ValueError("MissedRecoveryAccountMismatch")
            if (
                old["status"] != "missed"
                or old["attempt_id"]
                or old["external_reference"]
                or json.loads(old["result"]).get("reason") != "delivery_window_elapsed"
            ):
                raise ValueError("MissedRecoveryOriginalNotProvenUnsent")
            if new["status"] != "not_attempted" or new["attempt_id"] or new["external_reference"]:
                raise ValueError("MissedRecoveryReplacementNotReady")
            if db.execute(
                "SELECT 1 FROM attempts WHERE item_id IN (?,?)", (old_id, new_id)
            ).fetchone():
                raise ValueError("MissedRecoveryAttemptExists")
            old_payload, new_payload = json.loads(old["payload"]), json.loads(new["payload"])
            if (
                digest(old_payload) != old["payload_hash"]
                or digest(new_payload) != new["payload_hash"]
            ):
                raise ValueError("MissedRecoveryPayloadIntegrityFailed")
            ignored = {"scheduled_at", "position", "account_label", "platform"}
            if {k: v for k, v in old_payload.items() if k not in ignored} != {
                k: v for k, v in new_payload.items() if k not in ignored
            }:
                raise ValueError("MissedRecoveryPayloadMismatch")
            account = db.execute(
                "SELECT * FROM accounts WHERE id=?", (old["account_id"],)
            ).fetchone()
            if not account or account["platform"] != "x":
                raise ValueError("MissedRecoveryUnsupportedAccount")
            event = event_id("x", old_payload)
            if not event or event_id("x", new_payload) != event:
                raise ValueError("MissedRecoveryTargetMismatch")
            job = db.execute(
                "SELECT * FROM execution_jobs WHERE id=?", ("send:" + old_id,)
            ).fetchone()
            if (
                not job
                or job["state"] != "missed"
                or job["owner"]
                or job["lease_until"] > now
                or job["expires_at"] > now
            ):
                raise ValueError("MissedRecoveryLocalJobNotTerminal")
            if (
                states[old_id].get("account_id") != old["account_id"]
                or states[old_id].get("pool") != job["pool"]
            ):
                raise ValueError("MissedRecoveryCloudIdentityMismatch")
            if db.execute(
                "SELECT 1 FROM cloud_receipt_outbox WHERE job_id IN (?,?)",
                ("send:" + old_id, "send:" + new_id),
            ).fetchone():
                raise ValueError("MissedRecoveryReceiptPending")
            if historical_sender(db, project, "x", event):
                raise ValueError("MissedRecoveryOtherHistory")
            key = (project, "x", event, "reply")
            intent = db.execute(
                "SELECT * FROM interaction_intents WHERE project_id=? AND platform=? AND source_event_id=? AND action_kind=?",
                key,
            ).fetchone()
            marker = db.execute(
                "SELECT 1 FROM audit WHERE task_id=? AND kind='interaction.explicit_missed_recovery_authorized' AND json_extract(details,'$.old_item_id')=? AND json_extract(details,'$.new_item_id')=?",
                (new_task_id, old_id, new_id),
            ).fetchone()
            if intent and intent["state"] == "reserved" and intent["item_id"] == new_id and marker:
                continue
            if not intent or intent["state"] != "reserved" or intent["item_id"] != old_id:
                raise ValueError("MissedRecoveryIntentNotOwned")
            changes.append((key, old_id, new_id, event))
        for key, old_id, new_id, event in changes:
            if (
                db.execute(
                    "UPDATE interaction_intents SET item_id=? WHERE project_id=? AND platform=? AND source_event_id=? AND action_kind=? AND item_id=? AND state='reserved'",
                    (new_id, *key, old_id),
                ).rowcount
                != 1
            ):
                raise ValueError("MissedRecoveryIntentChanged")
            Store.audit(
                db,
                "interaction.explicit_missed_recovery_authorized",
                {
                    "old_item_id": old_id,
                    "new_item_id": new_id,
                    "source_event_id": event,
                    "source": source,
                    "cloud_state": "missed",
                    "attempts_changed": False,
                },
                new_task_id,
                now,
            )
    return {
        "old_task_id": old_task_id,
        "new_task_id": new_task_id,
        "count": len(pairs),
        "transferred": len(changes),
    }
