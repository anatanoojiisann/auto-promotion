"""Official Discord Bot API; no user tokens, joining, DMs or automatic retries."""

from __future__ import annotations

import getpass
import hashlib
import json
import math
import re
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..models import epoch
from .common import BackendError, read_profile, save_profile

VIEW = 1 << 10
SEND = 1 << 11
ATTACH = 1 << 15
HISTORY = 1 << 16
ADMIN = 1 << 3
ALL = (1 << 64) - 1
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 25 * 1024 * 1024


def snowflake(value):
    value = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,19}", value) or int(value) > ALL:
        raise BackendError("DiscordInvalidID")
    return value


class DiscordError(BackendError):
    def __init__(self, status, retry_after=0):
        code = {
            401: "DiscordInvalidToken",
            403: "DiscordMissingPermissions",
            404: "DiscordTargetUnavailable",
            429: "DiscordRateLimited",
        }.get(status, "DiscordRequestRejected")
        super().__init__(code)
        self.status = status
        self.retry_after = retry_after


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect must never forward a Bot credential to a different destination.
        return None


class DiscordHTTP:
    def __init__(self, settings):
        self.token = settings.get("bot_token", "").strip()
        if not self.token or any(c.isspace() for c in self.token):
            raise BackendError("DiscordInvalidToken")
        self.opener = build_opener(NoRedirect())

    def request(self, path, *, body=None, files=None):
        if not re.fullmatch(r"[A-Za-z0-9/@?=&_-]+", path):
            raise BackendError("DiscordInvalidEndpoint")
        data = None
        content_type = "application/json"
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if files:
            boundary = "ap-" + uuid.uuid4().hex
            chunks = [
                (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="payload_json"\r\n'
                    "Content-Type: application/json\r\n\r\n"
                ).encode()
                + data
                + b"\r\n"
            ]
            for index, (filename, content) in enumerate(files):
                if any(c in filename for c in '\r\n"\\'):
                    raise BackendError("DiscordInvalidFilename")
                chunks.append(
                    (
                        f'--{boundary}\r\nContent-Disposition: form-data; name="files[{index}]"; '
                        f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
                    ).encode("utf-8")
                    + content
                    + b"\r\n"
                )
            chunks.append(f"--{boundary}--\r\n".encode())
            data = b"".join(chunks)
            content_type = "multipart/form-data; boundary=" + boundary
        request = Request(
            "https://discord.com/api/v10/" + path,
            data=data,
            headers={
                "Authorization": "Bot " + self.token,
                "Content-Type": content_type,
                "User-Agent": "DiscordBot (https://github.com/anatanoojiisann/auto-promotion, 0.1)",
            },
            method="POST" if body is not None else "GET",
        )
        try:
            with self.opener.open(request, timeout=25) as response:
                return json.loads(response.read())
        except HTTPError as exc:
            seconds = 60
            if exc.code == 429:
                try:
                    observed = float(
                        json.loads(exc.read()).get(
                            "retry_after", exc.headers.get("Retry-After", 60)
                        )
                    )
                    seconds = max(1, math.ceil(observed)) if math.isfinite(observed) else 60
                except (ValueError, TypeError, AttributeError):
                    try:
                        observed = float(exc.headers.get("Retry-After", 60))
                        seconds = max(1, math.ceil(observed)) if math.isfinite(observed) else 60
                    except (ValueError, TypeError, AttributeError):
                        pass
            raise DiscordError(exc.code, seconds) from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise BackendError("DiscordTransportUnavailable") from None


def bot_identity(http):
    me = http.request("users/@me")
    if me.get("bot") is not True:
        raise BackendError("DiscordBotAccountRequired")
    return {"identity": snowflake(me.get("id", "")), "username": str(me.get("username", "Bot"))}


def configure(label, *, token=None, accounts_dir=None, before_save=None):
    settings = {"bot_token": (token or getpass.getpass("Discord Bot token (hidden): ")).strip()}
    try:
        settings.update(bot_identity(DiscordHTTP(settings)))
        if before_save:
            before_save()
        save_profile("discord", label, settings, accounts_dir=accounts_dir)
        return {
            "platform": "discord",
            "account": label,
            "authenticated": True,
            "discord_user_id": settings["identity"],
            "username": settings["username"],
        }
    finally:
        settings.clear()


def channel_permissions(guild, member, channel):
    """Discord's base-role and channel-overwrite order, using integer bitfields."""
    user_id = str(member.get("user", {}).get("id", ""))
    if user_id == str(guild.get("owner_id")):
        return ALL
    role_ids = {str(guild["id"]), *map(str, member.get("roles", []))}
    permissions = 0
    for role in guild["roles"]:
        if str(role["id"]) in role_ids:
            permissions |= int(role["permissions"])
    if permissions & ADMIN:
        return ALL
    overwrites = channel.get("permission_overwrites", [])
    for overwrite in overwrites:
        if str(overwrite["id"]) == str(guild["id"]) and int(overwrite["type"]) == 0:
            permissions = (permissions & ~int(overwrite["deny"])) | int(overwrite["allow"])
    allow = deny = 0
    for overwrite in overwrites:
        if int(overwrite["type"]) == 0 and str(overwrite["id"]) in role_ids - {str(guild["id"])}:
            deny |= int(overwrite["deny"])
            allow |= int(overwrite["allow"])
    permissions = (permissions & ~deny) | allow
    for overwrite in overwrites:
        if int(overwrite["type"]) == 1 and str(overwrite["id"]) == user_id:
            permissions = (permissions & ~int(overwrite["deny"])) | int(overwrite["allow"])
    return permissions


class DiscordBackend:
    def __init__(self, label, *, http_factory=DiscordHTTP):
        self.settings = read_profile("discord", label)
        self.http = http_factory(self.settings)

    def identity(self):
        if bot_identity(self.http)["identity"] != self.settings["identity"]:
            raise BackendError("DiscordIdentityMismatch")

    def channel(self, identifier):
        channel = self.http.request("channels/" + snowflake(identifier))
        if str(channel.get("id")) != str(identifier) or channel.get("type") not in {0, 5}:
            raise BackendError("DiscordTextChannelRequired")
        guild_id = snowflake(channel.get("guild_id", ""))
        guild = self.http.request("guilds/" + guild_id)
        member = self.http.request(f"guilds/{guild_id}/members/{self.settings['identity']}")
        permissions = self.eligible(guild, member, channel)
        return channel, permissions

    @staticmethod
    def eligible(guild, member, channel):
        # Preserve this workbench's public-only policy: visible to @everyone in the server.
        everyone = {"user": {"id": "0"}, "roles": []}
        if channel.get("nsfw") or not channel_permissions(guild, everyone, channel) & VIEW:
            raise BackendError("DiscordPublicChannelRequired")
        permissions = channel_permissions(guild, member, channel)
        required = VIEW | SEND | HISTORY
        timeout = member.get("communication_disabled_until")
        if permissions & required != required or (timeout and epoch(timeout) > time.time()):
            raise BackendError("DiscordMissingPermissions")
        return permissions

    def targets(self):
        targets = []
        after = "0"
        # Bound large installations without presenting a partial catalog as complete.
        for _ in range(3):
            guilds = self.http.request("users/@me/guilds?limit=100&after=" + after)
            for summary in guilds:
                guild_id = snowflake(summary["id"])
                try:
                    guild = self.http.request("guilds/" + guild_id)
                    member = self.http.request(
                        f"guilds/{guild_id}/members/{self.settings['identity']}"
                    )
                    channels = self.http.request("guilds/" + guild_id + "/channels")
                except DiscordError as exc:
                    if exc.status in {403, 404}:
                        continue
                    raise
                for channel in channels:
                    if channel.get("type") not in {0, 5}:
                        continue
                    try:
                        permissions = self.eligible(guild, member, channel)
                    except BackendError:
                        continue
                    targets.append(
                        {
                            "id": snowflake(channel["id"]),
                            "guild_id": guild_id,
                            "name": str(channel.get("name", "")),
                            "guild_name": str(summary.get("name", "")),
                            "can_attach": bool(permissions & ATTACH),
                            "can_reply": channel["type"] == 0,
                        }
                    )
                    if len(targets) >= 500:
                        return {"targets": targets, "truncated": True}
            if len(guilds) < 100:
                return {"targets": targets, "truncated": False}
            after = snowflake(guilds[-1]["id"])
        return {"targets": targets, "truncated": True}

    def preflight(self, item):
        channel, permissions = self.channel(item["target"])
        action = item["action"]
        if action not in {"send_message", "send_file", "reply"} or len(item["text"]) > 2000:
            raise BackendError("DiscordInvalidMessage")
        media = item.get("media", [])
        if not item["text"].strip() and not media:
            raise BackendError("DiscordInvalidMessage")
        if item.get("title") or item.get("reply_to") or item.get("recipient_id"):
            raise BackendError("DiscordInvalidMessage")
        if (action == "send_file" and not media) or (action == "send_message" and media):
            raise BackendError("DiscordInvalidMessage")
        if media and not permissions & ATTACH:
            raise BackendError("DiscordAttachmentPermissionRequired")
        if len(media) > 10:
            raise BackendError("DiscordMediaTooLarge")
        total = 0
        for entry in media:
            size = Path(entry["path"]).stat().st_size
            total += size
            if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise BackendError("DiscordMediaTooLarge")
        if action == "reply":
            message_id = snowflake(item.get("discord_reply_to", ""))
            if channel["type"] != 0:
                raise BackendError("DiscordReplyTextChannelRequired")
            original = self.http.request(f"channels/{channel['id']}/messages/{message_id}")
            if (
                str(original.get("id")) != message_id
                or str(original.get("channel_id")) != item["target"]
            ):
                raise BackendError("DiscordTargetUnavailable")
        elif item.get("discord_reply_to"):
            raise BackendError("DiscordInvalidMessage")
        return channel

    def run(self, operation, payload, checkpoint, authorize):
        self.identity()
        if operation == "health":
            return {
                "healthy": True,
                "service_ready": True,
                "identity_verified": True,
                "account_identity": self.settings["identity"],
                "permissions": ["send_message", "send_file", "reply"],
                "reason": "bot_identity_verified_channel_permissions_checked_before_send",
            }
        if operation == "targets":
            return self.targets()
        if operation == "verify":
            return self.verify(payload)
        if operation == "preflight":
            try:
                self.preflight(payload["payload"])
                return {"allowed": True, "target_verified": True}
            except DiscordError as exc:
                if exc.status == 429:
                    raise
                return {"allowed": False, "reason": exc.safe_code}
            except BackendError as exc:
                return {"allowed": False, "reason": exc.safe_code}
        if operation != "execute":
            raise BackendError("UnsupportedDiscordOperation")
        item = payload["payload"]
        try:
            channel = self.preflight(item)
        except BackendError as exc:
            if isinstance(exc, DiscordError) and exc.status == 429:
                raise
            return {"status": "failed_known", "verified": False, "reason": exc.safe_code}
        files = []
        for entry in item["media"]:
            content = Path(entry["path"]).read_bytes()
            if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise BackendError("ApprovedMediaChanged")
            files.append((entry["filename"], content))
        body = {
            "content": item["text"],
            "allowed_mentions": {"parse": [], "replied_user": False},
            "nonce": payload["item_id"][:25],
            "enforce_nonce": True,
        }
        if files:
            body["attachments"] = [{"id": i, "filename": f[0]} for i, f in enumerate(files)]
        if item["action"] == "reply":
            body["message_reference"] = {
                "message_id": item["discord_reply_to"],
                "channel_id": item["target"],
                "fail_if_not_exists": True,
            }
        authorize()
        try:
            created = self.http.request(
                f"channels/{channel['id']}/messages", body=body, files=files
            )
        except DiscordError as exc:
            if exc.status not in {400, 401, 403, 404, 413}:
                raise
            return {"status": "failed_known", "verified": False, "reason": exc.safe_code}
        message_id = snowflake(created.get("id", ""))
        result = {
            "status": "unknown",
            "verified": False,
            "message_ids": [message_id],
            "external_reference": f"https://discord.com/channels/{channel['guild_id']}/{channel['id']}/{message_id}",
        }
        checkpoint(result)
        return self.verify({**payload, "receipt": result})

    def verify(self, payload):
        receipt = payload.get("receipt") or payload.get("result", {})
        ids = receipt.get("message_ids", [])
        if len(ids) != 1:
            return {
                **receipt,
                "status": "unknown",
                "verified": False,
                "reason": "no_durable_receipt",
            }
        item = payload["payload"]
        channel_id, message_id = snowflake(item["target"]), snowflake(ids[0])
        observed = self.http.request(f"channels/{channel_id}/messages/{message_id}")
        expected_files = sorted(
            (m["filename"], Path(m["path"]).stat().st_size) for m in item["media"]
        )
        observed_files = sorted(
            (m.get("filename", ""), m.get("size", -1)) for m in observed.get("attachments", [])
        )
        verified = (
            str(observed.get("id")) == message_id
            and str(observed.get("channel_id")) == channel_id
            and str(observed.get("author", {}).get("id")) == self.settings["identity"]
            and observed.get("content", "") == item["text"]
            and expected_files == observed_files
            and str(observed.get("message_reference", {}).get("message_id", ""))
            == item.get("discord_reply_to", "")
        )
        return {**receipt, "status": "confirmed" if verified else "unknown", "verified": verified}


def call(label, operation, payload, checkpoint, authorize):
    try:
        return DiscordBackend(label).run(operation, payload, checkpoint, authorize)
    except DiscordError as exc:
        if exc.status != 429:
            raise
        return {
            "status": "unknown",
            "allowed": False,
            "verified": False,
            "rate_limited": True,
            "retry_after_seconds": exc.retry_after,
            "reason": exc.safe_code,
        }
