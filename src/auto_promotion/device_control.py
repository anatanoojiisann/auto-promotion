"""Scoped Windows controller. Does not bootstrap or synchronize a database."""

import argparse
import ctypes
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

SAFE_ERRORS = {
    "DeviceRequiresExplicitChannelHandoff",
    "DeviceUnauthorized",
    "DeviceAuthorizationExpired",
    "DeviceAccountStartBusy",
    "DeviceMessageBudgetUsed",
    "DeviceRequestChanged",
    "FrozenDeviceDraftChanged",
    "PrepareRequired",
    "ExecutorLoginNotVerified",
    "ExecutorAccountHasOtherPendingTasks",
    "ExecutorSetupIncomplete",
    "ExecutionStopped",
    "DeviceTaskNotOwned",
    "DeviceRegistrationConflict",
    "UnknownDeviceAccount",
    "ControlOutcomeUncertainCheckStatusDoNotRecreate",
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def protect(data, decrypt=False):
    if os.name != "nt":
        raise RuntimeError("WindowsCredentialProtectionRequired")

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buf = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    dest = Blob()
    api = (
        ctypes.windll.crypt32.CryptUnprotectData
        if decrypt
        else ctypes.windll.crypt32.CryptProtectData
    )
    if not api(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(dest)):
        raise RuntimeError("WindowsCredentialProtectionFailed")
    try:
        return ctypes.string_at(dest.data, dest.size)
    finally:
        ctypes.windll.kernel32.LocalFree(dest.data)


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".pending")
    with temporary.open("wb") as f:
        f.write(protect(canonical(data).encode()))
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)


def load(path):
    return json.loads(protect(Path(path).read_bytes(), decrypt=True))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request(config, operation, body, *, operator=None):
    origin = config["url"]
    if origin != "https://auto-promotion-control.auto-promotion-control.workers.dev":
        raise ValueError("UnexpectedControlOrigin")
    path = "/v2/device/" + operation
    token = operator or config["token"]
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    if operator is None:
        body = {**body, "store_id": config["store_id"]}
        headers["X-Node-ID"] = config["node_id"]
        signed = canonical(
            {
                "protocol": 2,
                "method": "POST",
                "path": path,
                "node_id": config["node_id"],
                "body": body,
            }
        )
        headers["X-Local-Control-Proof"] = hmac.new(
            hashlib.sha256(token.encode()).hexdigest().encode(), signed.encode(), hashlib.sha256
        ).hexdigest()
    req = urllib.request.Request(
        origin + path, data=canonical(body).encode(), headers=headers, method="POST"
    )
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=30) as response:
            return json.loads(response.read(65536))
    except urllib.error.HTTPError as exc:
        # Only fixed codes can leave the response; never echo bodies or headers.
        try:
            code = json.loads(exc.read(4096)).get("error")
        except (ValueError, AttributeError):
            code = None
        if isinstance(code, str) and code in SAFE_ERRORS:
            raise RuntimeError(code) from None
        raise RuntimeError("ControlRequestRejectedHTTP" + str(exc.code)) from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError("ControlOutcomeUncertainCheckStatusDoNotRecreate") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["init", "enroll", "prepare", "start", "status", "cancel", "revoke"]
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--account")
    parser.add_argument("--target")
    parser.add_argument("--text")
    parser.add_argument("--operator-token-file")
    args = parser.parse_args()
    print(json.dumps(execute(args), ensure_ascii=False))


def execute(args):
    path = Path(args.config)
    if args.command == "init":
        if path.exists():
            raise RuntimeError("DeviceConfigAlreadyExists")
        if not args.account or not args.target:
            raise ValueError("init requires --account and --target")
        config = {
            "url": "https://auto-promotion-control.auto-promotion-control.workers.dev",
            "node_id": "device_" + uuid.uuid4().hex,
            "store_id": uuid.uuid4().hex,
            "token": secrets.token_urlsafe(48),
            "expires_at": int(time.time()) + 86400,
            "scope": {
                "account_ids": [args.account],
                "targets": [args.target],
                "action": "send_text",
                "max_messages": 1,
            },
        }
        save(path, config)
        return {"created": True, "storage": "Windows current-user DPAPI", "online_requests": 0}
    config = load(path)
    if args.command == "enroll":
        if not args.operator_token_file:
            raise ValueError(
                "enroll requires --operator-token-file (existing operator; never rotates it)"
            )
        body = {k: config[k] for k in ["node_id", "store_id", "expires_at", "scope"]}
        body["token_hash"] = hashlib.sha256(config["token"].encode()).hexdigest()
        result = request(
            config, "register", body, operator=Path(args.operator_token_file).read_text().strip()
        )
    elif args.command == "revoke":
        if not args.operator_token_file:
            raise ValueError("revoke requires --operator-token-file")
        result = request(
            config,
            "revoke",
            {"node_id": config["node_id"]},
            operator=Path(args.operator_token_file).read_text().strip(),
        )
    elif args.command == "prepare":
        if not args.text:
            raise ValueError("prepare requires --text")
        old = config.get("draft")
        if old and old["text"] != args.text:
            raise RuntimeError("FrozenDeviceDraftChanged")
        if not old:
            config["draft"] = {
                "request_id": uuid.uuid4().hex,
                "account_id": config["scope"]["account_ids"][0],
                "target": config["scope"]["targets"][0],
                "text": args.text,
                "expires_at": min(config["expires_at"], int(time.time()) + 1800),
            }
            save(path, config)  # Persist idempotency key before any request.
        result = request(config, "prepare", config["draft"])
        config["plan_id"] = result["plan_id"]
        save(path, config)
    else:
        if not config.get("plan_id"):
            raise RuntimeError("PrepareRequired")
        result = request(config, args.command, {"plan_id": config["plan_id"]})
    allowed = {
        "revoked",
        "registered",
        "existing",
        "created",
        "plan_id",
        "task_id",
        "job_id",
        "state",
        "attempted",
        "verified",
        "submitted",
        "message_ids",
    }
    return {k: v for k, v in result.items() if k in allowed}


if __name__ == "__main__":
    main()
