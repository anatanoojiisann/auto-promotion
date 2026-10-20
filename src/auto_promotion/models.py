from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PLATFORMS = ("telegram", "reddit", "instagram", "x", "discord", "tiktok")
CAPABILITIES = {
    "telegram": {
        "discover",
        "send_message",
        "reply",
        "send_file",
        "schedule_message",
        "schedule_file",
        "join",
    },
    "reddit": {"discover", "reply", "post"},
    "instagram": {"publish", "manual_comment"},
    "x": {"discover", "like", "reply", "repost", "quote"},
    "discord": {"send_message", "reply", "send_file"},
    "tiktok": {
        "publish",
        "reply",
        "comment",
        "like",
        "unlike",
        "hide_comment",
        "unhide_comment",
        "delete_comment",
    },
}


def canonical(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def epoch(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("时间必须包含明确的 UTC 偏移量")
    return parsed.timestamp()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountInput(Model):
    platform: Literal["telegram", "reddit", "instagram", "x", "discord", "tiktok"]
    label: str = Field(min_length=1, max_length=80)
    backend_label: str = Field(min_length=1, max_length=100)
    mode: Literal["live", "simulation"] = "live"
    daily_limit: int = Field(default=5, ge=1, le=300)
    hourly_limit: int = Field(default=3, ge=1, le=20)
    min_interval: int = Field(default=120, ge=0, le=86400)

    @field_validator("label", "backend_label")
    @classmethod
    def safe_label(cls, value):
        if not value.strip() or any(c in value for c in "\r\n\x00"):
            raise ValueError("账号标签不能为空或包含控制字符")
        return value.strip()


class AccountUpdate(Model):
    label: str = Field(min_length=1, max_length=80)
    daily_limit: int = Field(ge=1, le=300)
    hourly_limit: int = Field(ge=1, le=20)
    min_interval: int = Field(ge=0, le=86400)

    @field_validator("label")
    @classmethod
    def safe_label(cls, value):
        return AccountInput.safe_label(value)


class Policy(Model):
    execution_channel: Literal["inherit", "cloud", "local"] = "inherit"
    queries: list[str] = Field(default_factory=list, max_length=20)
    required_keywords: list[str] = Field(default_factory=list)
    blocked_keywords: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=lambda: ["en", "zh"])
    public_only: Literal[True] = True
    min_members: int = Field(default=0, ge=0)
    min_activity: int = Field(default=0, ge=0)
    min_confidence: float = Field(default=0.8, ge=0.5, le=1)
    max_candidates: int = Field(default=20, ge=1, le=200)
    max_joins: int = Field(default=0, ge=0, le=20)
    outbound_after_membership_seconds: int | None = Field(default=None, ge=3600, le=604800)
    outbound_after_service_recovery_seconds: int | None = Field(default=None, ge=30, le=86400)
    timezone: str = "Asia/Shanghai"
    starts_at: str
    ends_at: str
    min_interval: int = Field(default=120, ge=0, le=86400)
    ordering_mode: Literal["strict", "independent_accounts"] = "strict"
    pacing_scope: Literal["task", "account"] = "task"
    quota_snapshot: dict = Field(default_factory=dict)
    daily_limit: int = Field(default=5, ge=1, le=300)
    target_daily_limit: int = Field(default=3, ge=1, le=3)
    user_daily_limit: int = Field(default=2, ge=1, le=2)
    target_allowlist: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def window(self):
        ZoneInfo(self.timezone)
        if epoch(self.ends_at) <= epoch(self.starts_at):
            raise ValueError("结束时间必须晚于开始时间")
        if not self.languages:
            raise ValueError("必须指定允许语言")
        return self


class TaskInput(Model):
    name: str = Field(min_length=1, max_length=160)
    account_ids: list[str] = Field(min_length=1, max_length=200)
    policy: Policy


class Media(Model):
    filename: str = Field(min_length=1, max_length=160)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    path: str = ""
    source_url: str = ""
    mime_type: str = "application/octet-stream"
    media_type: Literal["IMAGE", "VIDEO", "REELS", ""] = ""
    is_ai_generated: bool = False


class TikTokPostOptions(Model):
    privacy_level: Literal[
        "", "PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY"
    ] = ""
    disable_comment: bool = True
    disable_duet: bool = True
    disable_stitch: bool = True
    is_brand_organic: bool = False
    is_branded_content: bool = False
    is_ai_generated: bool = False
    thumbnail_offset: int = Field(default=0, ge=0, le=600000)
    photo_cover_index: int = Field(default=0, ge=0, le=9)
    auto_add_music: bool = False


class ItemInput(Model):
    account_id: str
    action: str
    target: str = Field(min_length=1, max_length=500)
    text: str = Field(default="", max_length=40000)
    title: str = Field(default="", max_length=300)
    media: list[Media] = Field(default_factory=list, max_length=10)
    scheduled_at: str
    reply_to: int | None = Field(default=None, gt=0)
    discord_reply_to: str = Field(default="", pattern=r"^(?:|[1-9][0-9]{0,19})$")
    recipient_id: str = Field(default="", max_length=160)
    target_owned: bool = False
    execution_mode: Literal["automatic", "manual"] = "automatic"
    opportunity_id: str = ""
    content_key: str = ""
    profile_version: int = Field(default=0, ge=0)
    tiktok_video_id: str = Field(default="", pattern=r"^(?:|[0-9]{1,30})$")
    tiktok_comment_id: str = Field(default="", pattern=r"^(?:|[0-9]{1,30})$")
    tiktok_options: TikTokPostOptions | None = None

    @field_validator("scheduled_at")
    @classmethod
    def valid_time(cls, value):
        epoch(value)
        return value


class TaskStateInput(Model):
    state: Literal["paused", "active", "cancelled"]


class CandidateInput(Model):
    account_id: str
    target: str
    title: str = ""
    text: str = ""
    language: str = "unknown"
    members: int = Field(default=0, ge=0)
    activity: int = Field(default=0, ge=0)
    confidence: float = Field(default=0, ge=0, le=1)
    is_public: bool = True


class ManualOutcome(Model):
    observed_text: str
    external_reference: str
    verified: bool


class QuotaLimit(Model):
    daily: int = Field(default=100, ge=1, le=100000)
    hourly: int = Field(default=20, ge=1, le=10000)


class ExecutionSettings(Model):
    global_limit: QuotaLimit = Field(default_factory=QuotaLimit)
    platform_limits: dict[str, QuotaLimit] = Field(default_factory=dict)
    max_workers: int = Field(default=6, ge=1, le=50)
    platform_workers: dict[str, int] = Field(default_factory=lambda: {"x": 2, "reddit": 2})
    pool_workers: dict[str, int] = Field(default_factory=dict)
    quota_timezone: Literal["UTC"] = "UTC"

    @model_validator(mode="after")
    def platforms(self):
        from .routing import POOLS

        if any(p not in PLATFORMS for p in {*self.platform_limits, *self.platform_workers}):
            raise ValueError("未知平台")
        if any(not 1 <= n <= 50 for n in self.platform_workers.values()):
            raise ValueError("平台并发数必须为 1–50")
        if any(k not in POOLS or not 1 <= v <= 50 for k, v in self.pool_workers.items()):
            raise ValueError("工作池名称或并发数无效")
        return self


class AccountProfile(Model):
    brand: str = Field(default="", max_length=160)
    role: str = Field(default="", max_length=160)
    languages: list[str] = Field(default_factory=lambda: ["en", "zh"], max_length=30)
    topics: list[str] = Field(default_factory=list, max_length=50)
    audience: list[str] = Field(default_factory=list, max_length=50)
    tone: str = Field(default="", max_length=1000)
    facts: str = Field(default="", max_length=5000)
    blocked_keywords: list[str] = Field(default_factory=list, max_length=100)
    allowed_targets: list[str] = Field(default_factory=list, max_length=1000)
    reply_template: str = Field(default="", max_length=40000)
    work_start_hour: int = Field(default=0, ge=0, le=23)
    work_end_hour: int = Field(default=24, ge=1, le=24)
    timezone: str = "Asia/Shanghai"

    @model_validator(mode="after")
    def window(self):
        ZoneInfo(self.timezone)
        if not self.languages or self.work_end_hour <= self.work_start_hour:
            raise ValueError("请指定语言和有效工作时段")
        return self


class PoolInput(Model):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)
    account_ids: list[str] = Field(default_factory=list, max_length=1000)
    limit: QuotaLimit = Field(default_factory=lambda: QuotaLimit(daily=1000, hourly=100))


class OpportunityInput(Model):
    platform: Literal["telegram", "reddit", "instagram", "x", "discord", "tiktok"]
    external_id: str = Field(min_length=1, max_length=160)
    native_target_id: str = Field(default="", pattern=r"^(?:|-?[1-9][0-9]{0,20})$")
    target: str = Field(min_length=1, max_length=500)
    action: str = "reply"
    author_id: str = Field(default="", max_length=160)
    title: str = Field(default="", max_length=300)
    context: str = Field(default="", max_length=40000)
    language: str = Field(default="unknown", max_length=30)
    topics: list[str] = Field(default_factory=list, max_length=50)
    audience: list[str] = Field(default_factory=list, max_length=50)
    confidence: float = Field(default=0.8, ge=0, le=1)
    value: float = Field(default=0.5, ge=0, le=1)
    members: int = Field(default=0, ge=0)
    activity: int = Field(default=0, ge=0)
    is_public: bool = True
    permission: Literal["unknown", "allowed", "denied"] = "unknown"
    permission_evidence: str = Field(default="", max_length=2000)
    permission_account_ids: list[str] = Field(default_factory=list, max_length=1000)
    published_at: str | None = None
    expires_at: str | None = None
    draft_text: str = Field(default="", max_length=40000)
    draft_title: str = Field(default="", max_length=300)
    reply_to: int | None = Field(default=None, gt=0)
    discord_reply_to: str = Field(default="", pattern=r"^(?:|[1-9][0-9]{0,19})$")
    target_owned: bool = False
    media: list[Media] = Field(default_factory=list, max_length=10)
    tiktok_video_id: str = Field(default="", pattern=r"^(?:|[0-9]{1,30})$")
    tiktok_comment_id: str = Field(default="", pattern=r"^(?:|[0-9]{1,30})$")
    tiktok_options: TikTokPostOptions | None = None

    @model_validator(mode="after")
    def valid(self):
        if self.action not in CAPABILITIES[self.platform] - {"discover", "join"}:
            raise ValueError("平台不支持该互动动作")
        for value in (self.published_at, self.expires_at):
            if value:
                epoch(value)
        if self.platform == "telegram" and self.action == "reply" and not self.reply_to:
            raise ValueError("Telegram 回复需要原消息 ID")
        if self.platform == "discord" and self.action == "reply" and not self.discord_reply_to:
            raise ValueError("Discord 回复需要原消息 ID")
        if self.platform == "tiktok" and self.action != "publish" and not self.tiktok_video_id:
            raise ValueError("TikTok 评论互动需要自有视频 ID")
        return self


class OpportunityImport(Model):
    items: list[OpportunityInput] = Field(min_length=1, max_length=5000)


class CampaignInput(Model):
    name: str = Field(min_length=1, max_length=160)
    pool_id: str
    opportunity_ids: list[str] = Field(min_length=1, max_length=10000)
    policy: Policy
    limit: QuotaLimit = Field(default_factory=QuotaLimit)
    batch_size: int = Field(default=50, ge=1, le=100)


class PlanInput(Model):
    expected_revision: int | None = Field(default=None, ge=1)


class AssignmentUpdate(Model):
    text: str = Field(max_length=40000)
    title: str = Field(default="", max_length=300)


class GenerateDraftsInput(Model):
    assignment_ids: list[str] = Field(min_length=1, max_length=100)


class ContentCollectInput(Model):
    target: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=200, ge=1, le=1000)
