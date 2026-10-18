"""Bounded negative caching for read-only Telegram eligibility checks.

No successful authorization is cached. Every actual send still checks its own
account, manifest, stops, lease and current target permissions.
"""

import json
from pathlib import Path

from .models import canonical, digest

DEFINITE_DENIALS = {
    "ChannelPrivateError",
    "UserNotParticipantError",
    "ChatWriteForbiddenError",
    "ChatAdminRequiredError",
    "PublicTelegramGroupRequired",
    "public_group_required",
    "TargetWriteForbidden",
}


class TargetChecks:
    def __init__(self, engine, account, operation, payload):
        self.engine, self.account, self.operation = engine, account, operation
        item = payload.get("payload", payload)
        target = item.get("target", "")
        self.enabled = bool(
            account["platform"] == "telegram"
            and account["mode"] == "live"
            and operation in {"preflight", "verify_join", "feedback"}
            and target
        )
        normalized = target.removeprefix("https://t.me/").removeprefix("http://t.me/")
        self.target = normalized.lstrip("@").casefold()
        self.key = digest(
            [
                operation,
                self.target,
                sorted({Path(m["filename"]).suffix.lower() for m in item.get("media", [])}),
            ]
        )
        self.identity = digest([account["backend_label"], account.get("identity_hash")])

    def read(self, db=None):
        if not self.enabled:
            return None
        query = "SELECT result,retry_at FROM telegram_target_checks WHERE account_id=? AND identity_key=? AND check_key=?"
        params = (self.account["id"], self.identity, self.key)
        row = db.execute(query, params).fetchone() if db else self.engine.store.one(query, params)
        if row and row["retry_at"] > self.engine.clock():
            return {**json.loads(row["result"]), "eligibility_retry_at": row["retry_at"]}
        return None

    def failure(self, reason):
        return {"allowed": False, "verified": False, "reason": reason}

    def save(self, result):
        if not self.enabled:
            return
        allowed = (
            result.get("allowed") is True
            if self.operation == "preflight"
            else result.get("verified") is True and result.get("can_send") is not False
        )
        with self.engine.store.connect(write=True) as db:
            params = (self.account["id"], self.identity, self.key)
            if allowed:
                if self.operation == "verify_join" and result.get("can_send") is True:
                    db.execute(
                        "DELETE FROM telegram_target_checks WHERE account_id=? AND identity_key=? AND target=?",
                        (self.account["id"], self.identity, self.target),
                    )
                else:
                    db.execute(
                        "DELETE FROM telegram_target_checks WHERE account_id=? AND identity_key=? AND check_key=?",
                        params,
                    )
                return
            # Definite denials expire too: a membership/permission change can
            # recover through a later read, while network faults wait only 30s.
            seconds = (
                300
                if result.get("reason") in DEFINITE_DENIALS or result.get("can_send") is False
                else 30
            )
            if result.get("rate_limited") or result.get("risk_warning"):
                seconds = max(seconds, int(result.get("retry_after_seconds") or 3600))
            safe = {
                key: result[key]
                for key in (
                    "status",
                    "allowed",
                    "verified",
                    "reason",
                    "can_send",
                    "external_reference",
                    "outbound_eligible_at_utc",
                    "rate_limited",
                    "risk_warning",
                    "retry_after_seconds",
                )
                if key in result
            }
            db.execute(
                "INSERT INTO telegram_target_checks VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(account_id,identity_key,check_key) DO UPDATE SET "
                "retry_at=excluded.retry_at,result=excluded.result",
                (*params, self.target, self.engine.clock() + seconds, canonical(safe)),
            )
