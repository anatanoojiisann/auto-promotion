"""One bounded X discovery page through the registered, guarded reader."""

import math
import time

from .models import digest
from .routing import route_for


def search(engine, account_id, query, *, lookback_hours=12, timeout_seconds=30, max_results=100):
    from .core import Conflict

    if (
        not isinstance(query, str)
        or not query.strip()
        or len(query) > 1000
        or type(lookback_hours) not in {int, float}
        or not math.isfinite(lookback_hours)
        or not 0 < lookback_hours <= 168
        or type(timeout_seconds) is not int
        or not 1 <= timeout_seconds <= 120
        or type(max_results) is not int
        or not 1 <= max_results <= 100
    ):
        raise Conflict("InvalidXReadWindowOrBudget")
    account = engine.account(account_id)
    if account["platform"] != "x":
        raise Conflict("XAccountRequired")
    if account["mode"] != "live":
        raise Conflict("LiveXReaderRequired")
    upper = engine.clock()
    lower = upper - lookback_hours * 3600
    started = time.monotonic()
    with engine.lanes.acquire(account_id, resource=route_for(account, {}).resource):
        current = engine.account(account_id)
        if current.get("disabled") or current.get("outbound_block"):
            result = {
                "candidates": [],
                "read_state": "blocked",
                "reason": "AccountDisabled"
                if current.get("disabled")
                else "AccountOutboundBlocked",
                "read_primary_request_count": 0,
                "read_primary_response_count": 0,
                "account_block_reason": (current.get("outbound_block") or {}).get("reason"),
            }
        else:
            result = engine.call(
                current,
                "discover",
                {
                    "high_frequency": True,
                    "read_only": True,
                    "query": query.strip(),
                    "lower_at": lower,
                    "upper_at": upper,
                    "cursor": None,
                    "timeout_seconds": timeout_seconds,
                    "read_request_limit": 30,
                },
            )
    observations = result.get("candidates", [])[:max_results]
    # Search-page observations never imply an unresolved need or permission to reply.
    value = {
        **result,
        "candidates": observations,
        "observations": observations,
        "decisions": [],
        "quality_status": "context_review_required",
        "coverage_complete": False,
        "read_complete": False,
        "reader": "unified_x_readonly_v1",
        "account_id": account_id,
        "window": {"lower_at": lower, "upper_at": upper},
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "external_interactions": 0,
    }
    engine.store.audit_best_effort(
        "x.readonly_search",
        {
            "account_id": account_id,
            "query_hash": digest(query.strip()),
            "count": len(observations),
            "reader": value["reader"],
            "read_state": result.get("read_state"),
            "elapsed_seconds": value["elapsed_seconds"],
            "lower_at": lower,
            "upper_at": upper,
        },
        now=engine.clock(),
    )
    return value
