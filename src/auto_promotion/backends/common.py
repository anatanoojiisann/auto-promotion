"""Private profiles, process locks and durable transport receipts."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from ..config import database_path
from ..models import PLATFORMS, canonical, digest, epoch


def observed_timestamp(value):
    if isinstance(value, datetime):
        return value.timestamp() if value.tzinfo else None
    if isinstance(value, str):
        try:
            return epoch(value)
        except (ValueError, OverflowError):
            return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return None


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


def x_api_channel(account):
    """Local configuration evidence only; never probes API or grants reply permission."""
    if account["mode"] == "simulation":
        return {"transport": "simulation", "blockers": []}
    blockers = []
    backend = "legacy" if os.getenv("AP_X_BACKEND") == "legacy" else "builtin"
    if backend == "legacy":
        blockers.append("x_api_builtin_backend_required")
    try:
        settings = read_profile("x", account["backend_label"])
    except BackendError:
        settings = {}
    official = settings.get("connection_type") == "x_api"
    if not official:
        blockers.append("x_api_account_required")
    else:
        if (
            not isinstance(settings.get("access_token"), str)
            or not settings["access_token"].strip()
        ):
            blockers.append("x_api_token_missing")
        if not str(settings.get("user_id") or "").isdecimal():
            blockers.append("x_api_user_id_missing")
    return {
        "transport": "x_api" if official and backend == "builtin" else "browser_or_unconfigured",
        "backend": backend,
        "blockers": blockers,
    }


def automatic_actions(account):
    """Account-local opt-in; existing X connections remain read-only by default."""
    if account["mode"] == "live" and account["platform"] in {"instagram", "facebook", "tiktok"}:
        try:
            profile = read_profile(account["platform"], account["backend_label"])
        except BackendError:
            return []
        expected = "tiktok_desktop" if account["platform"] == "tiktok" else "meta_desktop"
        if profile.get("connection_type") != expected:
            return []
        actions = ["comment", "reply"]
        if account["platform"] == "tiktok" and profile.get("direct_messages_enabled") is True:
            actions += ["send_direct_message", "reply_direct_message"]
        return actions
    if account["platform"] != "x" or account["mode"] != "live":
        return []
    try:
        profile = read_profile("x", account["backend_label"])
    except BackendError:
        return []
    if profile.get("connection_type") == "x_api":
        return [
            "reply"
        ]  # Every target still requires the official API preflight and submit checks.
    if profile.get("browser_channel") == "browser_use":
        return []
    return ["reply"] if "reply" in profile.get("automatic_actions", []) else []


def automatic_action_allowed(account, action, policy):
    """An owned post is a task-scoped opt-in on an already commissioned X connection."""
    allowed = account.get("automatic_actions", [])
    return action in allowed or (
        account.get("platform") == "x"
        and action == "post"
        and policy.get("allow_owned_x_posts") is True
        and policy.get("execution_channel") == "cloud"
        and "reply" in allowed
    )


def owned_x_target(target, identity):
    from urllib.parse import urlparse

    parsed = urlparse(target)
    return bool(
        isinstance(identity, str)
        and re.fullmatch(r"[a-z0-9_]{1,15}", identity.casefold())
        and parsed.scheme == "https"
        and parsed.netloc.casefold() in {"x.com", "www.x.com"}
        and not parsed.query
        and not parsed.fragment
        and parsed.path.casefold() == "/" + identity.casefold()
    )


def x_post_weight(text):
    """Conservative twitter-text v3 weights; joined emoji can be over-counted."""

    def weight(fragment):
        return sum(
            1
            if (
                ord(c) <= 4351
                or 8192 <= ord(c) <= 8205
                or 8208 <= ord(c) <= 8223
                or 8242 <= ord(c) <= 8247
            )
            else 2
            for c in fragment
        )

    total, cursor = 0, 0
    for match in re.finditer(r"https?://[^\s<>()]+", text):
        total += weight(text[cursor : match.start()])
        raw = match.group().rstrip(".,!?;:'\"")
        total += 23 + weight(match.group()[len(raw) :])
        cursor = match.end()
    return total + weight(text[cursor:])


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

    def begin(self, key, payload, *, binding=None):
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
                (
                    key,
                    digest(payload),
                    canonical(
                        {
                            "status": "unknown",
                            "verified": False,
                            **({"receipt_binding": binding} if binding else {}),
                        }
                    ),
                ),
            )
        return None

    def save(self, key, result):
        with self.connect() as db:
            db.execute("UPDATE receipts SET result=? WHERE id=?", (canonical(result), key))


def verify_claim(request):
    """A direct provider invocation must belong to an explicitly started task."""
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
        from ..queue_store import verify_job_claim

        try:
            verify_job_claim(db, request)
        except ValueError as exc:
            raise BackendError(str(exc)) from None
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
        from ..read_cooldowns import x_read_until

        if x_read_until(db, account) > time.time():
            raise BackendError("AccountReadCooldown")
        from ..account_blocks import active_block, read_private_stop

        if read_private_stop(platform, request["account"]) or active_block(
            db, account["id"], time.time()
        ):
            raise BackendError("AccountOutboundBlocked")
        task = db.execute("SELECT * FROM tasks WHERE id=?", (row["task_id"],)).fetchone()
        stop = db.execute("SELECT value FROM meta WHERE key='global_stop'").fetchone()
        if not task or task["state"] != "active" or not stop or stop[0] != "0":
            raise BackendError("ExecutionStopped")
        if task["phase"] not in ({"discovered", "starting", "ready"} if join else {"ready"}):
            raise BackendError("TaskNotStarted")
        if "campaign_id" in task.keys() and task["campaign_id"]:
            campaign = db.execute(
                "SELECT state FROM campaigns WHERE id=?", (task["campaign_id"],)
            ).fetchone()
            if not campaign or campaign[0] in {"paused", "cancelled"}:
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
            from ..interaction_intents import candidate_guard

            reason = candidate_guard(db, row["id"], now)
            if reason:
                raise BackendError(reason)
            item = json.loads(row["payload"])
            if item.get("execution_mode") == "manual":
                raise BackendError("ManualActionMustNotUseBackend")
            current_account = dict(account)
            current_account["automatic_actions"] = automatic_actions(current_account)
            if account["platform"] == "reddit" or (
                account["platform"] == "x"
                and not automatic_action_allowed(current_account, item["action"], policy)
            ):
                raise BackendError("PlatformAutomationNotEnabled")
            if account["platform"] == "x" and item["action"] == "post":
                profile = read_profile("x", account["backend_label"])
                if not item.get("target_owned") or not owned_x_target(
                    item["target"], profile["identity"]
                ):
                    raise BackendError("XOwnedTargetRequired")
            if item.get("opportunity_id"):
                from ..planner import execution_mode

                source = db.execute(
                    "SELECT payload FROM opportunities WHERE id=?", (item["opportunity_id"],)
                ).fetchone()
                if (
                    not source
                    or execution_mode(current_account, json.loads(source[0]))[0] != "automatic"
                ):
                    raise BackendError("TargetPermissionRevoked")
                content = json.loads(source[0])
                if content.get("expires_at") and now >= epoch(content["expires_at"]):
                    raise BackendError("ExecutionWindowElapsed")
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
        from ..cloud_control import CloudControl, CloudUnavailable
        from ..execution_channels import independent_scope

        context = request.get("execution_job") or {}
        independent = bool(
            context.get("channel") == "local"
            and (context.get("task_channel") or independent_scope(db))
        )
        cloud = None if independent else CloudControl.configured()
        required = db.execute(
            "SELECT value FROM meta WHERE key='cloud_control_required'"
        ).fetchone()
        if not cloud and required and required[0] == "1" and not independent:
            raise BackendError("CloudAuthorizationUnavailable")
        if (request.get("execution_job") or {}).get("channel") == "local":
            from ..adapters import AdapterRegistry
            from ..execution_channels import guard_local
            from ..store import Store

            # Recheck local ownership immediately before submission. All normal
            # task, stop, permission and payload checks above remain mandatory.
            # Submission checks read an initialized queue. Re-running schema
            # migrations here can collide with an unrelated writer and turn a
            # prepared reply into an uncertain outcome before submission.
            local_engine = Engine(
                Store(path, initialize=False), AdapterRegistry(path.parent), live_enabled=True
            )
            try:
                guard_local(local_engine, request["execution_job"], cloud)
            except CloudUnavailable:
                raise BackendError("LocalChannelAuthorizationUnavailable") from None
        elif cloud:
            attempt_id = payload.get("attempt_id")
            if join:
                attempt = db.execute(
                    "SELECT id FROM join_attempts WHERE candidate_id=?", (row["id"],)
                ).fetchone()
                attempt_id = attempt["id"] if attempt else None
            try:
                cloud.guard(request.get("execution_job"), attempt_id)
            except CloudUnavailable:
                raise BackendError("CloudAuthorizationUnavailable") from None


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
