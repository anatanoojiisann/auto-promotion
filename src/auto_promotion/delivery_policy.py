"""Versioned item deadlines. Legacy tasks retain their original timing rules."""

from .models import epoch

DURABLE = "phone_queue_v1"


def durable(policy):
    return policy.get("dispatch_policy") == DURABLE


def late(policy, due_at, now):
    return not durable(policy) and now - due_at > 900


def item_deadline(policy, due_at, action):
    end = epoch(policy["ends_at"])
    if action in {"schedule_message", "schedule_file"}:
        return min(end, due_at)
    return end if durable(policy) else min(end, due_at + 900)
