"""Official TikTok Accounts API. A submitted item is never submitted a second time."""

from __future__ import annotations

import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..models import TikTokPostOptions, canonical, digest
from .common import BackendError, read_profile, save_profile

BASE = "https://business-api.tiktok.com/open_api/v1.3/"
COMMENT_ACTIONS = {
    "reply",
    "comment",
    "like",
    "unlike",
    "hide_comment",
    "unhide_comment",
    "delete_comment",
}
NO_TEXT_ACTIONS = COMMENT_ACTIONS - {"reply", "comment"}
APP_LABEL = "__tiktok_app__"


def comment_binding(payload):
    """Optional native URL evidence must describe the requested post/comment."""
    payload = dict(payload)
    url = payload.get("comment_url")
    if not url:
        return payload
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"www.tiktok.com", "tiktok.com"}
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
        or parsed.fragment
    ):
        raise BackendError("TikTokCommentURLInvalid")
    match = re.fullmatch(r"/@[A-Za-z0-9_.]+/(?:video|photo)/([1-9][0-9]{0,29})/?", parsed.path)
    query = parse_qs(parsed.query, keep_blank_values=True)
    video = match.group(1) if match else (query.get("video_id") or [None])[0]
    if video != payload.get("video_id") or ("video_id" in query and query["video_id"] != [video]):
        raise BackendError("TikTokCommentVideoMismatch")
    if not match:
        raise BackendError("TikTokCommentURLInvalid")
    identifiers = query.get("comment_id", [])
    if len(identifiers) != 1 or not re.fullmatch(r"[1-9][0-9]{0,29}", identifiers[0]):
        raise BackendError("TikTokParentCommentRequired")
    if payload.get("comment_id") and payload["comment_id"] != identifiers[0]:
        raise BackendError("TikTokParentCommentMismatch")
    payload["comment_id"] = identifiers[0]
    return payload


def receipt_binding(identity, payload):
    return digest(
        [
            identity,
            payload.get("task_id"),
            payload.get("item_id"),
            payload.get("attempt_id"),
            payload["payload"],
        ]
    )


class TikTokError(BackendError):
    def __init__(self, code, *, limited=False, retry_after=60):
        super().__init__(code)
        self.limited, self.retry_after = limited, retry_after


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TikTokHTTP:
    def __init__(self, token="", *, opener=None):
        self.token = token
        self.opener = opener or build_opener(NoRedirects())

    def request(self, path, *, params=None, body=None):
        # Paths are fixed by this module; never send tokens to arbitrary URLs.
        if not re.fullmatch(r"[a-z0-9_/]+/", path):
            raise BackendError("TikTokInvalidEndpoint")
        url = BASE + path
        if params:
            url += "?" + urlencode(
                {k: canonical(v) if isinstance(v, (list, dict)) else v for k, v in params.items()}
            )
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Access-Token"] = self.token
        request = Request(
            url,
            headers=headers,
            data=canonical(body).encode() if body is not None else None,
            method="POST" if body is not None else "GET",
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                value = json.loads(response.read(4 * 1024 * 1024))
        except HTTPError as exc:
            retry = exc.headers.get("Retry-After", "60")
            retry = max(1, min(86400, int(retry))) if retry.isdigit() else 60
            raise TikTokError(
                "TikTokRateLimited"
                if exc.code == 429
                else "TikTokAccessDenied"
                if exc.code == 403
                else "TikTokHTTPRejected",
                limited=exc.code == 429,
                retry_after=retry,
            ) from None
        except (URLError, TimeoutError, OSError):
            raise TikTokError("TikTokTransportUnavailable") from None
        except (ValueError, TypeError):
            raise TikTokError("TikTokInvalidResponse") from None
        code = value.get("code") if isinstance(value, dict) else None
        if code != 0:
            limited = code in {40100, 40101, 40102, 40103, 40104}
            reason = (
                "TikTokRateLimited"
                if limited
                else "TikTokAuthorizationRequired"
                if code == 40105
                else "TikTokPermissionDenied"
                if code == 40001
                else "TikTokRequestRejected"
            )
            raise TikTokError(reason, limited=limited)
        data = value.get("data")
        if not isinstance(data, dict):
            raise TikTokError("TikTokInvalidResponse")
        return data


def scope_set(value):
    if isinstance(value, str):
        return set(filter(None, re.split(r"[,\s]+", value)))
    return (
        set(value) if isinstance(value, list) and all(isinstance(v, str) for v in value) else set()
    )


def token_settings(data, previous=None, *, now=None):
    now = time.time() if now is None else now
    previous = previous or {}
    identity = data.get("open_id") or previous.get("identity")
    if (
        not isinstance(identity, str)
        or not identity
        or (previous.get("identity") and previous["identity"] != identity)
    ):
        raise BackendError("TikTokIdentityMismatch")
    if not isinstance(data.get("access_token"), str) or not data["access_token"]:
        raise BackendError("TikTokAuthorizationRequired")
    expires = data.get("expires_in")
    if not isinstance(expires, (int, float)) or isinstance(expires, bool) or expires <= 0:
        raise BackendError("TikTokTokenExpiryMissing")
    result = {
        **previous,
        "identity": identity,
        "access_token": data["access_token"],
        "expires_at": now + expires,
        "refresh_token": data.get("refresh_token") or previous.get("refresh_token", ""),
        "scopes": sorted(scope_set(data.get("scope", previous.get("scopes", [])))),
    }
    refresh_expiry = data.get("refresh_token_expires_in")
    if isinstance(refresh_expiry, (int, float)) and refresh_expiry > 0:
        result["refresh_expires_at"] = now + refresh_expiry
    return result


def secure_url(value):
    p = urlparse(value)
    decoded = p.path
    for _ in range(3):
        decoded = unquote(decoded)
    if "\\" in decoded or any(part in {".", ".."} for part in decoded.split("/")):
        raise BackendError("TikTokPublicHTTPSRequired")
    if (
        p.scheme != "https"
        or not p.hostname
        or p.username
        or p.password
        or p.fragment
        or p.port not in {None, 443}
    ):
        raise BackendError("TikTokPublicHTTPSRequired")
    if p.hostname in {"localhost", "localhost.localdomain"} or p.hostname.endswith(".local"):
        raise BackendError("TikTokPublicHTTPSRequired")
    import ipaddress

    try:
        address = ipaddress.ip_address(p.hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise BackendError("TikTokPublicHTTPSRequired")
    return p


def media_url(value, prefixes):
    parsed = secure_url(value)
    for prefix in prefixes:
        allowed = secure_url(prefix)
        if parsed.netloc == allowed.netloc and parsed.path.startswith(
            allowed.path.rstrip("/") + "/"
        ):
            return value
    raise BackendError("TikTokVerifiedMediaPrefixRequired")


def validate_item(item):
    action = item["action"]
    if not item.get("target_owned"):
        raise BackendError("TikTokOwnedTargetRequired")
    if item.get("reply_to") or item.get("discord_reply_to") or item.get("recipient_id"):
        raise BackendError("TikTokInvalidReplyFields")
    if action == "publish":
        media = item.get("media", [])
        if not media or not item.get("tiktok_options"):
            raise BackendError("TikTokMediaAndOptionsRequired")
        options = TikTokPostOptions.model_validate(item["tiktok_options"])
        kinds = {m["media_type"] for m in media}
        video = kinds <= {"VIDEO", "REELS"} and len(media) == 1
        if not video and kinds != {"IMAGE"}:
            raise BackendError("TikTokCannotMixMedia")
        if item.get("tiktok_video_id") or item.get("tiktok_comment_id"):
            raise BackendError("TikTokPublishCannotReply")
        limit = 2200 if video else 4000
        if len(item.get("text", "").encode("utf-16-le")) // 2 > limit:
            raise BackendError("TikTokCaptionTooLong")
        if not video and (
            not options.privacy_level
            or options.photo_cover_index >= len(media)
            or len(item.get("title", "").encode("utf-16-le")) // 2 > 90
        ):
            raise BackendError("TikTokPhotoOptionsInvalid")
        if video and options.privacy_level not in {"", "PUBLIC_TO_EVERYONE"}:
            raise BackendError("TikTokVideoPublicOnly")
        if options.is_branded_content and options.privacy_level == "SELF_ONLY":
            raise BackendError("TikTokBrandedContentCannotBePrivate")
        for entry in media:
            secure_url(entry.get("source_url", ""))
            valid = (
                {"video/mp4", "video/webm", "video/quicktime"}
                if video
                else {"image/jpeg", "image/webp"}
            )
            if entry.get("mime_type") not in valid:
                raise BackendError("TikTokUnsupportedMediaType")
        return
    if action not in COMMENT_ACTIONS or not re.fullmatch(
        r"[0-9]{1,30}", item.get("tiktok_video_id", "")
    ):
        raise BackendError("TikTokOwnedVideoRequired")
    if action != "comment" and not re.fullmatch(r"[0-9]{1,30}", item.get("tiktok_comment_id", "")):
        raise BackendError("TikTokCommentRequired")
    if action == "comment" and item.get("tiktok_comment_id"):
        raise BackendError("TikTokTopLevelCommentCannotReply")
    if item.get("media") or item.get("title") or item.get("tiktok_options"):
        raise BackendError("TikTokCommentTextOnly")
    if action in {"reply", "comment"} and (
        not item.get("text", "").strip() or len(item["text"].encode("utf-8")) > 1200
    ):
        raise BackendError("TikTokCommentLengthInvalid")
    if action in NO_TEXT_ACTIONS and item.get("text"):
        raise BackendError("TikTokCommentActionCannotHaveText")


class TikTokBackend:
    def __init__(self, label, *, http_factory=TikTokHTTP, clock=time.time):
        self.label, self.http_factory, self.clock = label, http_factory, clock
        self.settings = read_profile("tiktok", label)
        self.app = read_profile("tiktok", APP_LABEL)
        if self.settings.get("client_id") != self.app["identity"]:
            raise BackendError("TikTokApplicationMismatch")
        self.refresh()
        self.http = http_factory(self.settings["access_token"])
        self.granted = None

    def refresh(self):
        if self.settings.get("expires_at", 0) > self.clock() + 300:
            return
        if (
            not self.settings.get("refresh_token")
            or self.settings.get("refresh_expires_at", float("inf")) <= self.clock()
        ):
            raise BackendError("TikTokAuthorizationRequired")
        data = self.http_factory().request(
            "tt_user/oauth2/refresh_token/",
            body={
                "client_id": self.app["identity"],
                "client_secret": self.app["client_secret"],
                "grant_type": "refresh_token",
                "refresh_token": self.settings["refresh_token"],
            },
        )
        self.settings = token_settings(data, self.settings, now=self.clock())
        save_profile("tiktok", self.label, self.settings)

    def identity(self):
        if not {"user.info.basic", "user.info.username"} <= self.scopes():
            raise BackendError("TikTokPermissionDenied")
        result = self.http.request(
            "business/get/",
            params={
                "business_id": self.settings["identity"],
                "fields": ["username", "display_name"],
            },
        )
        username = result.get("username")
        if not isinstance(username, str) or not username:
            raise BackendError("TikTokIdentityUnavailable")
        if username != self.settings["username"]:
            self.settings["username"] = username
            save_profile("tiktok", self.label, self.settings)
        # Stable identity is the token's open_id; usernames may be renamed.
        return result

    def scopes(self):
        if self.granted is not None:
            return self.granted
        data = self.http_factory().request(
            "tt_user/token_info/get/",
            body={"app_id": self.app["identity"], "access_token": self.settings["access_token"]},
        )
        if (
            data.get("creator_id") != self.settings["identity"]
            or str(data.get("app_id")) != self.app["identity"]
        ):
            raise BackendError("TikTokIdentityMismatch")
        self.granted = scope_set(data.get("scope"))
        return self.granted

    def require(self, action):
        granted = self.scopes()
        required = (
            {"video.publish", "video.list"}
            if action == "publish"
            else {"comment.list.manage", "comment.list", "video.list"}
        )
        if not required <= granted:
            raise BackendError("TikTokPermissionDenied")

    def owned_video(self, video_id):
        result = self.http.request(
            "business/video/list/",
            params={
                "business_id": self.settings["identity"],
                "filters": {"video_ids": [video_id]},
                "fields": ["item_id", "caption", "create_time", "share_url"],
            },
        )
        return next(
            (r for r in result.get("videos", []) if str(r.get("item_id")) == video_id), None
        )

    def comments(self, video_id, *, comment_id="", cursor=0, limit=20, replies=False):
        if not self.owned_video(video_id):
            raise BackendError("TikTokOwnedVideoRequired")
        params = {
            "business_id": self.settings["identity"],
            "video_id": video_id,
            "cursor": cursor,
            "max_count": min(limit, 30),
            "status": "ALL",
        }
        if comment_id:
            params["comment_id" if replies else "comment_ids"] = (
                comment_id if replies else [comment_id]
            )
        return self.http.request(
            "business/comment/reply/list/" if replies else "business/comment/list/", params=params
        )

    def gate(self, item):
        validate_item(item)
        if item["target"] != self.settings["identity"]:
            raise BackendError("TikTokOwnedTargetRequired")
        self.require(item["action"])
        if item["action"] == "publish":
            for entry in item["media"]:
                media_url(entry["source_url"], self.app.get("media_url_prefixes", []))
            settings = self.http.request(
                "business/video/settings/", params={"business_id": self.settings["identity"]}
            )
            options = item["tiktok_options"]
            if options.get("privacy_level") and options["privacy_level"] not in settings.get(
                "privacy_level_options", []
            ):
                raise BackendError("TikTokPrivacyUnavailable")
            for key in ("comment", "duet", "stitch"):
                if settings.get(key + "_disabled") is True and not options.get(
                    "disable_" + key, True
                ):
                    raise BackendError("TikTokInteractionDisabled")
        else:
            if not self.owned_video(item["tiktok_video_id"]):
                raise BackendError("TikTokOwnedVideoRequired")
            if item["action"] != "comment":
                result = self.comments(
                    item["tiktok_video_id"], comment_id=item["tiktok_comment_id"]
                )
                found = next(
                    (
                        c
                        for c in result.get("comments", [])
                        if str(c.get("comment_id")) == item["tiktok_comment_id"]
                    ),
                    None,
                )
                if not found or str(found.get("video_id")) != item["tiktok_video_id"]:
                    raise BackendError("TikTokCommentUnavailable")
                if item["action"] == "delete_comment" and found.get("owner") is not True:
                    raise BackendError("TikTokCanOnlyDeleteOwnComment")

    def run(self, operation, payload, checkpoint, authorize):
        self.identity()
        if operation == "health":
            granted = self.scopes()
            permissions = (["publish"] if {"video.publish", "video.list"} <= granted else []) + (
                sorted(COMMENT_ACTIONS)
                if {"comment.list.manage", "comment.list", "video.list"} <= granted
                else []
            )
            return {
                "healthy": True,
                "service_ready": True,
                "identity_verified": True,
                "account_identity": self.settings["identity"],
                "permissions": permissions,
                "reason": "account_read_verified",
            }
        if operation == "targets":
            params = {
                "business_id": self.settings["identity"],
                "fields": ["item_id", "caption", "create_time", "share_url"],
                "max_count": min(payload.get("limit", 20), 20),
            }
            if payload.get("cursor"):
                params["cursor"] = payload["cursor"]
            data = self.http.request("business/video/list/", params=params)
            return {
                "targets": data.get("videos", []),
                "next_cursor": data.get("cursor", 0),
                "has_more": bool(data.get("has_more")),
            }
        if operation == "content":
            payload = comment_binding(payload)
            data = self.comments(
                payload["video_id"],
                comment_id=payload.get("comment_id", ""),
                cursor=payload.get("cursor", 0),
                limit=payload.get("limit", 20),
            )
            content = []
            for row in data.get("comments", []):
                if str(row.get("video_id")) != payload["video_id"] or (
                    payload.get("comment_id")
                    and str(row.get("comment_id")) != payload["comment_id"]
                ):
                    return {
                        "content_items": [],
                        "healthy": False,
                        "reason": "TikTokCommentVideoMismatch",
                        "read_page_complete": False,
                        "next_cursor": payload.get("cursor", 0),
                        "has_more": True,
                    }
                if row.get("owner") is True:
                    continue
                identifier = str(row.get("comment_id", ""))
                if not re.fullmatch(r"[0-9]{1,30}", identifier):
                    continue
                content.append(
                    {
                        "platform": "tiktok",
                        "external_id": identifier,
                        "target": self.settings["identity"],
                        "action": "reply",
                        "context": row.get("text", ""),
                        "author_id": str(row.get("user_id", "")),
                        "target_owned": True,
                        "tiktok_video_id": payload["video_id"],
                        "tiktok_comment_id": identifier,
                        "permission": "allowed",
                        "permission_evidence": "TikTok Accounts API: comment on token owner's video",
                        "permission_account_ids": [payload["account_id"]],
                        "is_public": row.get("status") == "PUBLIC",
                    }
                )
            return {
                "content_items": content,
                "next_cursor": data.get("cursor", 0),
                "has_more": bool(data.get("has_more")),
            }
        if operation == "insights":
            fields = ["username", "display_name"]
            if "user.info.stats" in self.scopes():
                fields += ["followers_count", "following_count", "total_likes", "videos_count"]
            data = self.http.request(
                "business/get/",
                params={
                    "business_id": self.settings["identity"],
                    "fields": fields,
                },
            )
            return {"insights": {k: data[k] for k in fields if k in data}}
        if operation == "verify":
            return self.verify(payload)
        item = payload["payload"]
        self.gate(item)
        if operation == "preflight":
            return {"allowed": True, "target_verified": True}
        if operation != "execute":
            raise BackendError("UnsupportedTikTokOperation")
        action = item["action"]
        body = {"business_id": self.settings["identity"]}
        if action == "publish":
            options = TikTokPostOptions.model_validate(item["tiktok_options"]).model_dump()
            photo = item["media"][0]["media_type"] == "IMAGE"
            post_info = {
                k: options[k] for k in ("disable_comment", "is_brand_organic", "is_branded_content")
            }
            post_info["caption"] = item["text"]
            if photo:
                post_info.update(
                    privacy_level=options["privacy_level"],
                    title=item.get("title", ""),
                    auto_add_music=options["auto_add_music"],
                )
                body.update(
                    photo_images=[m["source_url"] for m in item["media"]],
                    photo_cover_index=options["photo_cover_index"],
                )
            else:
                post_info.update(
                    {k: options[k] for k in ("disable_duet", "disable_stitch", "thumbnail_offset")}
                )
                post_info["is_ai_generated"] = options["is_ai_generated"] or any(
                    m.get("is_ai_generated") for m in item["media"]
                )
                body["video_url"] = item["media"][0]["source_url"]
            body["post_info"] = post_info
            endpoint = "business/photo/publish/" if photo else "business/video/publish/"
        else:
            if action not in {"like", "unlike", "delete_comment"}:
                body.update(video_id=item["tiktok_video_id"])
            if action != "comment":
                body["comment_id"] = item["tiktok_comment_id"]
            if action in {"reply", "comment"}:
                body["text"] = item["text"]
            if action in {"like", "unlike"}:
                body["action"] = action.upper()
            if action in {"hide_comment", "unhide_comment"}:
                body["action"] = "HIDE" if action == "hide_comment" else "UNHIDE"
            endpoint = {
                "reply": "reply/create",
                "comment": "create",
                "like": "like",
                "unlike": "like",
                "hide_comment": "hide",
                "unhide_comment": "hide",
                "delete_comment": "delete",
            }[action]
            endpoint = "business/comment/" + endpoint + "/"
        authorize()
        if hasattr(checkpoint, "progress"):
            checkpoint.progress("platform_submission", True)
        data = self.http.request(
            endpoint, body=body
        )  # Exactly one write; never retry a timed-out POST.
        receipt = {
            "status": "unknown",
            "verified": False,
            "platform_status": "ACCEPTED",
            "tiktok_receipt_binding": receipt_binding(self.settings["identity"], payload),
        }
        if action == "publish":
            if not isinstance(data.get("share_id"), str) or not data["share_id"]:
                raise BackendError("TikTokReceiptUnavailable")
            receipt["publish_id"] = data["share_id"]
        elif action in {"reply", "comment"}:
            receipt["comment_id"] = str(data.get("comment_id", ""))
        else:
            receipt["comment_id"] = item["tiktok_comment_id"]
            receipt["mutation_accepted"] = True
        checkpoint(receipt)
        return self.verify({**payload, "receipt": receipt})

    def verify(self, payload):
        item = payload["payload"]
        receipt = payload.get("receipt") or payload.get("result", {})
        result = {**receipt, "status": "unknown", "verified": False}
        if item["target"] != self.settings["identity"]:
            raise BackendError("TikTokOwnedTargetRequired")
        binding = receipt.get("tiktok_receipt_binding")
        if (binding and binding != receipt_binding(self.settings["identity"], payload)) or (
            not binding and any(payload.get(k) for k in ("task_id", "item_id", "attempt_id"))
        ):
            return {**result, "reason": "TikTokReceiptBindingMismatch"}
        if item["action"] == "publish":
            if not receipt.get("publish_id"):
                return {**result, "reason": "no_durable_receipt"}
            data = self.http.request(
                "business/publish/status/",
                params={
                    "business_id": self.settings["identity"],
                    "publish_id": receipt["publish_id"],
                },
            )
            result["platform_status"] = data.get("status", "UNKNOWN")
            if data.get("status") == "FAILED":
                # Platform has conclusively rejected it. Keep the receipt and never re-submit.
                return {
                    **result,
                    "status": "failed_known",
                    "verified": True,
                    "reason": "TikTokPublishFailed",
                }
            ids = data.get("post_ids", [])
            if data.get("status") != "PUBLISH_COMPLETE" or not ids:
                return {**result, "reason": "TikTokProcessing"}
            if len(ids) != 1 or not re.fullmatch(r"[0-9]{1,30}", str(ids[0])):
                return {**result, "reason": "TikTokReceiptMismatch"}
            video = self.owned_video(str(ids[0]))
            if not video or video.get("caption", "") != item["text"]:
                return {**result, "reason": "TikTokPostNotYetVisible"}
            result.update(
                post_ids=list(map(str, ids)), published_at=numeric_time(video.get("create_time"))
            )
            reference = video.get("share_url") or "https://www.tiktok.com/@" + self.settings[
                "username"
            ] + "/video/" + str(ids[0])
            parsed_reference = urlparse(reference)
            match = re.fullmatch(
                r"/@[A-Za-z0-9_.]+/(?:video|photo)/([1-9][0-9]{0,29})/?", parsed_reference.path
            )
            if (
                not match
                or match.group(1) != str(ids[0])
                or parse_qs(parsed_reference.query).get("comment_id")
            ):
                return {**result, "reason": "TikTokReceiptMismatch"}
            reference = parsed_reference._replace(query="", fragment="").geturl()
        else:
            identifier = receipt.get("comment_id")
            if not identifier:
                return {**result, "reason": "no_durable_receipt"}
            if not isinstance(identifier, str) or not re.fullmatch(r"[1-9][0-9]{0,29}", identifier):
                return {**result, "reason": "TikTokReceiptMismatch"}
            if (item["action"] in NO_TEXT_ACTIONS and identifier != item["tiktok_comment_id"]) or (
                item["action"] == "reply" and identifier == item["tiktok_comment_id"]
            ):
                return {**result, "reason": "TikTokReceiptMismatch"}
            if not self.owned_video(item["tiktok_video_id"]):
                return {**result, "reason": "TikTokOwnedVideoRequired"}
            data = self.comments(item["tiktok_video_id"], comment_id=identifier)
            found = next(
                (r for r in data.get("comments", []) if str(r.get("comment_id")) == identifier),
                None,
            )
            action = item["action"]
            if found and str(found.get("video_id")) != item["tiktok_video_id"]:
                return {**result, "reason": "TikTokReceiptMismatch"}
            if action in {"reply", "comment"}:
                verified = bool(
                    found
                    and found.get("text") == item["text"]
                    and found.get("owner") is True
                    and (
                        str(found.get("parent_comment_id")) == item["tiktok_comment_id"]
                        if action == "reply"
                        else found.get("parent_comment_id") in (None, "", 0, "0")
                    )
                )
            elif action in {"like", "unlike"}:
                verified = bool(found and found.get("liked") is (action == "like"))
            elif action in {"hide_comment", "unhide_comment"}:
                verified = bool(
                    found
                    and found.get("status") == ("HIDDEN" if action == "hide_comment" else "PUBLIC")
                )
            else:
                # An empty successful lookup including hidden comments confirms removal.
                verified = receipt.get("mutation_accepted") is True and not found
            if not verified:
                return {**result, "reason": "TikTokCommentNotYetVerified"}
            reference = (
                "https://www.tiktok.com/@"
                + self.settings["username"]
                + "/video/"
                + item["tiktok_video_id"]
                + "?comment_id="
                + identifier
            )
            result["published_at"] = (
                numeric_time(found.get("create_time"))
                if found and action in {"reply", "comment"}
                else None
            )
        parsed = urlparse(reference)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"www.tiktok.com", "tiktok.com"}
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
        ):
            return {**result, "reason": "TikTokInvalidReceiptURL"}
        return {**result, "status": "confirmed", "verified": True, "external_reference": reference}


def call(label, operation, payload, checkpoint, authorize):
    try:
        settings = read_profile("tiktok", label)
        from .tiktok_desktop import call as desktop_call
        from .tiktok_desktop import desktop_profile

        if desktop_profile(settings):
            return desktop_call(settings, operation, payload, checkpoint, authorize)
        return TikTokBackend(label).run(operation, payload, checkpoint, authorize)
    except TikTokError as exc:
        if exc.safe_code == "TikTokAccessDenied":
            return {
                "status": "unknown",
                "verified": False,
                "allowed": False,
                "healthy": False,
                "reason": exc.safe_code,
                "failure_class": "access_denied",
                "read_state": "access_denied",
                "rate_limited": False,
                "retry_after_seconds": exc.retry_after,
                "execution_step": "tiktok_api_" + operation,
                "cooldown_source": "tiktok_api_access_denied",
            }
        if not exc.limited:
            if operation == "preflight":
                return {"allowed": False, "reason": exc.safe_code}
            raise
        return {
            "status": "unknown",
            "verified": False,
            "allowed": False,
            "healthy": False,
            "rate_limited": True,
            "retry_after_seconds": exc.retry_after,
            "reason": exc.safe_code,
        }
    except BackendError as exc:
        if operation == "preflight":
            return {"allowed": False, "reason": exc.safe_code}
        raise


def numeric_time(value):
    try:
        return float(value) if value is not None else None
    except (ValueError, TypeError):
        return None
