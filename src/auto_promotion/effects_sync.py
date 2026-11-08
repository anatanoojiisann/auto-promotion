"""Incremental, read-only export of public publication counters to the effects Worker."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .engagement import Engagement
from .models import canonical
from .statistics import REPLYABLE

ITEM_FIELDS = (
    "item_id",
    "task_id",
    "task_name",
    "account_id",
    "platform",
    "account_label",
    "text",
    "title",
    "target",
    "action",
    "external_reference",
    "published_at",
    "capability",
    "status",
    "metrics",
    "estimated",
    "delta",
    "observed_at",
    "previous_observed_at",
    "next_at",
)


class SyncError(RuntimeError):
    pass


class ReadStore:
    def __init__(self, connection):
        self.connection = connection

    def rows(self, query, parameters=()):
        return [dict(row) for row in self.connection.execute(query, parameters)]

    def one(self, query, parameters=()):
        row = self.connection.execute(query, parameters).fetchone()
        return dict(row) if row else None


def read_snapshot(database, previous):
    connection = sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        store = ReadStore(connection)
        binding = store.one("SELECT value FROM meta WHERE key='execution_store_id'")
        source_id = (
            binding["value"]
            if binding
            else hashlib.sha256(str(Path(database).resolve()).encode()).hexdigest()
        )
        if previous.get("source_id") not in (None, source_id):
            raise SyncError("EffectsSourceChanged")
        cursor = store.one("SELECT COALESCE(MAX(id),0) cursor FROM engagement_snapshots")["cursor"]
        if cursor < previous.get("source_cursor", 0):
            raise SyncError("EffectsSourceRewound")
        captured = time.time()
        engagement = Engagement(SimpleNamespace(store=store, clock=lambda: captured))
        items = [
            {key: item[key] for key in ITEM_FIELDS}
            for item in engagement.items()
            if item["action"] in REPLYABLE
        ]
        active = {item["item_id"] for item in items}
        digests = {
            item["item_id"]: hashlib.sha256(canonical(item).encode()).hexdigest() for item in items
        }
        changed = [
            item
            for item in items
            if previous.get("digests", {}).get(item["item_id"]) != digests[item["item_id"]]
        ]
        new_items = active - set(previous.get("digests", {}))
        samples = []
        for row in store.rows(
            "SELECT id,item_id,observed_at,metrics,estimated,status FROM engagement_snapshots ORDER BY id"
        ):
            if row["item_id"] in active and (
                row["id"] > previous.get("source_cursor", 0) or row["item_id"] in new_items
            ):
                samples.append(
                    {
                        "source_id": row["id"],
                        "item_id": row["item_id"],
                        "observed_at": row["observed_at"],
                        "metrics": json.loads(row["metrics"]),
                        "estimated": json.loads(row["estimated"]),
                        "status": row["status"],
                    }
                )
        return {
            "source_id": source_id,
            "sync_id": str(uuid.uuid4()),
            "captured_at": captured,
            "source_cursor": cursor,
            "items": changed,
            "samples": samples,
            "manifest": sorted(active),
            "digests": digests,
        }
    finally:
        connection.close()


def chunks(values, maximum, bytes_limit=900000):
    batch, size = [], 0
    for value in values:
        encoded_size = len(canonical(value).encode("utf-8"))
        if batch and (len(batch) >= maximum or size + encoded_size > bytes_limit):
            yield batch
            batch, size = [], 0
        if encoded_size > bytes_limit:
            raise SyncError("EffectsRecordTooLarge")
        batch.append(value)
        size += encoded_size
    if batch:
        yield batch


def post(config, payload):
    destination = config["url"].rstrip("/")
    parsed = urlparse(destination)
    if (
        (
            parsed.scheme != "https"
            and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"})
        )
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise SyncError("InvalidEffectsDestination")
    request = Request(
        destination + "/internal/sync",
        data=canonical(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + config["sync_token"],
            "Content-Type": "application/json",
            "User-Agent": "AutoPromotionEffectsSync/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=25) as response:
            result = json.load(response)
        if not result.get("accepted"):
            raise SyncError("EffectsSyncNotAccepted")
        return result
    except HTTPError as exc:
        code = "EffectsHttpError" + str(exc.code)
        try:
            candidate = json.loads(exc.read(4096)).get("error")
            if isinstance(candidate, str) and candidate.isidentifier() and len(candidate) < 100:
                code = candidate
        except (ValueError, OSError):
            pass
        finally:
            exc.close()
        raise SyncError(code) from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise SyncError("EffectsTransportUnavailable") from None


def save_state(path, state):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(canonical(state), encoding="utf-8")
    os.replace(temporary, path)


def sync_once(database, config, state_path, *, sender=post):
    state_path = Path(state_path)
    previous = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    snapshot = read_snapshot(database, previous)
    common = {
        key: snapshot[key] for key in ("source_id", "sync_id", "captured_at", "source_cursor")
    }
    for values in chunks(snapshot["items"], 100):
        sender(config, {**common, "items": values, "samples": [], "complete": False})
    for values in chunks(snapshot["samples"], 400):
        sender(config, {**common, "items": [], "samples": values, "complete": False})
    result = sender(
        config,
        {**common, "items": [], "samples": [], "complete": True, "manifest": snapshot["manifest"]},
    )
    if not result.get("complete") or result.get("source_cursor") != snapshot["source_cursor"]:
        raise SyncError("EffectsSyncAcknowledgementMismatch")
    save_state(
        state_path,
        {
            "source_id": snapshot["source_id"],
            "source_cursor": snapshot["source_cursor"],
            "digests": snapshot["digests"],
            "last_synced_at": time.time(),
        },
    )
    return {
        "synced": True,
        "items": len(snapshot["manifest"]),
        "changed_items": len(snapshot["items"]),
        "uploaded_samples": len(snapshot["samples"]),
        "source_cursor": snapshot["source_cursor"],
        "cloud_samples": result.get("sample_count"),
    }


@contextmanager
def sync_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
                handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise SyncError("EffectsSyncAlreadyRunning") from None
        else:
            import fcntl

            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise SyncError("EffectsSyncAlreadyRunning") from None
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    log = logging.getLogger("effects_sync")
    log.setLevel(logging.INFO)
    log.addHandler(
        RotatingFileHandler(
            args.config.with_suffix(".log"), maxBytes=1000000, backupCount=2, encoding="utf-8"
        )
    )
    state_path = args.config.with_suffix(".state.json")
    with sync_lock(args.config.with_suffix(".lock")):
        while True:
            try:
                result = sync_once(args.db, config, state_path)
            except Exception as exc:
                result = {
                    "synced": False,
                    "error": str(exc) if isinstance(exc, SyncError) else type(exc).__name__,
                }
            result["at"] = time.time()
            log.info(canonical(result))
            if args.once:
                print(canonical(result))
                return 0 if result["synced"] else 1
            time.sleep(max(60, int(config.get("interval_seconds", 300))))


if __name__ == "__main__":
    raise SystemExit(main())
