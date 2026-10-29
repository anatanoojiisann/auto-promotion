"""Hala source/text transport. An accepted request is not a delivery receipt."""

import json
import os
import shlex
import socket
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

ENDPOINT = "https://api.hala.finance/alerts"


@dataclass(frozen=True)
class AlertSettings:
    key: str = field(default="", repr=False)
    enabled: bool = False
    source: str = "Auto Promotion"
    timeout: int = 10
    poll_seconds: int = 30
    progress_seconds: int = 1800

    @classmethod
    def environment(cls, env_file=None, *, enable=False):
        key = os.getenv("ALERT_RELAY_KEY", "")
        path = env_file or os.getenv("AP_ALERTS_ENV_FILE")
        if not key and path:
            # Read only the named key. Never evaluate a file or import unrelated settings.
            for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
                name, sep, value = line.strip().removeprefix("export ").partition("=")
                if sep and name.strip() == "ALERT_RELAY_KEY":
                    try:
                        key = " ".join(shlex.split(value, comments=True))
                    except ValueError:
                        raise ValueError("AlertKeyFileInvalid") from None
        if key and (not key.isascii() or any(c.isspace() for c in key)):
            raise ValueError("AlertKeyInvalid")
        if os.getenv("AP_ALERTS_ENDPOINT", ENDPOINT) != ENDPOINT:
            raise ValueError("AlertEndpointNotAllowed")

        def bounded(name, default, low, high):
            try:
                value = int(os.getenv(name, str(default)))
            except ValueError:
                raise ValueError("AlertSettingInvalid") from None
            if not low <= value <= high:
                raise ValueError("AlertSettingOutOfRange")
            return value

        return cls(
            key=key,
            enabled=enable or os.getenv("AP_ALERTS_ENABLED") == "1",
            source=os.getenv("AP_ALERTS_SOURCE", "Auto Promotion"),
            timeout=bounded("AP_ALERTS_TIMEOUT_SECONDS", 10, 1, 30),
            poll_seconds=bounded("AP_ALERTS_POLL_SECONDS", 30, 5, 300),
        )


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AlertRelay:
    def __init__(self, settings, *, opener=None):
        self.settings = settings
        self.opener = opener or build_opener(NoRedirect())

    def send(self, text):
        from .alert_templates import clean

        if not self.settings.enabled or not self.settings.key:
            return {"state": "failed", "reason": "relay_not_configured"}
        body = json.dumps(
            {
                "source": clean(self.settings.source, 80).replace(self.settings.key, "[密钥隐藏]"),
                "text": text.replace(self.settings.key, "[密钥隐藏]"),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            ENDPOINT,
            data=body,
            headers={
                "Authorization": "Bearer " + self.settings.key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.settings.timeout) as response:
                code = response.status
                raw = response.read(65536)
            if not 200 <= code < 300:
                return {"state": "unknown", "reason": "relay_unexpected_status"}
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeError):
                result = None
            if isinstance(result, dict) and (
                result.get("success") is False
                or result.get("ok") is False
                or result.get("status") in {"error", "failed", "rejected"}
            ):
                return {"state": "failed", "reason": "relay_business_rejected", "http": code}
            return {"state": "accepted", "reason": "delivery_unverified", "http": code}
        except HTTPError as exc:
            code = exc.code
            retry = exc.headers.get("Retry-After", "") if exc.headers else ""
            exc.close()
            if code == 429:
                return {
                    "state": "retry",
                    "reason": "relay_rate_limited",
                    "retry_after": min(86400, max(30, int(retry))) if retry.isdigit() else 120,
                    "http": code,
                }
            return {
                "state": "unknown" if code >= 500 or code == 408 else "failed",
                "reason": "relay_authorization_failed"
                if code in {401, 403}
                else "relay_outcome_unknown"
                if code >= 500 or code == 408
                else "relay_request_rejected",
                "http": code,
            }
        except URLError as exc:
            # Only failures known to precede submission are safe to retry.
            if isinstance(exc.reason, (ConnectionRefusedError, socket.gaierror)):
                return {"state": "retry", "reason": "relay_not_connected", "retry_after": 30}
            return {"state": "unknown", "reason": "relay_outcome_unknown"}
        except Exception:
            # No exception message, response body or credential is persisted.
            return {"state": "unknown", "reason": "relay_outcome_unknown"}
