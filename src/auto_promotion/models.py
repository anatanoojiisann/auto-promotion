from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PLATFORMS = ("telegram", "reddit", "instagram", "x")
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
    platform: Literal["telegram", "reddit", "instagram", "x"]
    label: str = Field(min_length=1, max_length=80)
    backend_label: str = Field(min_length=1, max_length=100)
    mode: Literal["live", "simulation"] = "live"
    daily_limit: int = Field(default=5, ge=1, le=100)
    hourly_limit: int = Field(default=3, ge=1, le=20)
    min_interval: int = Field(default=120, ge=0, le=86400)

    @field_validator("label", "backend_label")
    @classmethod
    def safe_label(cls, value):
        if not value.strip() or any(c in value for c in "\r\n\x00"):
            raise ValueError("账号标签不能为空或包含控制字符")
        return value.strip()


class Policy(Model):
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
    timezone: str = "Asia/Shanghai"
    starts_at: str
    ends_at: str
    min_interval: int = Field(default=120, ge=0, le=86400)
    daily_limit: int = Field(default=5, ge=1, le=100)
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
    account_ids: list[str] = Field(min_length=1, max_length=12)
    policy: Policy


class Media(Model):
    filename: str = Field(min_length=1, max_length=160)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    path: str = ""
    source_url: str = ""
    mime_type: str = "application/octet-stream"
    media_type: Literal["IMAGE", "VIDEO", "REELS", ""] = ""
    is_ai_generated: bool = False


class ItemInput(Model):
    account_id: str
    action: str
    target: str = Field(min_length=1, max_length=500)
    text: str = Field(default="", max_length=40000)
    title: str = Field(default="", max_length=300)
    media: list[Media] = Field(default_factory=list, max_length=10)
    scheduled_at: str
    reply_to: int | None = Field(default=None, gt=0)
    recipient_id: str = Field(default="", max_length=160)
    target_owned: bool = False

    @field_validator("scheduled_at")
    @classmethod
    def valid_time(cls, value):
        epoch(value)
        return value


class ApprovalInput(Model):
    phrase: str


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
