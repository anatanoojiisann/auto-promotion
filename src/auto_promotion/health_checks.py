"""Bounded health-display probes; never authorization for a platform write."""

import os
from pathlib import Path

from .backends.common import profile_path
from .models import digest

TRANSIENT = {
    "IncompleteReadError",
    "IncompleteRead",
    "ConnectionResetError",
    "ConnectionError",
    "TimeoutError",
    "TimeoutExpired",
    "Timeout",
    "OSError",
    "CloudTransportUnavailable",
    "TabbitConnectionUnavailable",
    "TabbitIdentityUnavailable",
    "BackendError",
    "connection_unavailable",
    "LocalDatabaseUnavailable",
}
AUTH = {
    "TelegramLoginRequired",
    "TelegramIdentityMismatchOrRestricted",
    "BrowserIdentityMismatch",
    "AccountIdentityChanged",
    "AccountIdentityUnavailable",
    "DuplicatePlatformIdentity",
    "AuthKeyUnregisteredError",
    "SessionRevokedError",
    "SessionExpiredError",
    "UserDeactivatedError",
    "UserDeactivatedBanError",
    "XAPIAuthenticationRejected",
}
NAVIGATION = {
    "BrowserNavigationAccessDenied",
    "BrowserNavigationDownload",
    "BrowserNavigationTimeout",
    "BrowserNavigationUnavailable",
    "BrowserNavigationServerError",
}


def category(reason):
    if reason in AUTH:
        return "authorization_required"
    if reason in NAVIGATION:
        return "navigation_blocked"
    if reason in TRANSIENT:
        return "temporary_unavailable"
    return "unverified"


def probe_revision(account, database):
    # Metadata only: no credential/session content or secret enters this key.
    root = os.getenv("AP_ACCOUNTS_DIR", str(Path(database).parent / "accounts"))
    path = profile_path(account["platform"], account["backend_label"], accounts_dir=root)
    try:
        stat = (path / "credentials.json").stat()
        revision = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino, stat.st_size)
    except OSError:
        revision = None
    return digest(
        [
            str(path.resolve()),
            revision,
            "legacy"
            if os.getenv("AP_" + account["platform"].upper() + "_BACKEND") == "legacy"
            else "builtin",
        ]
    )


def probe_key(account, database, *, revision=None):
    return digest(
        [
            account["platform"],
            account["backend_label"],
            account["mode"],
            account.get("identity_hash"),
            revision or probe_revision(account, database),
        ]
    )


def state(account, now):
    if account["disabled"]:
        return "disabled"
    if account["cooldown_until"] > now:
        return "cooldown"
    if account["status"] == "healthy":
        return "healthy"
    return account["health"].get("probe_state", "unverified")


def cached(account, now, key, *, force=False):
    previous = account["health"]
    return bool(
        account["mode"] == "live"
        and previous.get("probe_key") == key
        and previous.get("next_check_at", 0) > now
        and (not force or previous.get("probe_state") != "healthy")
    )
