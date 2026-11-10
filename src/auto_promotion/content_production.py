"""Device-independent production using the existing draft provider and content queue."""

import json
import uuid
from typing import Literal

from pydantic import Field, model_validator

from .content_queue import ContentDraft, ContentQueue
from .core import Conflict, NotFound
from .drafts import DraftService
from .models import AccountProfile, ItemInput, Model, canonical, digest
from .planner import content_key, render_template
from .store import Store


class ProductionInput(Model):
    idempotency_key: str = Field(min_length=1, max_length=160)
    opportunity_ids: list[str] = Field(min_length=1, max_length=100)
    profile: AccountProfile = Field(default_factory=AccountProfile)
    mode: Literal["existing_draft", "template", "model"] = "existing_draft"

    @model_validator(mode="after")
    def unique_sources(self):
        if len(set(self.opportunity_ids)) != len(self.opportunity_ids):
            raise ValueError("Duplicate production source")
        return self


class ContentProduction:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store
        self.queue = ContentQueue(engine)

    def enqueue(self, family, source_id, revision, platform, payload, context, *, db):
        """Save producer output and link in the SAME caller transaction; no tasks are created."""
        key = "producer:" + digest([family, source_id, revision])
        entry = self.queue.put(
            ContentDraft(
                platform=platform,
                idempotency_key=key,
                **{
                    k: v
                    for k, v in payload.items()
                    if k in ItemInput.model_fields and k not in {"account_id", "scheduled_at"}
                },
                context={
                    **context,
                    "producer": family,
                    "source_id": source_id,
                    "source_revision": revision,
                },
            ),
            _db=db,
        )
        db.execute(
            "INSERT OR IGNORE INTO content_producer_links VALUES(?,?,?,?)",
            (family, source_id, str(revision), entry["id"]),
        )
        return entry

    def get(self, run_id):
        run = self.store.one(
            "SELECT id,request_hash,created_at FROM content_production_runs WHERE id=?", (run_id,)
        )
        if not run:
            raise NotFound("ContentProductionNotFound")
        items = self.store.rows(
            "SELECT position,source_id,state,content_id,reason FROM content_production_items WHERE run_id=? ORDER BY position",
            (run_id,),
        )
        return {
            **run,
            "state": "completed" if all(i["state"] == "ready" for i in items) else "pending",
            "items": items,
            "execution_started": False,
        }

    def produce(self, value: ProductionInput, *, provider=None):
        raw, now = value.model_dump(), self.engine.clock()
        run_id = "production-" + digest(value.idempotency_key)[:32]
        with self.store.connect(write=True) as db:
            old = db.execute(
                "SELECT * FROM content_production_runs WHERE id=?", (run_id,)
            ).fetchone()
            if old and old["request_hash"] != digest(raw):
                raise Conflict("ContentProductionIdempotencyConflict")
            if not old:
                sources = []
                for identifier in value.opportunity_ids:
                    row = db.execute(
                        "SELECT * FROM opportunities WHERE id=?", (identifier,)
                    ).fetchone()
                    if not row:
                        raise NotFound("ProductionSourceNotFound")
                    source = json.loads(row["payload"])
                    if source["platform"] == "discord":
                        raise Conflict("ProductionPlatformOutsideSixAppScope")
                    sources.append(
                        (
                            identifier,
                            canonical(
                                {
                                    "source": source,
                                    "content_key": row["content_key"],
                                    "revision": row["revision"],
                                    "observed_at": row["updated_at"],
                                }
                            ),
                        )
                    )
                db.execute(
                    "INSERT INTO content_production_runs VALUES(?,?,?,?,?)",
                    (
                        run_id,
                        digest(raw),
                        canonical(raw),
                        canonical(value.profile.model_dump()),
                        now,
                    ),
                )
                for position, (identifier, source) in enumerate(sources):
                    db.execute(
                        "INSERT INTO content_production_items(run_id,position,source_id,source_snapshot,state) VALUES(?,?,?,?,'pending')",
                        (run_id, position, identifier, source),
                    )
        for position in range(len(value.opportunity_ids)):
            owner = uuid.uuid4().hex
            with self.store.connect(write=True) as db:
                row = db.execute(
                    "SELECT * FROM content_production_items WHERE run_id=? AND position=?",
                    (run_id, position),
                ).fetchone()
                if row["state"] == "ready" or (
                    row["state"] == "generating" and row["lease_until"] > self.engine.clock()
                ):
                    continue
                db.execute(
                    "UPDATE content_production_items SET state='generating',owner=?,lease_until=?,reason=NULL WHERE run_id=? AND position=?",
                    (owner, self.engine.clock() + 120, run_id, position),
                )
                snapshot = json.loads(row["source_snapshot"])
            source, profile = snapshot["source"], value.profile.model_dump()
            try:
                if value.mode == "existing_draft":
                    text, title = source["draft_text"], source["draft_title"]
                elif value.mode == "template":
                    text = render_template(
                        profile["reply_template"],
                        {**source, **profile, "author": source["author_id"]},
                    )
                    title = source["draft_title"]
                else:
                    generated = (provider or DraftService()).generate(profile, source)
                    text, title = generated.text, generated.title
                if not text.strip():
                    raise Conflict("ProductionTextRequired")
                if any(
                    k.casefold() in text.casefold()
                    for k in profile["blocked_keywords"]
                    if k.strip()
                ):
                    raise Conflict("ProductionBlockedKeyword")
                payload = {
                    k: source[k]
                    for k in ItemInput.model_fields
                    if k in source and k not in {"account_id", "scheduled_at"}
                }
                payload.update(
                    text=text,
                    title=title,
                    opportunity_id=row["source_id"],
                    source_author_id=source["author_id"],
                    content_key=snapshot.get("content_key") or content_key(source),
                )
                with self.store.connect(write=True) as db:
                    current = db.execute(
                        "SELECT * FROM content_production_items WHERE run_id=? AND position=?",
                        (run_id, position),
                    ).fetchone()
                    if (
                        current["state"] != "generating"
                        or current["owner"] != owner
                        or current["lease_until"] <= self.engine.clock()
                    ):
                        raise Conflict("ProductionLeaseLost")
                    entry = self.enqueue(
                        "production",
                        run_id,
                        str(position),
                        source["platform"],
                        payload,
                        {
                            "source_kind": "opportunity",
                            "opportunity_id": row["source_id"],
                            "source_hash": digest(source),
                            "opportunity_revision": snapshot["revision"],
                            "source_observed_at": snapshot["observed_at"],
                            "profile_hash": digest(profile),
                            "source_context": source["context"][:2000],
                        },
                        db=db,
                    )
                    db.execute(
                        "UPDATE content_production_items SET state='ready',content_id=?,owner=NULL,lease_until=0 WHERE run_id=? AND position=? AND owner=?",
                        (entry["id"], run_id, position, owner),
                    )
                    Store.audit(
                        db,
                        "content.produced",
                        {"run_id": run_id, "content_id": entry["id"]},
                        now=self.engine.clock(),
                    )
            except Exception as error:
                # No provider response, endpoint, credential or untrusted exception body is persisted.
                reason = (
                    str(error)
                    if isinstance(error, Conflict)
                    and str(error)
                    in {"ProductionTextRequired", "ProductionBlockedKeyword", "ProductionLeaseLost"}
                    else "ContentProductionUnavailable"
                )
                with self.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE content_production_items SET state='pending',owner=NULL,lease_until=0,reason=? WHERE run_id=? AND position=? AND owner=?",
                        (reason, run_id, position, owner),
                    )
        return self.get(run_id)


def enqueue_hf(engine, db, candidate, text):
    """The existing high-frequency model output enters the same immutable content pool."""
    if candidate.get("task_id") or candidate.get("item_id"):
        raise Conflict("ExistingInteractionMustKeepOriginalSender")
    source = candidate["source"]
    revision = digest(["hf-content/2", candidate["version"], candidate["source_hash"], text])
    entry = ContentProduction(engine).enqueue(
        "hf_candidate",
        candidate["id"],
        revision,
        "x",
        {
            "action": "reply",
            "target": source["url"],
            "text": text,
            "source_author_id": source.get("author_id", ""),
            "content_key": digest(["x", candidate["source_event_id"]]),
        },
        {
            "policy_id": candidate["policy_id"],
            "candidate_version": candidate["version"],
            "source_hash": candidate["source_hash"],
            "source_event_id": candidate["source_event_id"],
            "source_observed_at": candidate["source_event_at"],
            "source_context": source.get("text", "")[:2000],
            "source_author": source.get("author_id", ""),
        },
        db=db,
    )
    db.execute(
        "UPDATE hf_candidates SET block_reason='content_queue_awaiting_batch' WHERE id=?",
        (candidate["id"],),
    )
    return entry
