"""Versioned high-frequency policy. Durations are seconds; stored timestamps are UTC."""

from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from .models import Model


class ScanSettings(Model):
    interval_seconds: int = Field(default=600, ge=30, le=604800)
    initial_lookback_seconds: int = Field(default=1800, ge=60, le=604800)
    overlap_seconds: int = Field(default=1200, ge=0, le=604800)
    request_timeout_seconds: int = Field(default=30, ge=1, le=120)
    round_deadline_seconds: int = Field(default=300, ge=10, le=86400)
    lease_seconds: int = Field(default=120, ge=10, le=3600)
    heartbeat_seconds: int = Field(default=30, ge=1, le=600)
    long_gap_reset_seconds: int = Field(default=1200, ge=60, le=1209600)
    max_pages_per_query: int = Field(default=20, ge=1, le=1000)
    max_reads_per_day: int = Field(default=1000, ge=1, le=100000)

    @model_validator(mode="after")
    def timing(self):
        if self.heartbeat_seconds * 2 >= self.lease_seconds:
            raise ValueError("心跳间隔必须小于租约的一半")
        if self.long_gap_reset_seconds < self.interval_seconds * 2:
            raise ValueError("长期中断阈值至少为扫描间隔的两倍")
        return self


class ReviewSettings(Model):
    batch_wait_seconds: int = Field(default=18000, ge=0, le=604800)
    expiry_alert_enabled: bool = True
    expiry_alert_lead_seconds: int = Field(default=1800, ge=0, le=86400)
    notification_channel: Literal["inherit", "workbench", "webhook"] = "inherit"


class FallbackSettings(Model):
    empty_rounds_required: int = Field(default=5, ge=1, le=100)
    evaluation_cooldown_seconds: int = Field(default=3000, ge=30, le=604800)
    max_candidates_per_evaluation: int = Field(default=3, ge=1, le=3)


class TTLSettings(Model):
    time_sensitive_seconds: int = Field(default=14400, ge=60, le=604800)
    explicit_unresolved_need_seconds: int = Field(default=86400, ge=60, le=604800)
    related_discussion_seconds: int = Field(default=14400, ge=60, le=604800)


class GenerationSettings(Model):
    retry_after_failure_seconds: int = Field(default=30, ge=1, le=86400)
    classification_max_retries: int = Field(default=3, ge=0, le=20)
    text_timeout_seconds: int = Field(default=60, ge=1, le=600)
    image_timeout_seconds: int = Field(default=180, ge=1, le=1800)
    video_timeout_seconds: int = Field(default=600, ge=1, le=3600)
    max_media_concurrency_per_project: Literal[1] = 1
    max_retries_after_confirmed_not_submitted: int = Field(default=1, ge=0, le=3)
    daily_image_budget: int | None = Field(default=None, ge=0, le=1000)
    daily_video_budget: int | None = Field(default=None, ge=0, le=1000)
    daily_cost_budget: float | None = Field(default=None, ge=0, le=1000000)
    currency: str = Field(default="credits", min_length=1, max_length=20)
    min_remaining_seconds: int = Field(default=120, ge=0, le=86400)


class PublishingSettings(Model):
    request_timeout_seconds: int = Field(default=30, ge=1, le=120)
    min_account_interval_seconds: int | None = Field(default=None, ge=0, le=86400)
    max_per_account_hour: int | None = Field(default=None, ge=1, le=20)
    max_per_account_day: int | None = Field(default=None, ge=1, le=300)
    retry_after_confirmed_not_submitted_seconds: int = Field(default=60, ge=1, le=86400)
    max_retries_after_confirmed_not_submitted: int = Field(default=1, ge=0, le=3)


class VerificationSettings(Model):
    backoff_seconds: list[int] = Field(default_factory=lambda: [30, 120, 300, 900], min_length=1)
    attention_after_seconds: int = Field(default=3600, ge=1, le=604800)
    pause_polling_after_attention: Literal[True] = True

    @model_validator(mode="after")
    def backoff(self):
        if len(self.backoff_seconds) > 20 or any(not 1 <= n <= 86400 for n in self.backoff_seconds):
            raise ValueError("核验退避间隔必须为 1 至 86400 秒")
        return self


class NotificationSettings(Model):
    retry_delays_seconds: list[int] = Field(default_factory=lambda: [30, 120, 600])
    max_retries_after_confirmed_not_sent: int = Field(default=3, ge=0, le=10)
    # Local hours [start, end). Equal endpoints mean no quiet period.
    quiet_hours: tuple[int, int] = (0, 0)

    @model_validator(mode="after")
    def ranges(self):
        if any(not 0 <= n <= 23 for n in self.quiet_hours):
            raise ValueError("静默时段使用 0 至 23 时")
        if not self.retry_delays_seconds or any(
            not 1 <= n <= 86400 for n in self.retry_delays_seconds
        ):
            raise ValueError("通知重试间隔必须为 1 至 86400 秒")
        return self


class VerifiedFact(Model):
    id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=3000)
    source: str = Field(min_length=1, max_length=1000)
    verified_at: float = Field(gt=0)
    valid_until: float = Field(gt=0)


class InteractionConfig(Model):
    schema_version: Literal[1] = 1
    project_id: str = Field(default="default", pattern=r"^[\w-]{1,80}$")
    policy_key: str = Field(default="default", pattern=r"^[\w-]{1,80}$")
    platform: Literal["x"] = "x"
    enabled: bool = False
    run_mode: Literal["dry_run", "live"] = "dry_run"
    timezone: str = "Asia/Shanghai"
    account_ids: list[str] = Field(default_factory=list, max_length=200)
    account_pool_id: str | None = None
    languages: list[str] = Field(default_factory=lambda: ["en", "zh"], min_length=1)
    audience: str = Field(default="", max_length=3000)
    queries: list[str] = Field(default_factory=list, max_length=20)
    classifier_version: str = Field(default="hf-v1", max_length=100)
    product_knowledge_version: str = Field(default="1", max_length=100)
    facts: list[VerifiedFact] = Field(default_factory=list, max_length=100)
    scan: ScanSettings = Field(default_factory=ScanSettings)
    review: ReviewSettings = Field(default_factory=ReviewSettings)
    fallback: FallbackSettings = Field(default_factory=FallbackSettings)
    ttl: TTLSettings = Field(default_factory=TTLSettings)
    generation: GenerationSettings = Field(default_factory=GenerationSettings)
    publishing: PublishingSettings = Field(default_factory=PublishingSettings)
    verification: VerificationSettings = Field(default_factory=VerificationSettings)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    scan_hours: tuple[int, int] = (0, 0)
    publish_hours: tuple[int, int] = (0, 0)
    observation_offsets_seconds: list[int] = Field(default_factory=lambda: [86400, 259200])

    @model_validator(mode="after")
    def validate_scope(self):
        try:
            ZoneInfo(self.timezone)
        except (KeyError, ValueError):
            raise ValueError("时区名称无效") from None
        if len(set(self.account_ids)) != len(self.account_ids):
            raise ValueError("账号不能重复")
        if len({f.id for f in self.facts}) != len(self.facts):
            raise ValueError("产品事实 ID 不能重复")
        if any(not q.strip() or len(q) > 512 for q in self.queries):
            raise ValueError("查询必须非空且不超过 512 字符")
        if self.enabled and (not self.queries or not (self.account_ids or self.account_pool_id)):
            raise ValueError("启用前必须指定查询和账号或号池")
        if any(not 0 <= n <= 23 for n in (*self.scan_hours, *self.publish_hours)):
            raise ValueError("运行时段使用 0 至 23 时")
        if len(self.observation_offsets_seconds) > 10 or any(
            not 1 <= n <= 2592000 for n in self.observation_offsets_seconds
        ):
            raise ValueError("观察间隔必须为 1 至 2592000 秒")
        return self


class Classification(Model):
    priority: Literal["P0", "P1", "P2", "P3", "EXCLUDED"]
    priority_reason: str = Field(min_length=1, max_length=3000)
    intent_evidence: list[str] = Field(default_factory=list, max_length=20)
    product_fit: str = Field(default="", max_length=3000)
    verified_fact_ids: list[str] = Field(default_factory=list, max_length=100)
    contribution: str = Field(default="", max_length=5000)
    language: str = Field(default="unknown", max_length=30)
    target_account_fit: bool = False
    context_status: Literal["complete", "missing", "inaccessible"] = "missing"
    opportunity_type: Literal["time_sensitive", "explicit_unresolved_need", "related_discussion"]
    recommended_media: Literal["none", "image", "video", "reuse_existing"] = "none"
    quality_status: Literal["passed", "pending", "failed"] = "pending"
    exclusion_reason: str = Field(default="", max_length=3000)
    value: float = Field(default=0, ge=0, le=1)


class ConfigSave(Model):
    config: InteractionConfig
    expected_version: int = Field(default=0, ge=0)
    apply_timing_to_open_batches: bool = False


class CandidateAction(Model):
    action: Literal["adopt", "skip", "defer", "edit", "verify", "verify_media"]
    versions: dict[str, int] = Field(min_length=1, max_length=100)
    account_id: str | None = None
    text: str | None = Field(default=None, max_length=20000)
    defer_until: float | None = None
