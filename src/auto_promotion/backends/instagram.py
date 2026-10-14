"""Instagram API with Instagram Login; credentials remain in a local private profile."""

from __future__ import annotations

import getpass
import json
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .common import BackendError, observed_timestamp, read_profile, save_profile


class GraphError(BackendError):
    def __init__(self, *, limited=False):
        super().__init__("InstagramRateLimited" if limited else "InstagramRequestRejected")
        self.limited = limited


class Graph:
    def __init__(self, settings):
        self.settings = settings
        if not re.fullmatch(r"v\d+\.\d+", settings.get("api_version", "")):
            raise BackendError("InstagramAPIVersionRequired")

    def request(self, path, *, fields=None, body=None):
        url = "https://graph.instagram.com/" + self.settings["api_version"] + "/" + path
        if fields:
            url += "?" + urlencode(fields)
        request = Request(
            url,
            data=urlencode(body).encode("utf-8") if body is not None else None,
            headers={
                "Authorization": "Bearer " + self.settings["access_token"],
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST" if body is not None else "GET",
        )
        try:
            with urlopen(request, timeout=25) as response:
                return json.loads(response.read())
        except HTTPError as exc:
            # Return only error types, never URLs, access tokens or Graph responses.
            limited = exc.code == 429
            try:
                code = json.loads(exc.read()).get("error", {}).get("code")
                limited |= code in {4, 17, 32, 613}
            except (ValueError, TypeError):
                pass
            raise GraphError(limited=limited) from None


def configure(label):
    settings = {
        "access_token": getpass.getpass("Instagram user access token (hidden): ").strip(),
        "api_version": input(
            "Instagram API version from your Meta app (for example v25.0): "
        ).strip(),
    }
    observed = Graph(settings).request("me", fields={"fields": "user_id,username"})
    user_id = str(observed.get("user_id") or observed.get("id") or "")
    username = str(observed.get("username", ""))
    if not user_id.isdigit() or not username:
        raise BackendError("InstagramIdentityUnavailable")
    settings.update(identity=user_id, username=username)
    save_profile("instagram", label, settings)
    return {
        "platform": "instagram",
        "account": label,
        "authenticated": True,
        "instagram_user_id": user_id,
    }


class InstagramBackend:
    def __init__(self, label, *, graph_factory=Graph):
        self.settings = read_profile("instagram", label)
        self.graph = graph_factory(self.settings)

    def identity(self):
        me = self.graph.request("me", fields={"fields": "user_id,username"})
        matched = (
            str(me.get("user_id") or me.get("id")) == self.settings["identity"]
            and me.get("username") == self.settings["username"]
        )
        if not matched:
            raise BackendError("InstagramIdentityMismatch")

    def run(self, operation, payload, checkpoint, authorize):
        self.identity()
        if operation == "health":
            return {
                "healthy": True,
                "service_ready": True,
                "identity_verified": True,
                "account_identity": self.settings["identity"],
                "permissions": ["publish", "manual_comment"],
                "reason": "account_read_verified",
            }
        if operation == "preflight":
            item = payload["payload"]
            return {"allowed": item["target"] == self.settings["identity"] and item["target_owned"]}
        if operation == "verify":
            return self.verify(payload)
        if operation != "execute" or payload["payload"]["action"] != "publish":
            raise BackendError("UnsupportedInstagramOperation")
        item = payload["payload"]
        if item["target"] != self.settings["identity"] or not item["target_owned"]:
            raise BackendError("InstagramOwnedTargetRequired")
        if not item["media"] or len(item["text"]) > 2200:
            raise BackendError("InstagramMediaOrCaptionInvalid")
        media = item["media"]
        children = []
        container_id = None
        for entry in media:
            if not entry["source_url"].startswith("https://"):
                raise BackendError("InstagramPublicHTTPSMediaRequired")
            body = {}
            kind = entry["media_type"]
            if kind == "IMAGE":
                body["image_url"] = entry["source_url"]
            elif kind in {"VIDEO", "REELS"}:
                body.update(
                    video_url=entry["source_url"], media_type="VIDEO" if len(media) > 1 else "REELS"
                )
            else:
                raise BackendError("InstagramMediaTypeRequired")
            if len(media) > 1:
                body["is_carousel_item"] = "true"
            else:
                body["caption"] = item["text"]
            authorize()
            created = self.graph.request(self.settings["identity"] + "/media", body=body)
            container_id = self.numeric_id(created)
            checkpoint({"status": "unknown", "verified": False, "container_id": container_id})
            self.wait_ready(container_id)
            children.append(container_id)
        if len(children) > 1:
            authorize()
            created = self.graph.request(
                self.settings["identity"] + "/media",
                body={
                    "media_type": "CAROUSEL",
                    "children": ",".join(children),
                    "caption": item["text"],
                },
            )
            container_id = self.numeric_id(created)
            checkpoint({"status": "unknown", "verified": False, "container_id": container_id})
            self.wait_ready(container_id)
        authorize()
        published = self.graph.request(
            self.settings["identity"] + "/media_publish", body={"creation_id": container_id}
        )
        media_id = self.numeric_id(published)
        result = {
            "status": "unknown",
            "verified": False,
            "media_id": media_id,
            "container_id": container_id,
        }
        checkpoint(result)
        return self.verify({**payload, "receipt": result})

    @staticmethod
    def numeric_id(result):
        value = str(result.get("id", ""))
        if not value.isdigit():
            raise BackendError("InstagramReceiptUnavailable")
        return value

    def wait_ready(self, identifier):
        for _ in range(15):
            result = self.graph.request(identifier, fields={"fields": "status_code"})
            if result.get("status_code") == "FINISHED":
                return
            if result.get("status_code") in {"ERROR", "EXPIRED"}:
                raise BackendError("InstagramContainerRejected")
            time.sleep(2)
        raise BackendError("InstagramMediaProcessingNotReady")

    def verify(self, payload):
        result = payload.get("receipt") or payload.get("result", {})
        media_id = str(result.get("media_id", ""))
        if not media_id.isdigit():
            return {
                **result,
                "status": "unknown",
                "verified": False,
                "reason": "no_durable_receipt",
            }
        own_media = self.graph.request(
            self.settings["identity"] + "/media",
            fields={"fields": "id,caption,permalink,timestamp", "limit": 100},
        )
        observed = next(
            (row for row in own_media.get("data", []) if str(row.get("id")) == media_id), None
        )
        url = observed.get("permalink", "") if observed else ""
        verified = bool(
            observed
            and (observed.get("caption") or "") == payload["payload"]["text"]
            and url.startswith("https://www.instagram.com/")
        )
        return {
            **result,
            "status": "confirmed" if verified else "unknown",
            "verified": verified,
            "external_reference": url or result.get("external_reference"),
            "published_at": observed_timestamp(observed.get("timestamp")) if verified else None,
        }


def call(label, operation, payload, checkpoint, authorize):
    try:
        return InstagramBackend(label).run(operation, payload, checkpoint, authorize)
    except GraphError as exc:
        if not exc.limited:
            raise
        return {
            "status": "unknown",
            "verified": False,
            "rate_limited": True,
            "retry_after_seconds": 3600,
            "reason": "InstagramRateLimited",
        }
