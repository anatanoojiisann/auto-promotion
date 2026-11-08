"""Run a headed, read-only browser bridge on a private Windows desktop."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid

from .backends.common import BackendError, read_profile

_CHILD_ENV = "AP_ENGAGEMENT_BACKGROUND_DESKTOP_CHILD"
_TIMEOUT_MS = 160_000
_MAX_RESULT_BYTES = 1_000_000


class BackgroundDesktopUnavailable(Exception):
    """The isolated desktop could not be prepared before a child was started."""


def needs_background_desktop(request):
    if (
        sys.platform != "win32"
        or os.environ.get(_CHILD_ENV) == "1"
        or request.get("platform") != "x"
        or request.get("operation") != "engagement"
        or request.get("payload", {}).get("read_only") is not True
    ):
        return False
    settings = read_profile("x", request["account"])
    return (
        settings.get("connection_type") != "x_api"
        and settings.get("browser_channel", "chromium") in {"chromium", "msedge", "chrome"}
        and settings.get("browser_headless", True) is False
    )


def dispatch_on_background_desktop(request):
    """Return the bridge result without showing a window on the active desktop.

    The child executes the normal bridge and account lock. Its job is terminated
    if this process exits, so an interrupted read cannot leave a browser holding
    the account profile open.
    """
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        import pywintypes
        import win32api
        import win32con
        import win32event
        import win32file
        import win32job
        import win32pipe
        import win32process
    except ImportError as exc:
        raise BackgroundDesktopUnavailable from exc

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.CreateDesktopW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
    ]
    user32.CreateDesktopW.restype = wintypes.HANDLE
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    desktop_name = "AP_Metrics_" + uuid.uuid4().hex
    desktop = user32.CreateDesktopW(desktop_name, None, None, 0, 0x01FF, None)
    if not desktop:
        raise BackgroundDesktopUnavailable

    handles = []
    process = None
    resumed = False
    reader = None
    output = []
    errors = []
    try:
        attributes = pywintypes.SECURITY_ATTRIBUTES()
        attributes.bInheritHandle = True
        child_input, parent_input = win32pipe.CreatePipe(attributes, 0)
        parent_output, child_output = win32pipe.CreatePipe(attributes, 0)
        handles.extend((child_input, parent_input, parent_output, child_output))
        for handle in (parent_input, parent_output):
            win32api.SetHandleInformation(handle, win32con.HANDLE_FLAG_INHERIT, 0)
        null = win32file.CreateFile(
            "NUL",
            win32con.GENERIC_WRITE,
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
            attributes,
            win32con.OPEN_EXISTING,
            0,
            None,
        )
        handles.append(null)
        job = win32job.CreateJobObject(None, "")
        handles.append(job)
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)

        startup = win32process.STARTUPINFO()
        startup.lpDesktop = "WinSta0\\" + desktop_name
        startup.dwFlags |= win32con.STARTF_USESTDHANDLES
        startup.hStdInput, startup.hStdOutput, startup.hStdError = child_input, child_output, null
        environment = {**os.environ, _CHILD_ENV: "1"}
        command = subprocess.list2cmdline([sys.executable, "-m", "auto_promotion.native_bridge"])
        process, thread, _, _ = win32process.CreateProcess(
            sys.executable,
            command,
            None,
            None,
            True,
            win32con.CREATE_SUSPENDED | win32process.CREATE_NO_WINDOW,
            environment,
            None,
            startup,
        )
        handles.extend((process, thread))
        win32job.AssignProcessToJobObject(job, process)

        def read_result():
            try:
                fd = msvcrt.open_osfhandle(parent_output.Detach(), os.O_RDONLY)
                result = bytearray()
                with os.fdopen(fd, "rb", buffering=0) as stream:
                    while chunk := stream.read(65536):
                        if len(result) <= _MAX_RESULT_BYTES:
                            result.extend(chunk[: _MAX_RESULT_BYTES + 1 - len(result)])
                output.append(result)
            except OSError as exc:
                errors.append(exc)

        reader = threading.Thread(target=read_result, daemon=True, name="metrics-desktop-result")
        reader.start()
        win32process.ResumeThread(thread)
        resumed = True
        child_input.Close()
        child_output.Close()
        input_fd = msvcrt.open_osfhandle(parent_input.Detach(), os.O_WRONLY)
        with os.fdopen(input_fd, "wb") as stream:
            stream.write(json.dumps(request, ensure_ascii=True).encode("utf-8"))

        if win32event.WaitForSingleObject(process, _TIMEOUT_MS) != win32event.WAIT_OBJECT_0:
            win32job.TerminateJobObject(job, 1)
            raise BackendError("BackgroundDesktopTimeout")
        reader.join(timeout=5)
        if reader.is_alive() or errors or not output or len(output[0]) > _MAX_RESULT_BYTES:
            raise BackendError("BackgroundDesktopResultUnavailable")
        if win32process.GetExitCodeProcess(process) != 0:
            raise BackendError("BackgroundDesktopProcessFailed")
        try:
            result = json.loads(output[0])
        except (UnicodeError, ValueError):
            raise BackendError("BackgroundDesktopResultUnavailable") from None
        if not isinstance(result, dict):
            raise BackendError("BackgroundDesktopResultUnavailable")
        return result
    except BackendError:
        raise
    except Exception as exc:
        if not resumed:
            raise BackgroundDesktopUnavailable from exc
        raise BackendError("BackgroundDesktopProcessFailed") from None
    finally:
        if process is not None and not resumed:
            try:
                win32process.TerminateProcess(process, 1)
            except Exception:
                pass
        for handle in reversed(handles):
            try:
                handle.Close()
            except Exception:
                pass
        if reader is not None and reader.is_alive():
            reader.join(timeout=2)
        user32.CloseDesktop(desktop)
