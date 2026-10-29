"""Read-only field evidence and explicitly isolated synthetic regression. Stdlib only.

Never imports application modules in collect/observe mode. No automatic .env loading,
schema initialization, task activation, receipt recovery, or platform requests.
"""

import argparse
import ast
import collections
import hashlib
import http.client
import importlib.util
import json
import math
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
STATES = {
    "pending",
    "leased",
    "completed",
    "confirmed",
    "unknown",
    "failed",
    "failed_known",
    "missed",
    "cancelled",
    "joined",
    "already_joined",
    "pending_admin_approval",
    "executing",
    "not_attempted",
    "manual_publication_required",
    "waiting_dependency",
    "waiting_schedule",
    "waiting_membership",
    "paused",
    "running",
    "stopped",
    "draft",
    "active",
}
FAULT_CASES = (
    "test_startup_resilience.StartupResilienceTests",
    "test_runtime_reliability.RuntimeReliabilityTests",
    "test_native_backends.ClaimTests",
    "test_persistent_pools.PersistentPoolTests.test_two_consumers_claim_only_once",
    "test_persistent_pools.PersistentPoolTests.test_expired_lease_with_attempt_does_not_requeue",
    "test_persistent_pools.PersistentPoolTests.test_unknown_send_is_never_replayed_after_worker_restart",
)
# Explicit cases below are reviewed mock-only tests. No dynamic discovery from a field host.
FAULT_MODULES = (
    "test_startup_resilience",
    "test_runtime_reliability",
    "test_native_backends",
    "test_persistent_pools",
)
ERROR_CODES = (
    "OperationalError",
    "database is locked",
    "LeaseExpired",
    "CloudWriteQuotaExceeded",
    "CloudWorkerException",
    "CloudTransportUnavailable",
    "PersistentJobLeaseRequired",
)


def check(status, code, **evidence):
    return {"status": status, "code": code, **evidence}


def safe_error(exc):
    # Raw exceptions can contain URLs, SQL, paths, tokens, or response bodies.
    return check("unverified", type(exc).__name__)


def fingerprint(root=ROOT):
    package = (root / "src" / "auto_promotion").resolve()
    # Read the literal manifest; never import or execute application modules.
    source = (package / "runtime_identity.py").resolve()
    if not source.is_relative_to(package):
        raise ValueError("RuntimeSourceManifestInvalid")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    manifest = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "FINGERPRINT_FILES"
            for target in node.targets
        )
    ]
    if len(manifest) != 1:
        raise ValueError("RuntimeSourceManifestUnavailable")
    names = ast.literal_eval(manifest[0])
    if (
        not isinstance(names, tuple)
        or not 1 <= len(names) <= 256
        or any(
            not isinstance(name, str)
            or len(name) > 100
            or Path(name).suffix not in {".py", ".js", ".html", ".css"}
            or not (package / name).resolve().is_relative_to(package)
            for name in names
        )
    ):
        raise ValueError("RuntimeSourceManifestInvalid")
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode())
        digest.update((package / name).read_bytes())
    return digest.hexdigest()


def git_evidence(root=ROOT):
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR") if key in os.environ}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    try:
        base = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(root)]
        head = (
            subprocess.run(
                base + ["rev-parse", "HEAD"], env=env, capture_output=True, timeout=5, check=True
            )
            .stdout.decode()
            .strip()
        )
        if not re.fullmatch(r"[a-f0-9]{40}", head):
            return check("unverified", "InvalidGitHead")
        dirty = subprocess.run(
            base + ["status", "--porcelain", "-z"],
            env=env,
            capture_output=True,
            timeout=5,
            check=True,
        ).stdout
        return check(
            "passed",
            "DiskVersionOnly",
            commit=head,
            dirty=bool(dirty),
            source_fingerprint=fingerprint(root),
        )
    except (OSError, subprocess.SubprocessError, ValueError, SyntaxError) as exc:
        return safe_error(exc)


def runtime_evidence(value, disk):
    if not isinstance(value, dict):
        return check("unverified", "StartupFingerprintMissing")
    digest, commit, started = (
        value.get(k) for k in ("source_fingerprint", "git_commit_at_start", "started_at")
    )
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        return check("unverified", "StartupFingerprintMissing")
    if not isinstance(started, (int, float)) or not 0 < started <= time.time() + 60:
        return check("unverified", "StartupTimeInvalid")
    if not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}", commit):
        commit = None
    matches = digest == disk.get("source_fingerprint") and commit == disk.get("commit")
    status = "passed" if matches and not disk.get("dirty") else "unverified"
    return check(
        status,
        "StartupEvidenceMatchesDisk" if status == "passed" else "LoadedVersionNotEstablished",
        started_at=started,
        commit=commit,
        source_fingerprint=digest,
        coverage="api_or_service_host_only",
    )


def loopback_health(url, disk, timeout=2):
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.path != "/health"
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            return check("failed", "OnlyLiteralLoopbackHealthAllowed")
        with closing(
            http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=timeout)
        ) as conn:
            conn.request("GET", "/health", headers={"Accept": "application/json"})
            response = conn.getresponse()
            if response.status != 200:
                return check("unverified", "HealthHTTPStatus", http_status=response.status)
            body = response.read(65537)
            if len(body) > 65536:
                return check("unverified", "HealthTooLarge")
            value = json.loads(body)
        if not isinstance(value, dict):
            return check("unverified", "HealthInvalid")
        sender = value.get("background", {}).get("sender", {})
        runtime = runtime_evidence(value.get("runtime"), disk)
        if sender.get("enabled") is not True or sender.get("alive") is not True:
            status, code = "failed", "SenderNotProvenAlive"
        elif runtime["status"] != "passed":
            status, code = "unverified", "RunningVersionUnverified"
        else:
            status, code = "passed", "ParentTaskAliveOnly"
        return check(
            status,
            code,
            sender_enabled=sender.get("enabled") is True,
            sender_alive=sender.get("alive") is True,
            runtime=runtime,
            live_writes=value.get("live_writes")
            if isinstance(value.get("live_writes"), bool)
            else None,
        )
    except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException) as exc:
        return safe_error(exc)


WINDOWS_METADATA = r"""
$ErrorActionPreference='Stop'
$p=@(Get-CimInstance Win32_Process -OperationTimeoutSec 5 -Filter "Name LIKE 'python%' OR Name LIKE 'auto-promotion%'" |
  Select-Object ProcessId,Name,@{n='Started';e={$_.CreationDate.ToUniversalTime().ToString('o')}})
$s=@(Get-CimInstance Win32_Service -OperationTimeoutSec 5 -Filter "Name LIKE '%auto%promotion%'" |
  Select-Object Name,State,ProcessId,StartMode)
$t=@(Get-CimInstance -Namespace 'Root/Microsoft/Windows/TaskScheduler' -ClassName MSFT_ScheduledTask `
  -OperationTimeoutSec 5 -Filter "TaskName LIKE 'auto%promotion%'" |
  Select-Object TaskName,State)
@{processes=$p;services=$s;scheduled_tasks=$t} | ConvertTo-Json -Depth 4 -Compress
"""


def windows_evidence():
    if sys.platform != "win32":
        return check("skipped", "WindowsHostRequired")
    try:
        # Fixed script: no CommandLine, service PathName, task Actions, user or environment.
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[Console]::OutputEncoding=[Text.UTF8Encoding]::new();" + WINDOWS_METADATA,
            ],
            capture_output=True,
            timeout=20,
            check=True,
        )
        if len(result.stdout) > 1048576:
            return check("unverified", "MetadataTooLarge")
        data = json.loads(result.stdout.decode("utf-8-sig"))
        out = {}
        for group in ("processes", "services", "scheduled_tasks"):
            rows = data.get(group)
            if not isinstance(rows, list):
                return check("unverified", "MetadataShapeInvalid")
            sanitized = []
            for row in rows[:200]:
                item = {
                    "name_hash": hashlib.sha256(
                        str(row.get("Name", row.get("TaskName", ""))).encode()
                    ).hexdigest()[:16]
                }
                for key in ("ProcessId",):
                    if isinstance(row.get(key), int):
                        item["pid"] = row[key]
                state = row.get("State")
                if state in {"Running", "Stopped", "Ready", "Disabled", 0, 1, 2, 3, 4}:
                    item["state"] = state
                started = row.get("Started")
                if isinstance(started, str) and re.fullmatch(r"[0-9T:+.Z-]{19,40}", started):
                    item["started_at"] = started
                sanitized.append(item)
            out[group] = sanitized
        return check("passed", "MetadataOnlyNotHealth", **out)
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
        return safe_error(exc)


def columns(db, table):
    return {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}


class EvidenceUnavailable(ValueError):
    pass


@contextmanager
def read_only_database(path):
    path = Path(path).resolve()
    if re.search(r"(?:^|[._-])(session|credentials?|cookies?|keys?)(?:[._-]|$)", path.name, re.I):
        raise EvidenceUnavailable("CredentialDatabaseForbidden")
    if not path.is_file():
        raise EvidenceUnavailable("DatabaseMissing")
    with path.open("rb") as source:
        header = source.read(20)
    if not header.startswith(b"SQLite format 3\x00"):
        raise EvidenceUnavailable("NotSQLiteDatabase")
    if header[18:20] == b"\x02\x02" and not all(
        Path(str(path) + suffix).is_file() for suffix in ("-wal", "-shm")
    ):
        raise EvidenceUnavailable("WALSidecarsRequiredForSafeRead")
    # Never immutable=1: that would hide committed rows in an active WAL.
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.25)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA busy_timeout=250")
        db.execute("BEGIN")
        yield db


def database_evidence(path, task_id=None, now=None):
    now = time.time() if now is None else now
    path = Path(path).resolve()
    try:
        with read_only_database(path) as db:
            required = {
                "execution_jobs": {
                    "state",
                    "task_id",
                    "available_at",
                    "expires_at",
                    "updated_at",
                    "lease_until",
                },
                "tasks": {"id", "state", "phase", "policy"},
                "meta": {"key", "value"},
            }
            if any(not fields <= columns(db, table) for table, fields in required.items()):
                return check("unverified", "LegacyOrIncompleteSchema")
            revision = db.execute(
                "SELECT value FROM meta WHERE key='store_schema_revision'"
            ).fetchone()
            if not revision or revision[0] != "1":
                return check("unverified", "StoreSchemaRevisionUnverified")
            where, args = ("WHERE task_id=?", (task_id,)) if task_id else ("", ())
            counts = collections.Counter()
            for state, count in db.execute(
                f"SELECT state,COUNT(*) FROM execution_jobs {where} GROUP BY state", args
            ):
                counts[state if state in STATES else "other"] += count
            predicate = "task_id=? AND " if task_id else ""
            base = (task_id,) if task_id else ()
            timing = db.execute(
                "SELECT SUM(state='pending' AND available_at>?),SUM(state='pending' AND expires_at<=?),"
                "SUM(state='pending' AND available_at<=? AND expires_at>?),"
                "SUM(state='leased' AND lease_until>?),MAX(updated_at) FROM execution_jobs WHERE "
                + predicate
                + "1=1",
                (now, now, now, now, now) + base,
            ).fetchone()
            stop = db.execute("SELECT value FROM meta WHERE key='global_stop'").fetchone()
            evidence = {
                "counts": dict(counts),
                "future": timing[0] or 0,
                "expired": timing[1] or 0,
                "due_pending": timing[2] or 0,
                "active_leases": timing[3] or 0,
                "last_queue_update": timing[4]
                if isinstance(timing[4], (int, float)) and math.isfinite(timing[4])
                else None,
                "global_stop": (stop[0] == "1") if stop and stop[0] in {"0", "1"} else None,
            }
            if {"heartbeat_at", "state"} <= columns(db, "pool_workers"):
                evidence["recent_pool_heartbeats"] = db.execute(
                    "SELECT COUNT(*) FROM pool_workers WHERE state='running' AND heartbeat_at>?",
                    (now - 60,),
                ).fetchone()[0]
            if task_id:
                row = db.execute(
                    "SELECT state,phase,CASE WHEN json_valid(policy) THEN json_extract(policy,'$.starts_at') END,"
                    "CASE WHEN json_valid(policy) THEN json_extract(policy,'$.ends_at') END FROM tasks WHERE id=?",
                    (task_id,),
                ).fetchone()
                if row is None:
                    return check("unverified", "TaskMissing")
                evidence["task"] = {
                    "id_hash": hashlib.sha256(task_id.encode()).hexdigest()[:16],
                    "state": row[0] if row[0] in STATES else "other",
                    "phase": row[1]
                    if row[1]
                    in {
                        "review",
                        "discovery",
                        "executing",
                        "completed",
                        "waiting_membership",
                        "paused",
                        "starting",
                        "ready",
                        "discovered",
                    }
                    else "other",
                    "starts_at": parse_time(row[2]),
                    "ends_at": parse_time(row[3]),
                }
            if {"kind", "at"} <= columns(db, "audit"):
                evidence["recent_error_events"] = db.execute(
                    "SELECT COUNT(*) FROM audit WHERE at>? AND kind IN ('worker.error','statistics.error','pool.error')",
                    (now - 3600,),
                ).fetchone()[0]
            return check("passed", "ReadOnlyQueueSnapshotNotRecovery", **evidence)
    except EvidenceUnavailable as exc:
        return check("unverified", str(exc))
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        return safe_error(exc)


def receipt_evidence(database, receipt_database, task_id, item_id):
    if not all((database, receipt_database, task_id, item_id)):
        return check("failed", "ExplicitDatabasesTaskAndItemRequired")
    try:
        with read_only_database(database) as db, read_only_database(receipt_database) as private:
            if not {"id", "task_id", "status", "attempt_id", "external_reference"} <= columns(
                db, "items"
            ) or not {"id", "payload_hash", "result"} <= columns(private, "receipts"):
                return check("unverified", "ReceiptSchemaUnverified")
            item = db.execute(
                "SELECT status,attempt_id IS NOT NULL,COALESCE(length(external_reference),0)>0 FROM items WHERE task_id=? AND id=?",
                (task_id, item_id),
            ).fetchone()
            if item is None:
                return check("unverified", "SelectedItemMissing")
            receipt = private.execute(
                "SELECT CASE WHEN json_valid(result) THEN json_extract(result,'$.status') END,"
                "CASE WHEN json_valid(result) THEN json_type(result,'$.verified')='true' END,"
                "CASE WHEN json_valid(result) THEN json_type(result,'$.external_reference')='text' AND COALESCE(length(json_extract(result,'$.external_reference')),0)>0 END,"
                "length(payload_hash)=64 FROM receipts WHERE id=?",
                ("execute:" + item_id,),
            ).fetchone()
            if receipt is None:
                return check("unverified", "SelectedReceiptMissing")
            evidence = {
                "item_id_hash": hashlib.sha256(item_id.encode()).hexdigest()[:16],
                "queue_status": item[0] if item[0] in STATES else "other",
                "queue_has_attempt": bool(item[1]),
                "queue_has_external_reference": bool(item[2]),
                "private_status": receipt[0] if receipt[0] in STATES else "other",
                "private_verified": receipt[1] == 1,
                "private_has_external_reference": receipt[2] == 1,
                "private_payload_hash_present": receipt[3] == 1,
                "strict_binding_verified": False,
                "atomic_cross_database_snapshot": False,
            }
            mismatch = item[0] == "unknown" and receipt[0] == "confirmed" and receipt[1] == 1
            return check(
                "unverified",
                "ReadOnlyReconciliationCandidate" if mismatch else "ReceiptBindingRequiresReview",
                **evidence,
            )
    except EvidenceUnavailable as exc:
        return check("unverified", str(exc))
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        return safe_error(exc)


def parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def log_evidence(path, since, until):
    path = Path(path)
    if path.suffix.lower() not in {".log", ".txt", ".jsonl"} or any(
        part.lower() in {"accounts", "profiles", ".ssh", ".aws", ".env"} for part in path.parts
    ):
        return check("failed", "OnlyExplicitDiagnosticLogsAllowed")
    try:
        with path.open("rb") as source:
            source.seek(max(0, path.stat().st_size - 65536))
            lines = source.read(65536).decode("utf-8", errors="replace").splitlines()
        counts = collections.Counter()
        unknown_time = 0
        for line in lines:
            codes = [code for code in ERROR_CODES if code in line]
            if not codes:
                continue
            match = re.search(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)", line)
            at = parse_time(match.group()) if match else None
            if at is None:
                unknown_time += 1
            elif since <= at <= until:
                counts.update(codes)
        return check(
            "passed",
            "TailAllowlistedCodesOnly",
            window_counts=dict(counts),
            unknown_time_lines=unknown_time,
            tail_bytes_limit=65536,
        )
    except OSError as exc:
        return safe_error(exc)


def observe(
    database,
    task_id,
    since,
    until,
    duration,
    interval,
    deadline,
    *,
    reader=database_evidence,
    clock=time.time,
    sleep=time.sleep,
):
    if (
        not task_id
        or not 0 < duration <= 300
        or not 1 <= interval <= 60
        or since >= until
        or deadline <= clock()
        or deadline > clock() + 3600
    ):
        return check("failed", "ExplicitBoundedTaskWindowRequired")
    started = clock()
    end = min(started + duration, deadline)
    samples = []
    for _ in range(min(301, int(duration / interval) + 2)):
        samples.append(reader(database, task_id, now=clock()))
        remaining = end - clock()
        if remaining <= 0:
            break
        sleep(min(interval, remaining))
    if any(sample["status"] != "passed" for sample in samples):
        return check("unverified", "IncompleteObservation", samples=len(samples))
    last = samples[-1]
    changed = any(
        s.get("counts") != samples[0].get("counts")
        or s.get("last_queue_update") != samples[0].get("last_queue_update")
        for s in samples[1:]
    )
    task = last.get("task", {})
    if any(sample.get("counts", {}).get("unknown") for sample in samples):
        code, status = "UnknownReadOnlyReconciliationRequired", "unverified"
    elif any(
        sample.get("task", {}).get("starts_at") is None
        or sample.get("task", {}).get("ends_at") is None
        for sample in samples
    ):
        code, status = "TaskWindowUnverified", "unverified"
    elif any(sample["task"]["ends_at"] <= sample["task"]["starts_at"] for sample in samples):
        code, status = "TaskWindowInvalid", "unverified"
    elif clock() > until or clock() >= task["ends_at"]:
        code, status = "WindowExpiredNoAutomaticResume", "unverified"
    elif any(
        any(sample.get("counts", {}).get(state) for state in ("failed", "failed_known", "missed"))
        or sample.get("task", {}).get("state") in {"failed", "missed", "cancelled"}
        for sample in samples
    ):
        code, status = "UnsuccessfulQueueOutcomesNeedReview", "unverified"
    elif (
        last.get("counts", {}).get("pending_admin_approval")
        or last.get("counts", {}).get("waiting_dependency")
        or task.get("state") in {"paused", "draft"}
    ) or task.get("phase") in {
        "review",
        "waiting_membership",
        "discovery",
    }:
        code, status = "WaitingApprovalOrDependency", "unverified"
    elif not last.get("due_pending") and (
        last.get("future") or clock() < since or task["starts_at"] > clock()
    ):
        code, status = "FutureWork", "passed"
    elif changed:
        code, status = "QueueChangedNotDeliveryProof", "unverified"
    elif (
        last.get("due_pending")
        and not last.get("active_leases")
        and last.get("global_stop") is False
        and task.get("state") == "active"
        and task.get("starts_at") is not None
        and task.get("ends_at") is not None
    ):
        code, status = "SuspectedStallNeedsHostAndDependencyEvidence", "unverified"
    else:
        code, status = "NoDeliveryConclusion", "unverified"
    return check(
        status,
        code,
        samples=len(samples),
        observed_seconds=max(0, clock() - started),
        queue_changed=changed,
        window={"since": since, "until": until},
        final=last,
    )


def faults(timeout=180):
    """Reviewed source/test copies, minimal env, temporary worktree; no production imports here."""
    missing = [
        name
        for name in ("fastapi", "pydantic", "telethon", "httpx")
        if importlib.util.find_spec(name) is None
    ]
    if missing:
        return check("unverified", "SyntheticDependenciesMissing", missing=missing)
    with tempfile.TemporaryDirectory(prefix="ap-windows-验证-") as directory:
        root = Path(directory)

        def ignore(folder, names):
            return [
                name
                for name in names
                if name == "__pycache__"
                or (
                    not (Path(folder) / name).is_dir()
                    and Path(name).suffix not in {".py", ".html", ".css", ".js"}
                )
            ]

        shutil.copytree(ROOT / "src", root / "src", ignore=ignore)
        (root / "tests").mkdir()
        for name in FAULT_MODULES:
            shutil.copyfile(ROOT / "tests" / (name + ".py"), root / "tests" / (name + ".py"))
        guard = root / "sitecustomize.py"
        guard.write_text(
            """import ipaddress, sys
def guard(event, args):
    if event in {"socket.connect", "socket.bind"}:
        address=args[1]
        try: local=isinstance(address, tuple) and ipaddress.ip_address(address[0]).is_loopback
        except ValueError: local=False
        if not local: raise PermissionError("SyntheticLoopbackOnly")
sys.addaudithook(guard)
""",
            encoding="utf-8",
        )
        shutil.copyfile(guard, root / "src" / "sitecustomize.py")
        program = root / "run_faults.py"
        program.write_text(
            """import json, sys, unittest
from pathlib import Path
root=Path(__file__).parent
sys.path.insert(0,str(root/"src"));sys.path.insert(0,str(root/"tests"))
suite=unittest.TestLoader().loadTestsFromNames(json.loads(sys.argv[1]))
result=unittest.TestResult();suite.run(result)
print(json.dumps({"tests":result.testsRun,"failed":len(result.failures),"errors":len(result.errors),"skipped":len(result.skipped),"failure_cases":[t.id() for t,_ in result.failures],"error_cases":[t.id() for t,_ in result.errors],"skipped_cases":[t.id() for t,_ in result.skipped]}))
""",
            encoding="utf-8",
        )
        env = {
            key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR") if key in os.environ
        }
        env.update(
            HOME=directory,
            USERPROFILE=directory,
            APPDATA=str(root / "appdata"),
            LOCALAPPDATA=str(root / "localappdata"),
            TEMP=directory,
            TMP=directory,
            TMPDIR=directory,
            PYTHONPATH=directory,
            PYTHONIOENCODING="utf-8",
            AP_DB=str(root / "unused-default.db"),
            AP_ACCOUNTS_DIR=str(root / "accounts"),
            AUTO_PROMOTION_WORKSPACE=directory,
            AUTO_PROMOTION_LIVE_WRITES="0",
            AP_SYNTHETIC_VERIFICATION="1",
        )
        try:
            result = subprocess.run(
                [sys.executable, "-s", str(program), json.dumps(FAULT_CASES)],
                cwd=root,
                env=env,
                capture_output=True,
                timeout=timeout,
                check=True,
            )
            value = json.loads(result.stdout.decode("utf-8").splitlines()[-1])
            status = (
                "failed"
                if value["failed"] or value["errors"]
                else "unverified"
                if value["skipped"]
                else "passed"
            )
            return check(status, "SyntheticTestsNotFieldRecovery", **value)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            return safe_error(exc)


def summarize(checks):
    return {
        status: [name for name, value in checks.items() if value["status"] == status]
        for status in ("passed", "failed", "skipped", "unverified")
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", nargs="?", choices=("collect", "faults", "observe"), default="collect"
    )
    parser.add_argument(
        "--database", type=Path, help="Explicit business DB, read-only; never guessed"
    )
    parser.add_argument("--health-url", default="http://127.0.0.1:8780/health")
    parser.add_argument("--task-id")
    parser.add_argument("--item-id", help="Selected item for explicit private receipt comparison")
    parser.add_argument(
        "--receipt-database", type=Path, help="Explicit receipts.sqlite only; never scan profiles"
    )
    parser.add_argument(
        "--output", type=Path, help="Create a new UTF-8 JSON report, never overwrite"
    )
    parser.add_argument("--since", help="ISO timestamp with explicit UTC offset")
    parser.add_argument("--until", help="ISO timestamp with explicit UTC offset")
    parser.add_argument(
        "--deadline", help="Observation stops no later than this explicit timestamp"
    )
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--interval", type=float, default=10)
    parser.add_argument(
        "--log-file", type=Path, help="Optional explicit diagnostic log; tail is never exported"
    )
    args = parser.parse_args(argv)
    checks = {}
    if args.mode == "faults":
        checks["synthetic_faults"] = faults()
    elif args.mode == "observe":
        times = [parse_time(value) for value in (args.since, args.until, args.deadline)]
        if args.database is None or None in times:
            checks["observation"] = check("failed", "DatabaseTaskWindowDeadlineRequired")
        else:
            checks["observation"] = observe(
                args.database, args.task_id, *times[:2], args.duration, args.interval, times[2]
            )
    else:
        checks["python_sqlite"] = check(
            "passed" if sys.version_info >= (3, 11) else "failed",
            "InterpreterMetadata",
            python=platform.python_version(),
            sqlite=sqlite3.sqlite_version,
        )
        checks["windows_metadata"] = windows_evidence()
        checks["git"] = git_evidence()
        checks["health"] = loopback_health(args.health_url, checks["git"])
        checks["queue"] = (
            database_evidence(args.database, args.task_id)
            if args.database
            else check("skipped", "ExplicitDatabaseRequired")
        )
        if args.receipt_database:
            checks["receipt"] = receipt_evidence(
                args.database, args.receipt_database, args.task_id, args.item_id
            )
        if args.log_file:
            since, until = parse_time(args.since), parse_time(args.until)
            checks["log"] = (
                log_evidence(args.log_file, since, until)
                if since is not None and until is not None and since < until
                else check("failed", "ExplicitLogWindowRequired")
            )
    report = {
        "report_schema": 1,
        "mode": args.mode,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "platform": "Windows" if sys.platform == "win32" else "Other",
        "field_recovery_verified": False,
        "checks": checks,
        "summary": summarize(checks),
    }
    encoded = json.dumps(report, ensure_ascii=True, indent=2)
    if args.output:
        try:
            forbidden = {
                value.resolve()
                for value in (args.database, args.receipt_database, args.log_file)
                if value
            }
            if (
                args.output.suffix.lower() != ".json"
                or args.output.resolve() in forbidden
                or re.search(
                    r"(?:^|[._-])(env|session|credentials?|cookies?|keys?)(?:[._-]|$)",
                    args.output.name,
                    re.I,
                )
            ):
                raise PermissionError("SafeReportJSONPathRequired")
            descriptor = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
                target.write(encoded + "\n")
        except OSError as exc:
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "code": "ReportNotWritten",
                        "error_type": type(exc).__name__,
                    }
                )
            )
            return 1
    else:
        print(encoded)
    return 1 if report["summary"]["failed"] else 2 if report["summary"]["unverified"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
