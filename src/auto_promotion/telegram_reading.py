"""Local Telegram read settings and durable consumer scopes; never authorizes sending."""

import json

from .models import TelegramReadPolicy, digest
from .store import Store


def read_policy_context(context):
    if context is None:
        return {"policy": TelegramReadPolicy().model_dump(), "revision": 0}
    if (
        not isinstance(context, dict)
        or not context.get("database")
        or not context.get("account_id")
    ):
        raise ValueError("TelegramReadPolicyContextRequired")
    with Store(context["database"], initialize=False).connect() as db:
        account = db.execute(
            "SELECT platform,backend_label,identity_hash FROM accounts WHERE id=?",
            (context["account_id"],),
        ).fetchone()
        if not account or account[0] != "telegram":
            raise ValueError("TelegramReadPolicyAccountMismatch")
        if (
            context.get("backend_label") is not None
            and context["backend_label"] != account["backend_label"]
        ):
            raise ValueError("TelegramReadPolicyAccountMismatch")
        if (
            context.get("identity_hash") is not None
            and account["identity_hash"] is not None
            and context["identity_hash"] != account["identity_hash"]
        ):
            raise ValueError("TelegramReadPolicyIdentityMismatch")
        row = db.execute(
            "SELECT payload,revision FROM telegram_read_settings WHERE account_id=?",
            (context["account_id"],),
        ).fetchone()
    return {
        "policy": TelegramReadPolicy.model_validate(json.loads(row[0])).model_dump()
        if row
        else TelegramReadPolicy().model_dump(),
        "revision": row[1] if row else 0,
    }


def content_scope(account, connection_revision, target, limit=200):
    normalized = (
        target.removeprefix("https://t.me/").removeprefix("http://t.me/").lstrip("@").casefold()
    )
    identity = digest([account["id"], account.get("identity_hash"), connection_revision])
    return identity, digest(["telegram_content_incremental_v1", normalized, limit])


READ_FIELDS = (
    "read_state",
    "read_complete",
    "read_page_complete",
    "read_cursor_next",
    "read_cursor_rebound",
    "read_observed_at",
    "initial_window_scope",
    "read_identity_hash",
    "read_identity_verified",
    "read_peer_id",
    "read_rpc_attempts",
    "read_rpc_used",
    "read_rpc_count_source",
    "read_rpc_count_scope",
    "read_rpc_internal_retries",
    "read_rpc_wire_attempts",
    "read_rpc_budget_reserved",
    "read_rpc_budget_rejected",
    "read_rpc_budget_used",
    "read_rpc_budget_limit",
    "read_rpc_budget_resets_at",
    "read_rpc_budget_scope",
    "read_rpc_budget_authority",
    "read_cache_hits",
    "read_cache_misses",
    "read_cache_observed_at",
    "read_retry_after_seconds",
    "local_read_budget",
)


def read_diagnostics(value):
    return {key: value[key] for key in READ_FIELDS if key in value}
