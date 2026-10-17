"""Watch the existing sender without changing publication permissions or item states.

Run this host from Windows Task Scheduler, rather than a terminal-owned process.
The sender still owns all publication, once-only, quota and schedule checks.
"""

import argparse
import ctypes
import hashlib
import json
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import load_environment
from .store import Store
from .workers import Workers


def read_health(port):
    try:
        # This endpoint contains only service flags, never an operator credential.
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
            return json.load(response)
    except Exception as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ConnectionRefusedError):
            return None
        # A denied local socket is an observation failure, not proof that the
        # service has stopped. Never launch a replacement on that evidence.
        code = "local_access_denied" if isinstance(reason, PermissionError) else "probe_failed"
        return {"status": "unavailable", "probe_error": code}


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


class WindowsProcess:
    def __init__(self, pid):
        from ctypes import wintypes

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.GetExitCodeProcess.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.OpenProcess(0x1000, False, pid)
        self.pid = pid

    def poll(self):
        from ctypes import wintypes

        if not self.handle:
            return -1  # No exit code was observable; do not invent one.
        result = wintypes.DWORD()
        if not self.kernel.GetExitCodeProcess(self.handle, ctypes.byref(result)):
            return -1
        return None if result.value == 259 else result.value

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def imminent_work(store, now):
    return bool(
        store.one(
            "SELECT 1 FROM items i JOIN tasks t ON t.id=i.task_id WHERE t.state='active' "
            "AND (i.status='executing' OR (i.status='not_attempted' AND i.due_at<=? "
            "AND json_extract(t.policy,'$.ends_at') IS NOT NULL "
            "AND julianday(json_extract(t.policy,'$.ends_at'))>julianday(?,'unixepoch'))) LIMIT 1",
            (now + 900, now),
        )
    )


class ServiceHost:
    def __init__(
        self,
        store,
        port,
        log,
        *,
        probe=read_health,
        occupied=port_open,
        process=WindowsProcess,
        launch=None,
        clock=time.time,
        reports_only=False,
    ):
        self.store, self.port, self.log = store, port, log
        self.probe, self.occupied, self.process, self.clock = probe, occupied, process, clock
        self.launch = launch or self._launch
        self.child = None
        self.reports_only = reports_only
        self.sender = None
        self.sender_id = None
        self.state = None
        self.log_handles = []
        self.service_name = "reporting-service" if reports_only else "sender-service"
        self.status_path = Path(store.path).parent / f"{self.service_name}-{port}.json"

    def event(self, kind, **details):
        self.log.info(json.dumps({"at": self.clock(), "event": kind, **details}))
        with self.store.connect(write=True) as db:
            Store.audit(db, "service." + kind, details, now=self.clock())

    def _launch(self):
        folder = Path(self.store.path).parent / f"{self.service_name}-logs"
        folder.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.log_handles = [open(folder / f"{stamp}.{name}.log", "ab") for name in ("out", "err")]
        argv = [
            sys.executable,
            "-m",
            "auto_promotion.cli",
            "--db",
            str(self.store.path),
            "serve",
            "--port",
            str(self.port),
        ]
        env = None
        if self.reports_only:
            argv.append("--reports-only")
            env = {**os.environ, "AUTO_PROMOTION_LIVE_WRITES": "0"}
        return subprocess.Popen(
            argv,
            cwd=Path(__file__).resolve().parents[2],
            stdout=self.log_handles[0],
            stderr=self.log_handles[1],
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def step(self):
        if self.child is not None and self.child.poll() is not None:
            self.event("child_exited", pid=self.child.pid, exit_code=self.child.returncode)
            self.child = None
            for handle in self.log_handles:
                handle.close()
            self.log_handles = []
        if self.sender is not None:
            code = self.sender.poll()
            if code is not None:
                self.event(
                    "sender_exited", pid=self.sender.pid, exit_code=None if code == -1 else code
                )
                Workers(self.store).finish(
                    self.sender_id,
                    "observed_process_exit",
                    code if code != -1 else 1,
                    {"observer": "windows_task_sender_host", "exit_code_observed": code != -1},
                )
                self.sender.close()
                self.sender = self.sender_id = None
        health = self.probe(self.port)
        worker = {} if self.reports_only else Workers(self.store).snapshot()
        if (
            self.reports_only
            and health
            and health.get("worker") is False
            and health.get("report_worker") is True
            and health.get("live_writes") is False
            and health.get("statistics_only") is True
        ):
            state = "running"
        elif health and health.get("probe_error"):
            state = "probe_needs_attention"
        elif (
            not self.reports_only
            and health
            and health.get("worker") is True
            and worker.get("status") == "running"
        ):
            if self.sender is None or self.sender.pid != worker["pid"]:
                if self.sender:
                    self.sender.close()
                self.sender = self.process(worker["pid"])
                self.sender_id = worker["id"]
                self.event("sender_observed", pid=worker["pid"], worker_id=worker["id"])
            state = "running"
        elif self.child is not None:
            # A slow or unhealthy running child is never killed across an uncertain send.
            state = "starting_or_needs_attention"
        elif self.occupied(self.port):
            # Never kill or replace an occupied listener merely because health failed.
            state = "listener_needs_attention"
        elif self.sender is not None and self.sender.poll() is None:
            state = "sender_needs_attention"
        else:
            self.child = self.launch()
            self.event("child_started", pid=self.child.pid)
            state = "starting"
        if state != self.state:
            self.event("state_changed", state=state)
        self.state = state
        status = {
            "updated_at": self.clock(),
            "supervisor_pid": os.getpid(),
            "port": self.port,
            "state": state,
            "sender_pid": self.sender.pid if self.sender else None,
            "launcher_pid": self.child.pid if self.child else None,
            "live_writes": health.get("live_writes") if health else None,
            "reports_only": self.reports_only,
            "probe_error": health.get("probe_error") if health else None,
        }
        temporary = self.status_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(status, indent=2), encoding="utf-8")
        temporary.replace(self.status_path)
        return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--interval", type=float, default=10)
    parser.add_argument("--reports-only", action="store_true")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This service host is installed through Windows Task Scheduler")
    repo = Path(__file__).resolve().parents[2]
    os.chdir(repo)
    load_environment(repo / ".env")
    if args.reports_only:
        os.environ["AUTO_PROMOTION_LIVE_WRITES"] = "0"
    # Do not set AUTO_PROMOTION_LIVE_WRITES or release any stop. The CLI reads its existing config.
    os.environ["AP_DB"] = str(args.db.resolve())
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    key = hashlib.sha256(f"{args.db.resolve()}:{args.port}".casefold().encode()).hexdigest()[:24]
    role = "Reports" if args.reports_only else "Sender"
    mutex = kernel.CreateMutexW(None, True, "Local\\AutoPromotion" + role + "-" + key)
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        return  # Another scheduled or manual host already owns this exact database/port.
    service_name = "reporting-service" if args.reports_only else "sender-service"
    handler = RotatingFileHandler(
        args.db.parent / f"{service_name}.jsonl",
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    log = logging.getLogger(service_name)
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    host = ServiceHost(Store(args.db.resolve()), args.port, log, reports_only=args.reports_only)
    host.event("host_started", pid=os.getpid(), host="windows_task_scheduler")
    kernel.SetThreadExecutionState.argtypes = [wintypes.DWORD]
    kernel.SetThreadExecutionState.restype = wintypes.DWORD
    try:
        while True:
            try:
                host.step()
                # Prevent idle sleep only near pending sends. Lid/forced sleep remains an OS choice.
                if not args.reports_only:
                    awake = imminent_work(host.store, time.time())
                    kernel.SetThreadExecutionState(0x80000001 if awake else 0x80000000)
            except Exception as exc:
                host.event("host_error", error_type=type(exc).__name__)
            time.sleep(max(1, args.interval))
    finally:
        kernel.SetThreadExecutionState(0x80000000)


if __name__ == "__main__":
    main()
