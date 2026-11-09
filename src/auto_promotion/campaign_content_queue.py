"""Opt-in campaign adapter; preserve the existing campaign task and assignment IDs."""

import json

from .content_production import ContentProduction
from .content_queue import ContentBatch, ContentQueue
from .models import digest


def save_assignment(engine, assignment, *, db):
    payload = json.loads(assignment["payload"])
    account = db.execute(
        "SELECT platform FROM accounts WHERE id=?", (assignment["account_id"],)
    ).fetchone()
    revision = digest({"payload": payload, "profile": assignment["profile_snapshot"]})
    source = json.loads(assignment["source"])
    return ContentProduction(engine).enqueue(
        "campaign_assignment",
        assignment["id"],
        revision,
        account["platform"],
        payload,
        {
            "campaign_id": assignment["campaign_id"],
            "opportunity_id": assignment["opportunity_id"],
            "source_hash": digest(source),
            "source_context": source["context"][:2000],
        },
        db=db,
    )


def populate(engine, task, assignments):
    saved = []
    with engine.store.connect(write=True) as db:
        for assignment in assignments:
            entry = save_assignment(engine, assignment, db=db)
            payload = json.loads(assignment["payload"])
            saved.append(
                {
                    "content_id": entry["id"],
                    "account_id": assignment["account_id"],
                    "scheduled_at": payload["scheduled_at"],
                }
            )
    saved.sort(key=lambda value: value["scheduled_at"])
    batch = ContentBatch(
        idempotency_key="campaign-task:" + task["id"],
        task={"name": task["name"], "account_ids": task["account_ids"], "policy": task["policy"]},
        assignments=saved,
    )
    ContentQueue(engine).materialize(batch, _existing_task_id=task["id"])
    return engine.task(task["id"])
