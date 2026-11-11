"""Explicit single-store local authority; never silently fall back to a remote owner."""

import json
import math
import os
import socket
import uuid
from urllib.parse import urlsplit

from .models import canonical
from .store import Store


def state(db):
    row = db.execute("SELECT value FROM meta WHERE key='execution_local_primary'").fetchone()
    try:
        value = json.loads(row[0]) if row else {}
    except (ValueError, TypeError):
        return {"enabled": True, "invalid": True}
    return value if isinstance(value, dict) else {"enabled": True, "invalid": True}


def configured(db):
    return state(db).get("enabled") is True


def valid(db, account_id):
    mode = state(db)
    if mode.get("enabled") is not True:
        return None
    store = db.execute("SELECT value FROM meta WHERE key='execution_store_id'").fetchone()
    node = os.getenv("AP_NODE_ID", socket.gethostname())
    if not store or mode.get("store_id") != store[0] or mode.get("node_id") != node:
        return None
    row = db.execute(
        "SELECT value FROM meta WHERE key=?", ("local_primary_account:" + account_id,)
    ).fetchone()
    account = db.execute(
        "SELECT id,platform,mode,backend_label,identity_hash FROM accounts WHERE id=?",
        (account_id,),
    ).fetchone()
    try:
        grant = json.loads(row[0]) if row else {}
    except (ValueError, TypeError):
        return None
    if not account or not isinstance(grant, dict) or not grant.get("generation"):
        return None
    if not all(
        grant.get(key) == account[key]
        for key in ("id", "platform", "mode", "backend_label", "identity_hash")
    ):
        return None
    if (
        grant.get("node_id") != node
        or grant.get("store_id") != store[0]
        or grant.get("mode_generation") != mode.get("generation")
    ):
        return None
    return grant


def eligible(db):
    return [row[0] for row in db.execute("SELECT id FROM accounts") if valid(db, row[0])]


def enable(engine, evidence):
    """Administrative cutover, called locally after exclusive-owner verification.

    Each account stands alone. Evidence is produced by the cutover operator, not
    accepted by a public service endpoint. History and existing task state stay intact.
    """
    now = engine.clock()
    node = os.getenv("AP_NODE_ID", socket.gethostname())
    accepted, rejected = [], {}
    with engine.store.connect(write=True) as db:
        store = db.execute("SELECT value FROM meta WHERE key='execution_store_id'").fetchone()
        if not store or not store[0]:
            raise ValueError("LocalPrimaryStoreMissing")
        if db.execute(
            "SELECT 1 FROM execution_jobs WHERE state='leased' AND lease_until>? LIMIT 1", (now,)
        ).fetchone():
            raise ValueError("LocalPrimaryCutoverBusy")
        if db.execute("SELECT 1 FROM items WHERE status='executing' LIMIT 1").fetchone():
            raise ValueError("LocalPrimaryCutoverBusy")
        existing = state(db)
        if existing.get("enabled"):
            raise ValueError("LocalPrimaryAlreadyEnabled")
        generation = uuid.uuid4().hex
        for account_id, proof in evidence.items():
            account = db.execute(
                "SELECT id,platform,mode,backend_label,identity_hash FROM accounts WHERE id=?",
                (account_id,),
            ).fetchone()
            prior = db.execute(
                "SELECT * FROM account_task_channels WHERE account_id=?", (account_id,)
            ).fetchone()
            reason = None
            if not account or account["mode"] != "live" or not account["identity_hash"]:
                reason = "AccountIdentityUnavailable"
            elif (
                not isinstance(proof, dict)
                or proof.get("exclusive_local") is not True
                or not 0 <= now - proof.get("observed_at", 0) <= 120
            ):
                reason = "PriorOwnershipEvidenceRequired"
            elif prior and prior["until_at"] > now:
                reason = "PriorOwnershipStillActive"
            elif db.execute(
                "SELECT 1 FROM attempts WHERE account_id=? AND finished_at IS NULL LIMIT 1",
                (account_id,),
            ).fetchone():
                reason = "AttemptStillRunning"
            elif db.execute(
                "SELECT 1 FROM cloud_receipt_outbox r LEFT JOIN execution_jobs j ON j.id=r.job_id WHERE json_extract(r.lease,'$.account_id')=? OR j.account_id=? LIMIT 1",
                (account_id, account_id),
            ).fetchone():
                reason = "PriorReceiptPending"
            if reason:
                rejected[account_id] = reason
                continue
            grant = dict(account)
            grant.update(
                node_id=node,
                store_id=store[0],
                generation=uuid.uuid4().hex,
                mode_generation=generation,
                granted_at=now,
                prior_generation=prior["generation"] if prior else None,
                prior_until=prior["until_at"] if prior else None,
                ownership_observed_at=proof["observed_at"],
            )
            db.execute(
                "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("local_primary_account:" + account_id, canonical(grant)),
            )
            Store.audit(db, "execution.local_primary_account_granted", grant, now=now)
            accepted.append(account_id)
        if not accepted:
            raise ValueError("NoLocalPrimaryAccountsEligible")
        mode = {
            "enabled": True,
            "node_id": node,
            "store_id": store[0],
            "generation": generation,
            "activated_at": now,
        }
        db.execute(
            "INSERT INTO meta VALUES('execution_local_primary',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (canonical(mode),),
        )
        Store.audit(
            db,
            "execution.local_primary_enabled",
            {**mode, "accounts": accepted, "rejected": rejected},
            now=now,
        )
    return {"accepted": accepted, "rejected": rejected, "generation": generation}


def extend_account(engine, account_id, proof, *, source):
    """Grant one account under the existing local mode after verified ownership.

    This local administrative operation accepts evidence from the trusted cutover
    operator, never from a public request. It does not start or modify a task.
    """
    if not isinstance(source, str) or not source.strip() or len(source) > 500:
        raise ValueError("ExplicitLocalAccountInstructionRequired")
    if not isinstance(proof, dict) or proof.get("exclusive_local") is not True:
        raise ValueError("PriorOwnershipEvidenceRequired")
    observed_at = proof.get("observed_at")
    now = engine.clock()
    if (
        type(observed_at) not in (int, float)
        or not math.isfinite(observed_at)
        or not 0 <= now - observed_at <= 120
        or not isinstance(proof.get("evidence_ref"), str)
        or not proof["evidence_ref"].strip()
    ):
        raise ValueError("PriorOwnershipEvidenceRequired")
    node = os.getenv("AP_NODE_ID", socket.gethostname())
    with engine.store.connect(write=True) as db:
        mode = state(db)
        store = db.execute("SELECT value FROM meta WHERE key='execution_store_id'").fetchone()
        if (
            mode.get("enabled") is not True
            or not mode.get("generation")
            or not store
            or not store[0]
            or mode.get("store_id") != store[0]
            or mode.get("node_id") != node
        ):
            raise ValueError("LocalPrimaryModeBindingInvalid")
        account = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if (
            not account
            or account["mode"] != "live"
            or not account["identity_hash"]
            or account["status"] != "healthy"
            or account["disabled"]
            or account["cooldown_until"] > now
        ):
            raise ValueError("AccountIdentityUnavailable")
        expected = {
            "account_id": account_id,
            "identity_hash": account["identity_hash"],
            "node_id": node,
            "store_id": store[0],
            "mode_generation": mode["generation"],
        }
        if any(proof.get(key) != value for key, value in expected.items()):
            raise ValueError("PriorOwnershipEvidenceMismatch")
        if db.execute(
            "SELECT 1 FROM accounts WHERE platform=? AND identity_hash=? AND id<>?",
            (account["platform"], account["identity_hash"], account_id),
        ).fetchone():
            raise ValueError("LocalPrimaryIdentityAliasRequiresReconciliation")
        prior = db.execute(
            "SELECT * FROM account_task_channels WHERE account_id=?", (account_id,)
        ).fetchone()
        if proof.get("prior_generation") != (prior["generation"] if prior else None) or proof.get(
            "prior_until"
        ) != (prior["until_at"] if prior else None):
            raise ValueError("PriorOwnershipEvidenceMismatch")
        if prior and (prior["until_at"] > now or prior["state"] != "ready"):
            raise ValueError("PriorOwnershipStillActive")
        existing = db.execute(
            "SELECT 1 FROM meta WHERE key=?", ("local_primary_account:" + account_id,)
        ).fetchone()
        if existing:
            grant = valid(db, account_id)
            if grant:
                return {
                    "account_id": account_id,
                    "already_authorized": True,
                    "generation": grant["generation"],
                }
            raise ValueError("LocalPrimaryPriorGrantChanged")
        if db.execute(
            "SELECT 1 FROM account_blocks WHERE account_id=? AND state='active' "
            "AND (until_at IS NULL OR until_at>?)",
            (account_id, now),
        ).fetchone():
            raise ValueError("LocalPrimaryAccountBlocked")
        if db.execute(
            "SELECT 1 FROM attempts WHERE account_id=? AND finished_at IS NULL", (account_id,)
        ).fetchone():
            raise ValueError("AttemptStillRunning")
        if db.execute(
            "SELECT 1 FROM items WHERE account_id=? AND status IN "
            "('executing','unknown','scheduled')",
            (account_id,),
        ).fetchone():
            raise ValueError("LocalPrimaryOutcomeUnresolved")
        if (
            db.execute(
                "SELECT 1 FROM execution_jobs WHERE account_id=? AND state='leased' "
                "AND lease_until>?",
                (account_id, now),
            ).fetchone()
            or db.execute(
                "SELECT 1 FROM account_lanes WHERE account_id=? AND expires_at>?", (account_id, now)
            ).fetchone()
        ):
            raise ValueError("LocalPrimaryAccountBusy")
        if db.execute(
            "SELECT 1 FROM cloud_receipt_outbox r LEFT JOIN execution_jobs j ON j.id=r.job_id "
            "WHERE json_extract(r.lease,'$.account_id')=? OR j.account_id=? LIMIT 1",
            (account_id, account_id),
        ).fetchone():
            raise ValueError("PriorReceiptPending")
        grant = {
            key: account[key]
            for key in ("id", "platform", "mode", "backend_label", "identity_hash")
        }
        grant.update(
            node_id=node,
            store_id=store[0],
            generation=uuid.uuid4().hex,
            mode_generation=mode["generation"],
            granted_at=now,
            prior_generation=prior["generation"] if prior else None,
            prior_until=prior["until_at"] if prior else None,
            ownership_observed_at=observed_at,
            ownership_evidence_ref=proof["evidence_ref"],
        )
        db.execute(
            "INSERT INTO meta VALUES(?,?)",
            ("local_primary_account:" + account_id, canonical(grant)),
        )
        Store.audit(
            db,
            "execution.local_primary_account_granted",
            {**grant, "source": source.strip(), "extended_existing_mode": True},
            now=now,
        )
    return {
        "account_id": account_id,
        "already_authorized": False,
        "generation": grant["generation"],
    }


def _target(value):
    text = str(value or "").strip().lower().rstrip("/")
    if text.startswith("@"):
        return "telegram:" + text[1:]
    if "://" in text:
        url = urlsplit(text)
        host = (url.hostname or "").removeprefix("www.")
        if host in {"t.me", "telegram.me"}:
            return "telegram:" + url.path.strip("/")
        return host + url.path.rstrip("/")
    return text


def uncertain_duplicate(db, account, payload, item_id):
    """Do not recreate the same uncertain action under a new task id."""
    for old in db.execute(
        "SELECT i.id,i.task_id,i.payload FROM items i JOIN accounts a ON a.id=i.account_id WHERE i.id<>? AND i.status IN ('unknown','executing') AND a.platform=? AND (a.id=? OR (? IS NOT NULL AND a.identity_hash=?))",
        (
            item_id,
            account["platform"],
            account["id"],
            account["identity_hash"],
            account["identity_hash"],
        ),
    ):
        try:
            prior = json.loads(old["payload"])
        except ValueError:
            continue
        if (
            _target(prior.get("target")) == _target(payload.get("target"))
            and prior.get("action") == payload.get("action")
            and str(prior.get("text", "")).strip() == str(payload.get("text", "")).strip()
        ):
            return {
                "reason": "interaction_history_unresolved_or_published",
                "blocked_by_item_id": old["id"],
                "blocked_by_task_id": old["task_id"],
            }
    return None
