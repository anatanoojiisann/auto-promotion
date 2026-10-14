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
