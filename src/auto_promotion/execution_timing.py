"""Best-effort timing evidence, separate from business outcomes and send authority."""

import json
import time
import uuid
from contextlib import contextmanager


@contextmanager
def stage(engine, task_id, name, **ids):
    identifier, started, monotonic = uuid.uuid4().hex, engine.clock(), time.monotonic()
    details = {"span_id": identifier, "stage": name, **ids}
    engine.store.audit_best_effort("execution.stage_started", details, task_id=task_id, now=started)
    outcome = "returned"
    try:
        yield
    except BaseException:
        outcome = "raised"
        raise
    finally:
        engine.store.audit_best_effort(
            "execution.stage_finished",
            {
                **details,
                "started_at": started,
                "finished_at": engine.clock(),
                "duration_seconds": max(0, time.monotonic() - monotonic),
                "call_outcome": outcome,
            },
            task_id=task_id,
            now=engine.clock(),
        )


def report(db, task_id):
    rows = db.execute(
        "SELECT at,kind,details FROM audit WHERE task_id=? AND kind IN "
        "('execution.stage_started','execution.stage_finished') ORDER BY id DESC LIMIT 501",
        (task_id,),
    ).fetchall()
    spans = {}
    for row in reversed(rows[:500]):
        value = json.loads(row["details"])
        spans[value["span_id"]] = {
            "started_at": row["at"],
            **value,
            "in_progress": row["kind"] == "execution.stage_started",
        }
    return {
        "coverage": "recorded_stages",
        "truncated": len(rows) > 500,
        "spans": list(spans.values()),
    }
