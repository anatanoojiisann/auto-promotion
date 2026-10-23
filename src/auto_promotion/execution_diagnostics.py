"""Execution diagnostics without exception messages, source lines, or local variables."""

import re
from pathlib import Path

FIELDS = (
    "execution_step",
    "step_started_at",
    "submission_started",
    "exception_type",
    "diagnostic_traceback",
)


def safe_code(value, default="BackendError"):
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", value)
        else default
    )


def admission_failure_reason(exc):
    """Retain only known guard codes; never retain arbitrary exception text."""
    allowed = {
        "PacingWait",
        "AccountChannelLocalActive",
        "ExecutionStopped",
        "ActionNotEnabled",
        "ExecutionWindowElapsed",
        "AttemptRequired",
        "AttemptAlreadyRecorded",
        "QuotaExceeded",
        "LeaseExpired",
        "LeaseNotFound",
        "CloudExecutionClaimRequired",
        "TaskChannelClaimExpired",
        "CloudControlConfigurationMissing",
        "CloudCredentialNotConfigured",
        "CloudOperatorCredentialUnavailable",
        "CloudTransportUnavailable",
        "CloudControlRejected",
        "CloudWriteQuotaExceeded",
        "CloudAuthorizationUnavailable",
        "InvalidLease",
        "NodeUnauthorized",
        "PoolNotAssigned",
        "TaskChannelMismatch",
        "AccountChannelNotAssigned",
        "AccountChannelGenerationChanged",
        "AccountIdentityChanged",
        "CloudWorkerException",
        "CloudRequestRejected",
    }
    code = getattr(exc, "safe_code", "BackendError")
    detail = exc.args[0] if getattr(exc, "args", ()) else None
    if code == "CloudControlUnavailable" and isinstance(detail, str) and detail in allowed:
        return detail
    return safe_code(code)


def browser_read_failure_reason(exc, fallback):
    """Classify known browser failures without exposing paths, URLs or credentials."""
    message = str(exc).casefold()
    groups = (
        (
            ("processsingleton", "user data directory is already in use", "profile is in use"),
            "BrowserProfileBusy",
        ),
        (
            ("err_proxy_connection_failed", "err_tunnel_connection_failed"),
            "BrowserProxyUnavailable",
        ),
        (
            (
                "err_connection_reset",
                "err_connection_closed",
                "err_network_changed",
                "err_http2_protocol_error",
                "err_name_not_resolved",
            ),
            "BrowserNetworkUnavailable",
        ),
        (("eacces", "eperm", "access is denied", "permission denied"), "BrowserLocalAccessDenied"),
        (("executable doesn't exist",), "BrowserExecutableUnavailable"),
        (
            ("target page, context or browser has been closed", "target closed"),
            "BrowserContextClosed",
        ),
    )
    for markers, reason in groups:
        if any(marker in message for marker in markers):
            return reason
    return fallback


def exception_details(exc):
    frames = []
    trace = exc.__traceback__
    package = Path(__file__).resolve().parent
    while trace:
        code = trace.tb_frame.f_code
        try:
            source = Path(code.co_filename).resolve().relative_to(package).as_posix()
        except ValueError:
            pass
        else:
            frames.append(
                {
                    "file": source,
                    "function": safe_code(code.co_name, "unknown"),
                    "line": trace.tb_lineno,
                }
            )
        trace = trace.tb_next
    result = {
        "exception_type": safe_code(type(exc).__name__),
        "diagnostic_traceback": frames[-12:],
    }
    if getattr(exc, "execution_step", None):
        result["execution_step"] = safe_code(exc.execution_step, "unknown")
    if isinstance(getattr(exc, "submission_started", None), bool):
        result["submission_started"] = exc.submission_started
    return result
