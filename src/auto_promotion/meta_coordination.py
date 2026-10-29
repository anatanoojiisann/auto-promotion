"""Share the existing IG operation lock and cooldown with older desktop flows."""

import json
import os
from pathlib import Path

from .completion_sources import timestamp
from .meta_migration import iso
from .models import canonical


class LegacyCoordination:
    def __init__(self, connection):
        paths = connection["details"].get("coordination_paths", [])
        self.path = Path(paths[0]) if paths else None
        self.lock = self.path.parent / "interaction.lock" if self.path else None

    def acquire(self, owner, expires_at):
        if not self.lock:
            return
        from .core import Conflict

        try:
            with self.lock.open("x", encoding="utf-8") as output:
                json.dump(
                    {"owner": owner, "expires_at": expires_at, "source": "auto_promotion"}, output
                )
                output.flush()
                os.fsync(output.fileno())
        except FileExistsError:
            raise Conflict("IG 原有互动锁尚未释放，保持未尝试并等待原流程") from None

    def update(self, owner, marker, *, next_at=0, receipt_path=None):
        if not self.path:
            return
        # Never overwrite a lock or state owned by another actor.
        lock = json.loads(self.lock.read_text(encoding="utf-8-sig"))
        if lock.get("owner") != owner:
            raise ValueError("LegacyInteractionLockOwnerMismatch")
        state = json.loads(self.path.read_text(encoding="utf-8-sig"))
        previous = state.get("coordination_inflight") or {}
        if previous and previous.get("owner") != owner:
            raise ValueError("LegacyInteractionInFlight")
        if marker["status"] in {"executing", "unknown"}:
            state["coordination_inflight"] = marker
        else:
            state["coordination_inflight"] = None
        if marker["status"] == "confirmed" and marker.get("published_at"):
            published_at = marker["published_at"]
            current = timestamp(state.get("last_sent_at")) or 0
            if published_at >= current:
                state["last_sent_at"] = iso(published_at)
                state["coordination_last_outbound"] = {
                    "source": "auto_promotion",
                    "batch": marker["task_id"],
                    "task_id": marker["task_id"],
                    "item_id": marker["item_id"],
                    "account": marker["identity"],
                    "reply_url": marker["external_reference"],
                    "sent_at": iso(published_at),
                    "state_path": str(receipt_path),
                }
        if next_at:
            state["next_not_before"] = iso(
                max(timestamp(state.get("next_not_before")) or 0, next_at)
            )
        # Same-directory replacement preserves the original records and status.
        temporary = self.path.with_name(self.path.name + "." + owner + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as output:
                output.write(canonical(state))
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(self.path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def release(self, owner):
        if not self.lock or not self.lock.is_file():
            return
        value = json.loads(self.lock.read_text(encoding="utf-8-sig"))
        if value.get("owner") == owner:
            self.lock.unlink()
