"""Prepay bounded Telegram receipt reads against the existing shared account ledger."""

import math
import time

from .backends.common import BackendError
from .backends.telegram_reads import read_policy

RECEIPT_READ_LIMIT = 16
MIN_RECEIPT_SECONDS = 60


def reserve_receipt_reads(reads):
    """Charge the complete budget before submission; unused reads are never refunded."""
    reads.reserve(RECEIPT_READ_LIMIT)
    now = time.time()
    end = reads.budget_end
    if (
        not isinstance(end, (int, float))
        or not math.isfinite(end)
        or end - now < MIN_RECEIPT_SECONDS
    ):
        raise BackendError("PhoneTelegramReceiptBudgetWindowTooShort")
    return {"read_identity": reads.identity, "window_end": end, "reserved": RECEIPT_READ_LIMIT}


def validate_receipt_reads(reads, reservation, *, minimum_seconds=0):
    """Check the original window and current policy before a prepaid read or a click."""
    if (
        reservation["read_identity"] != reads.identity
        or reservation["reserved"] != RECEIPT_READ_LIMIT
    ):
        raise BackendError("PhoneTelegramReceiptBudgetBindingChanged")
    reads.refresh_policy()
    now = time.time()
    with reads.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        authority = db.execute(
            "SELECT policy FROM read_authority WHERE identity=?", (reads.identity,)
        ).fetchone()
        if authority:
            import json

            authoritative = read_policy(json.loads(authority[0]))
            policy = (
                authoritative
                if reads.context
                else {
                    key: (max if key == "window_seconds" else min)(value, authoritative[key])
                    for key, value in reads.requested_policy.items()
                }
            )
        else:
            policy = reads.policy
        row = db.execute(
            "SELECT start,end,used FROM budgets WHERE identity=?", (reads.identity,)
        ).fetchone()
        if (
            not row
            or not row[0] <= now < row[1]
            or row[1] != reservation["window_end"]
            or row[1] - now < minimum_seconds
            or row[2] > policy["requests_per_window"]
            or RECEIPT_READ_LIMIT > policy["scan_rpc_limit"]
        ):
            raise BackendError("PhoneTelegramReceiptBudgetUnavailable")
