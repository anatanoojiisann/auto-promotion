"""Protected, database-bound handoff for independent phone pool workers."""

import json
import os
import stat
import tempfile
from pathlib import Path

from .core import Conflict


def private_directory_is_safe(path):
    """Only the current user and the operating system may access this directory."""
    try:
        info = path.lstat()
        if (
            not path.is_dir()
            or path.is_symlink()
            or (
                getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            )
        ):
            return False
        if os.name != "nt":
            return info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) & 0o077 == 0
        import win32api
        import win32con
        import win32security

        descriptor = win32security.GetFileSecurity(
            str(path), win32security.DACL_SECURITY_INFORMATION
        )
        if not descriptor.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED:
            return False
        acl = descriptor.GetSecurityDescriptorDacl()
        if acl is None:
            return False
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        current = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        system = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
        allowed = {win32security.ConvertSidToStringSid(x) for x in (current, system)}
        for i in range(acl.GetAceCount()):
            ace = acl.GetAce(i)
            if ace[0][0] == win32security.ACCESS_DENIED_ACE_TYPE:
                continue
            if ace[0][0] != win32security.ACCESS_ALLOWED_ACE_TYPE:
                return False
            if win32security.ConvertSidToStringSid(ace[2]) not in allowed:
                return False
        return True
    except Exception:
        return False


def write_worker_config(database, config, kind):
    if kind not in {"social", "reddit"}:
        raise ValueError("Unsupported phone worker")
    configured = os.environ.get("AP_PHONE_PRIVATE_DIR", "")
    root = Path(configured)
    if not configured or not root.is_absolute() or not private_directory_is_safe(root):
        raise Conflict("PhonePrivateWorkerDirectoryRequired")
    path = root / f"phone-{kind}-worker.private.json"
    value = config.model_dump(mode="json")
    for name in (
        "service_token",
        "runtime_token",
        "oauth_client_secret",
        "reddit_read_access_token",
    ):
        secret = getattr(config, name, None)
        if secret is not None:
            value[name] = secret.get_secret_value()
    fd, temporary = tempfile.mkstemp(prefix=f"phone-{kind}-worker-", suffix=".tmp", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump({"database": str(Path(database).resolve()), "config": value}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return str(path)
