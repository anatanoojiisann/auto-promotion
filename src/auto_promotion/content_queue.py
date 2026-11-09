"""Device-independent immutable content, with an idempotent handoff to AP tasks.

Saving or materializing content never starts a task or calls a platform. The
existing explicit start, frozen manifest, original send queue and permits remain
the only submission path.
"""

import json
import uuid
from contextlib import nullcontext
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .core import Conflict, NotFound
from .models import CAPABILITIES, ItemInput, Model, TaskInput, canonical, digest
from .store import Store


class ContentDraft(ItemInput):
    platform: Literal["telegram", "reddit", "instagram", "facebook", "x", "discord", "tiktok"]
    idempotency_key: str = Field(min_length=1, max_length=160)
    account_id: Literal[""] = ""
    scheduled_at: Literal[""] = ""
    context: dict = Field(default_factory=dict)

    @field_validator("scheduled_at")
    @classmethod
    def valid_time(cls, value):
        return value  # Content production has no execution schedule.

    @model_validator(mode="after")
    def supported_content(self):
        if self.action not in CAPABILITIES[self.platform] - {"discover", "join"}:
            raise ValueError("UnsupportedContentAction")
        if len(canonical(self.context).encode()) > 32768:
            raise ValueError("ContentContextTooLarge")
        return self


class Assignment(Model):
    content_id: str
    account_id: str
    scheduled_at: str


class ContentBatch(Model):
    idempotency_key: str = Field(min_length=1, max_length=160)
    task: TaskInput
    assignments: list[Assignment] = Field(min_length=1, max_length=200)


class ContentQueue:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def put(self, value: ContentDraft, *, _db=None):
        raw = value.model_dump()
        for key in ("idempotency_key", "context", "platform", "account_id", "scheduled_at"):
            raw.pop(key)
        fingerprint = digest({"platform": value.platform, "payload": raw, "context": value.context})
        with nullcontext(_db) if _db is not None else self.store.connect(write=True) as db:
            old = db.execute(
                "SELECT * FROM content_queue_entries WHERE idempotency_key=?",
                (value.idempotency_key,),
            ).fetchone()
            if old:
                if old["content_hash"] != fingerprint:
                    raise Conflict("ContentIdempotencyConflict")
                return self.get(old["id"], _db=db)
            identifier = "content-" + uuid.uuid4().hex
            db.execute(
                "INSERT INTO content_queue_entries VALUES(?,?,?,?,?,?,?)",
                (
                    identifier,
                    value.idempotency_key,
                    value.platform,
                    canonical(raw),
                    canonical(value.context),
                    fingerprint,
                    self.engine.clock(),
                ),
            )
            Store.audit(
                db,
                "content.saved",
                {"content_id": identifier, "content_hash": fingerprint},
                None,
                self.engine.clock(),
            )
            return self.get(identifier, _db=db)

    def get(self, identifier, *, _db=None):
        with nullcontext(_db) if _db is not None else self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM content_queue_entries WHERE id=?", (identifier,)
            ).fetchone()
            if not row:
                raise NotFound("ContentNotFound")
            row = dict(row)
            row["payload"], row["context"] = json.loads(row["payload"]), json.loads(row["context"])
            binding = db.execute(
                "SELECT b.id batch_id,b.task_id FROM content_queue_members m "
                "JOIN content_queue_batches b ON b.id=m.batch_id WHERE m.content_id=?",
                (identifier,),
            ).fetchone()
            row["binding"] = dict(binding) if binding else None
            return row

    def list(self, limit=100):
        return [
            self.get(r["id"])
            for r in self.store.rows(
                "SELECT id FROM content_queue_entries ORDER BY created_at,id LIMIT ?", (limit,)
            )
        ]

    @staticmethod
    def _check_campaign_draft(db, original, value):
        if (
            not original
            or original["state"] != "draft"
            or not original["campaign_id"]
            or original["manifest_hash"] is not None
            or json.loads(original["account_ids"]) != value.task.account_ids
            or json.loads(original["policy"]) != value.task.policy.model_dump()
            or db.execute("SELECT 1 FROM items WHERE task_id=?", (original["id"],)).fetchone()
            or db.execute("SELECT 1 FROM attempts WHERE task_id=?", (original["id"],)).fetchone()
        ):
            raise Conflict("ContentExistingTaskMustBeUntouchedCampaignDraft")

    def materialize(self, value: ContentBatch, *, _existing_task_id=None):
        """Persist the handoff before creating a draft; safe after lost responses."""
        identifiers = [a.content_id for a in value.assignments]
        if len(set(identifiers)) != len(identifiers):
            raise Conflict("DuplicateContentAssignment")
        if (
            value.task.policy.execution_method != "phone"
            or value.task.policy.execution_channel != "local"
        ):
            raise Conflict("ContentQueueRequiresLocalPhone")
        if value.task.policy.dispatch_policy != "phone_queue_v1":
            raise Conflict("ContentQueueRequiresDurablePolicy")
        entries, items = [], []
        for a in value.assignments:
            entry = self.get(a.content_id)
            account = self.engine.account(a.account_id)
            if (
                account["platform"] != entry["platform"]
                or a.account_id not in value.task.account_ids
            ):
                raise Conflict("ContentAccountMismatch")
            item = ItemInput(
                **entry["payload"], account_id=a.account_id, scheduled_at=a.scheduled_at
            )
            entries.append(entry)
            items.append(item)
        request = value.model_dump()
        if _existing_task_id is not None:
            request["existing_task_id"] = _existing_task_id
        fingerprint = digest(request)
        batch_id = "content-batch-" + digest(value.idempotency_key)[:32]
        with self.store.connect(write=True) as db:
            old = db.execute(
                "SELECT * FROM content_queue_batches WHERE id=?", (batch_id,)
            ).fetchone()
            if old and old["request_hash"] != fingerprint:
                raise Conflict("ContentBatchIdempotencyConflict")
            if not old:
                # Reject before reserving content: an invalid adoption must leave no batch behind.
                if _existing_task_id is not None:
                    original = db.execute(
                        "SELECT * FROM tasks WHERE id=?", (_existing_task_id,)
                    ).fetchone()
                    self._check_campaign_draft(db, original, value)
                if any(
                    db.execute(
                        "SELECT 1 FROM content_queue_members WHERE content_id=?", (i,)
                    ).fetchone()
                    for i in identifiers
                ):
                    raise Conflict("ContentAlreadyAssigned")
                db.execute(
                    "INSERT INTO content_queue_batches VALUES(?,?,?,?,?)",
                    (batch_id, fingerprint, canonical(request), None, self.engine.clock()),
                )
                for position, entry in enumerate(entries):
                    current = db.execute(
                        "SELECT content_hash FROM content_queue_entries WHERE id=?", (entry["id"],)
                    ).fetchone()
                    if current["content_hash"] != entry["content_hash"]:
                        raise Conflict("ContentChanged")
                    db.execute(
                        "INSERT INTO content_queue_members VALUES(?,?,?)",
                        (entry["id"], batch_id, position),
                    )
        if _existing_task_id is None:
            task = self.engine.create_task(value.task, content_claim=(batch_id, fingerprint))
        else:
            with self.store.connect(write=True) as db:
                batch = db.execute(
                    "SELECT * FROM content_queue_batches WHERE id=?", (batch_id,)
                ).fetchone()
                original = db.execute(
                    "SELECT * FROM tasks WHERE id=?", (_existing_task_id,)
                ).fetchone()
                if batch["task_id"] not in {None, _existing_task_id} or not original:
                    raise Conflict("ContentExistingTaskMismatch")
                if not batch["task_id"]:
                    self._check_campaign_draft(db, original, value)
                    db.execute(
                        "UPDATE content_queue_batches SET task_id=? WHERE id=? AND task_id IS NULL",
                        (_existing_task_id, batch_id),
                    )
            task = self.engine.task(_existing_task_id)
        # A concurrent retry may have populated or frozen the same draft already.
        if not task["items"]:
            try:
                self.engine.set_items(task["id"], items, content_claim=(batch_id, fingerprint))
            except Conflict:
                task = self.engine.task(task["id"])
                if not task["items"]:
                    raise
        task = self.engine.task(task["id"])
        if len(task["items"]) != len(items) or any(
            ItemInput(
                **{
                    k: v
                    for k, v in actual["payload"].items()
                    if k not in {"position", "platform", "account_label"}
                }
            ).model_dump()
            != expected.model_dump()
            for actual, expected in zip(task["items"], items)
        ):
            raise Conflict("ContentTaskPayloadChanged")
        return {
            "batch_id": batch_id,
            "task_id": task["id"],
            "state": task["state"],
            "explicit_start_required": task["state"] == "draft",
            "items": [i["id"] for i in task["items"]],
        }


def publication_status(item):
    """A display projection only; never overwrites outcomes or requeues work."""
    result = item.get("result") or {}
    status = item["status"]
    if status == "unknown" or (result.get("submission_started") and status != "confirmed"):
        state = "pending_verification"
    elif status == "confirmed":
        state = (
            "published"
            if (
                result.get("verified") is True
                and item.get("external_reference")
                and (
                    result.get("native_id")
                    or result.get("message_id")
                    or result.get("native_comment_id")
                )
            )
            else "pending_verification"
        )
    elif status in {"executing", "claimed"}:
        state = "executing"
    elif status in {"cancelled", "not_attempted"}:
        state = "cancelled" if status == "cancelled" else "queued"
    elif status == "scheduled":
        state = "pending_verification"
    else:
        state = "needs_attention"
    return {
        "item_id": item["id"],
        "status": state,
        "reason": result.get("reason"),
        "original_status": status,
    }
