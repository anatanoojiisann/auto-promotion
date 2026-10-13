"""Private profiles, process locks and durable transport receipts."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from ..config import database_path
from ..models import PLATFORMS, canonical, digest, epoch


class BackendError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.safe_code = code


def profile_path(platform, label, *, accounts_dir=None):
    if platform not in PLATFORMS or not label.strip():
        raise BackendError("InvalidAccountLabel")
    root = Path(
        accounts_dir or os.getenv("AP_ACCOUNTS_DIR", database_path().parent / "accounts")
    ).resolve()
    slot = hashlib.sha256(label.encode("utf-8")).hexdigest()[:32]
    return root / platform / slot


def read_profile(platform, label, *, accounts_dir=None):
    path = profile_path(platform, label, accounts_dir=accounts_dir) / "credentials.json"
    if not path.is_file():
        raise BackendError("AccountNotConfigured")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("label") != label or not data.get("identity"):
            raise ValueError
        return data
    except (ValueError, TypeError):
        raise BackendError("InvalidAccountConfiguration") from None


def save_profile(platform, label, data, *, accounts_dir=None):
    root = profile_path(platform, label, accounts_dir=accounts_dir)
    root.mkdir(parents=True, exist_ok=True)
    target = root / "credentials.json"
    if (
        target.exists()
        and read_profile(platform, label, accounts_dir=accounts_dir)["identity"] != data["identity"]
    ):
        raise BackendError("ChangedIdentityRequiresNewAccountLabel")
    staging = root / "credentials.tmp"
    with staging.open("w", encoding="utf-8") as output:
        output.write(canonical({**data, "label": label}))
    staging.chmod(0o600)
    staging.replace(target)


@contextlib.contextmanager
def account_lock(platform, label, *, accounts_dir=None):
    """OS releases this lock even when a provider process is killed."""
    root = profile_path(platform, label, accounts_dir=accounts_dir)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "account.lock").open("a+b") as handle:
        handle.seek(0)
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise BackendError("AccountBusy") from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


class Receipts:
    def __init__(self, platform, label):
        root = profile_path(platform, label)
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "receipts.sqlite"
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS receipts("
                "id TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,result TEXT NOT NULL)"
            )

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA synchronous=FULL")
            yield db
            db.commit()
        finally:
            db.close()

    def read(self, key):
        with self.connect() as db:
            row = db.execute("SELECT result FROM receipts WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def begin(self, key, payload):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload_hash,result FROM receipts WHERE id=?", (key,)
            ).fetchone()
            if row:
                if row[0] != digest(payload):
                    raise BackendError("ReceiptPayloadMismatch")
                return json.loads(row[1])
            db.execute(
                "INSERT INTO receipts VALUES(?,?,?)",
                (key, digest(payload), canonical({"status": "unknown", "verified": False})),
            )
        return None

    def save(self, key, result):
        with self.connect() as db:
            db.execute("UPDATE receipts SET result=? WHERE id=?", (canonical(result), key))


def verify_claim(request):
    """A direct provider invocation cannot bypass durable task approvals."""
    platform = request["platform"]
    if (
        os.getenv("AUTO_PROMOTION_LIVE_WRITES") != "1"
        or os.getenv("AP_" + platform.upper() + "_OWNER") != "auto-promotion"
    ):
        raise BackendError("LiveWriterNotCommissioned")
    path = Path(request["database"]).resolve()
    from ..core import Engine

    with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        payload = request["payload"]
        join = request["operation"] == "join"
        row = db.execute(
            "SELECT * FROM candidates WHERE id=?" if join else "SELECT * FROM items WHERE id=?",
            (payload.get("candidate_id") if join else payload.get("item_id"),),
        ).fetchone()
        if not row or row["account_id"] != request["account_id"]:
            raise BackendError("InvalidExecutionClaim")
        if row["state" if join else "status"] != "executing":
            raise BackendError("ExecutionClaimNotActive")
        account = db.execute("SELECT * FROM accounts WHERE id=?", (row["account_id"],)).fetchone()
        if (
            account["mode"] != "live"
            or account["platform"] != platform
            or account["backend_label"] != request["account"]
            or account["status"] != "healthy"
            or account["disabled"]
            or account["cooldown_until"] > time.time()
        ):
            raise BackendError("AccountNotWritable")
        task = db.execute("SELECT * FROM tasks WHERE id=?", (row["task_id"],)).fetchone()
        stop = db.execute("SELECT value FROM meta WHERE key='global_stop'").fetchone()
        if not task or task["state"] != "active" or not stop or stop[0] != "0":
            raise BackendError("ExecutionStopped")
        policy = Engine._integrity(None, db, task, "discovery_join" if join else "outbound")
        now = time.time()
        if not epoch(policy["starts_at"]) <= now < epoch(policy["ends_at"]):
            raise BackendError("ExecutionWindowElapsed")
        if join:
            if row["decision"] != "qualified" or row["target"] != payload["target"]:
                raise BackendError("JoinTargetNotApproved")
            if not db.execute(
                "SELECT 1 FROM join_attempts WHERE candidate_id=? AND outcome='claimed'",
                (row["id"],),
            ).fetchone():
                raise BackendError("InvalidExecutionClaim")
        else:
            if row["attempt_id"] != payload.get("attempt_id"):
                raise BackendError("InvalidExecutionClaim")
            item = json.loads(row["payload"])
            if item != payload["payload"] or digest(item) != row["payload_hash"]:
                raise BackendError("ExecutionPayloadMismatch")
            due = epoch(item["scheduled_at"])
            if item["action"].startswith("schedule_"):
                if due <= now:
                    raise BackendError("ExecutionWindowElapsed")
            elif not due <= now <= due + 900:
                raise BackendError("ExecutionWindowElapsed")
            for media in item["media"]:
                actual = hashlib.sha256(Path(media["path"]).read_bytes()).hexdigest()
                if actual != media["sha256"]:
                    raise BackendError("ApprovedMediaChanged")


def language_evidence(text):
    """Only strong script evidence qualifies automatically; mixed/unknown is rejected."""
    letters = re.findall(r"[^\W\d_]", text, re.UNICODE)
    if not letters:
        return "unknown", 0.0
    chinese = sum("\u4e00" <= c <= "\u9fff" for c in letters)
    if chinese / len(letters) >= 0.5:
        return "zh", 0.85
    # Latin script alone does not prove English. Leave uncertain candidates for evidence entry.
    return "unknown", 0.0
