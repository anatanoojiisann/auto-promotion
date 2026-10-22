"""Versioned high-frequency policy. Durations are seconds; stored timestamps are UTC."""

from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator

from .models import Model


class ScanSettings(Model):
    source: Literal["x_api", "imported_results"] = "x_api"
    schedule_mode: Literal["fixed_interval", "random_interval"] = "fixed_interval"
    min_interval_seconds: int = Field(default=3000, ge=30, le=604800, strict=True)
    max_interval_seconds: int = Field(default=4200, ge=30, le=604800, strict=True)
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
        if self.min_interval_seconds > self.max_interval_seconds:
            raise ValueError("最小扫描间隔不能大于最大间隔（秒）")
        maximum = (
            self.max_interval_seconds
            if self.schedule_mode == "random_interval"
            else self.interval_seconds
        )
        if self.long_gap_reset_seconds < maximum * 2:
            raise ValueError("长期中断阈值至少为扫描间隔的两倍")
        return self


class ReviewSettings(Model):
    enabled: bool = True
    batch_wait_seconds: int = Field(default=18000, ge=0, le=604800)
    expiry_alert_enabled: bool = True
    expiry_alert_lead_seconds: int = Field(default=1800, ge=0, le=86400)
    notification_channel: Literal["inherit", "workbench", "webhook"] = "inherit"


class FallbackSettings(Model):
    enabled: bool = True
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
    enqueue_delay_min_seconds: int = Field(default=600, ge=0, le=86400)
    enqueue_delay_max_seconds: int = Field(default=900, ge=0, le=86400)
    request_timeout_seconds: int = Field(default=30, ge=1, le=120)
    min_account_interval_seconds: int | None = Field(default=None, ge=0, le=86400)
    max_per_account_hour: int | None = Field(default=None, ge=1, le=20)
    max_per_account_day: int | None = Field(default=None, ge=1, le=300)
    retry_after_confirmed_not_submitted_seconds: int = Field(default=60, ge=1, le=86400)
    max_retries_after_confirmed_not_submitted: int = Field(default=1, ge=0, le=3)

    @model_validator(mode="after")
    def delay_range(self):
        if self.enqueue_delay_max_seconds < self.enqueue_delay_min_seconds:
            raise ValueError("入队等待上限不能小于下限")
        return self


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
    product_id: str | None = None
    platforms: list[str] = Field(default_factory=list)
    versions: list[str] = Field(default_factory=list)
    release_status: Literal[
        "source_integrated", "test_verified", "release_verified", "unavailable"
    ] = "release_verified"
    constraints: list[str] = Field(default_factory=list)


class QueryGroup(Model):
    id: str = Field(pattern=r"^[\w-]{1,80}$")
    expression: str = Field(min_length=1, max_length=512)
    product_ids: list[str] = Field(default_factory=list)
    source_type: Literal["public_need", "public_mentions", "conversation", "private"] = (
        "public_need"
    )
    visibility_scope: str = Field(default="public", min_length=1, max_length=100)
    version: str = Field(default="1", min_length=1, max_length=100)
    enabled: bool = True
    max_reads_per_round: int = Field(default=20, ge=1, le=1000)
    required_any_phrases: list[str] = Field(default_factory=list, max_length=20)
    required_intent_phrases: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def read_filter(self):
        if any(
            not phrase.strip() or len(phrase) > 100
            for phrase in (self.required_any_phrases + self.required_intent_phrases)
        ):
            raise ValueError("查询本地筛选短语必须非空且不超过 100 字符")
        return self


class SearchAccounts(Model):
    account_pool_id: str | None = None
    account_ids: list[str] = Field(default_factory=list, max_length=200)
    rotation: Literal["round_robin"] = "round_robin"
    require_different_from_previous: bool = True


class ProductSettings(Model):
    product_id: str = Field(pattern=r"^[\w-]{1,80}$")
    enabled: bool = True
    execution_pool_id: str | None = None
    execution_account_ids: list[str] = Field(default_factory=list, max_length=200)
    preferred_execution_account_ids_by_language: dict[str, list[str]] = Field(default_factory=dict)
    languages: list[str] = Field(default_factory=lambda: ["en", "zh"], min_length=1)
    knowledge_version: str = "1"
    target_platforms: list[str] = Field(default_factory=list)
    target_versions: list[str] = Field(default_factory=list)
    query_groups: list[QueryGroup] = Field(default_factory=list, max_length=40)
    facts: list[VerifiedFact] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def account_preferences(self):
        if any(
            language not in self.languages
            or not choices
            or len(choices) != len(set(choices))
            or not set(choices) <= set(self.execution_account_ids)
            for language, choices in self.preferred_execution_account_ids_by_language.items()
        ):
            raise ValueError("语言优先账号必须在该产品的执行账号池内")
        return self


class InteractionConfig(Model):
    schema_version: Literal[1, 2] = 1
    strategy_profile: Literal["legacy", "joint_p0_p1"] = "legacy"
    dedupe_scope_id: str | None = Field(default=None, pattern=r"^[\w-]{1,80}$")
    historical_project_ids: list[str] = Field(default_factory=list)
    search_accounts: SearchAccounts = Field(default_factory=SearchAccounts)
    products: list[ProductSettings] = Field(default_factory=list, max_length=20)
    product_tie_break_order: list[str] = Field(default_factory=lambda: ["aigram", "aurax"])
    project_id: str = Field(default="default", pattern=r"^[\w-]{1,80}$")
    policy_key: str = Field(default="default", pattern=r"^[\w-]{1,80}$")
    platform: Literal["x"] = "x"
    enabled: bool = False
    run_mode: Literal["dry_run", "live", "discovery_only"] = "dry_run"
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
        if self.strategy_profile == "joint_p0_p1":
            if self.schema_version != 2:
                raise ValueError("联合策略使用 schema_version=2")
            self.review.enabled = self.fallback.enabled = False
            products = {p.product_id: p for p in self.products if p.enabled}
            if len({p.product_id for p in self.products}) != len(self.products):
                raise ValueError("产品 ID 不能重复")
            fact_ids = set()
            for p in self.products:
                if len({q.id for q in p.query_groups}) != len(p.query_groups):
                    raise ValueError("同产品查询组 ID 不能重复")
                for q in p.query_groups:
                    if not q.expression.strip() or not set(q.product_ids) <= products.keys():
                        raise ValueError("查询表达式或产品关联无效")
                for f in p.facts:
                    if f.product_id != p.product_id or f.id in fact_ids:
                        raise ValueError("事实必须有唯一 ID 和正确的产品归属")
                    fact_ids.add(f.id)
            required_products = {"aigram", "aurax"}
            valid_products = (
                bool(products) and set(products) <= required_products
                if self.run_mode == "discovery_only"
                else set(products) == required_products
            )
            if self.enabled and (
                not valid_products
                or any(not any(q.enabled for q in p.query_groups) for p in products.values())
            ):
                raise ValueError("启用联合扫描前需为所选产品配置查询组")
            if self.enabled and not (
                self.search_accounts.account_ids or self.search_accounts.account_pool_id
            ):
                raise ValueError("启用前必须指定搜索账号池")
            if len(set(self.search_accounts.account_ids)) != len(self.search_accounts.account_ids):
                raise ValueError("搜索账号不能重复")
        elif self.enabled and (not self.queries or not (self.account_ids or self.account_pool_id)):
            raise ValueError("启用前必须指定查询和账号或号池")
        if any(not 0 <= n <= 23 for n in (*self.scan_hours, *self.publish_hours)):
            raise ValueError("运行时段使用 0 至 23 时")
        if len(self.observation_offsets_seconds) > 10 or any(
            not 1 <= n <= 2592000 for n in self.observation_offsets_seconds
        ):
            raise ValueError("观察间隔必须为 1 至 2592000 秒")
        return self

    @property
    def joint(self):
        return self.strategy_profile == "joint_p0_p1"

    @property
    def scope_id(self):
        return self.dedupe_scope_id or self.project_id

    def active_facts(self, now, product_id=None):
        if not self.joint:
            return [f for f in self.facts if f.verified_at <= now < f.valid_until]
        return [
            f
            for p in self.products
            if p.enabled and (not product_id or p.product_id == product_id)
            for f in p.facts
            if f.verified_at <= now < f.valid_until
            and f.release_status == "release_verified"
            and (not f.platforms or bool(set(f.platforms) & set(p.target_platforms)))
            and (not f.versions or bool(set(f.versions) & set(p.target_versions)))
        ]


def joint_defaults():
    return InteractionConfig(
        schema_version=2,
        strategy_profile="joint_p0_p1",
        policy_key="aigram_aurax_joint",
        scan=ScanSettings(
            schedule_mode="random_interval",
            interval_seconds=3600,
            initial_lookback_seconds=7200,
            long_gap_reset_seconds=9000,
        ),
        products=[ProductSettings(product_id=p) for p in ("aigram", "aurax")],
    )


class IntentEvidence(Model):
    source_event_id: str
    field: Literal["text", "context"]
    quote: str = Field(min_length=1, max_length=3000)


class ProductMatch(Model):
    product_id: str
    verified_fact_ids: list[str] = Field(default_factory=list)
    satisfied_constraints: list[str] = Field(default_factory=list)
    unsatisfied_constraints: list[str] = Field(default_factory=list)


class Classification(Model):
    classification_status: Literal[
        "classified", "pending_context", "pending_facts", "model_failed"
    ] = "classified"
    intent_type: str = ""
    unresolved: bool = False
    matched_product_ids: list[str] = Field(default_factory=list)
    primary_product_id: str | None = None
    explicit_product_id: str | None = None
    product_matches: list[ProductMatch] = Field(default_factory=list)
    primary_product_reason: str = ""
    priority: Literal["P0", "P1", "P2", "P3", "EXCLUDED"]
    priority_reason: str = Field(min_length=1, max_length=3000)
    intent_evidence: list[str | IntentEvidence] = Field(default_factory=list, max_length=20)
    product_fit: str = Field(default="", max_length=3000)
    verified_fact_ids: list[str] = Field(default_factory=list, max_length=100)
    contribution: str = Field(default="", max_length=5000)
    language: str = Field(default="unknown", max_length=30)
    target_account_fit: bool = False
    context_status: Literal["complete", "missing", "inaccessible"] = "missing"
    opportunity_type: Literal[
        "time_sensitive", "explicit_unresolved_need", "related_discussion"
    ] = "explicit_unresolved_need"
    recommended_media: Literal["none", "image", "video", "reuse_existing"] = "none"
    quality_status: Literal["passed", "pending", "failed"] = "pending"
    exclusion_reason: str = Field(default="", max_length=3000)
    value: float = Field(default=0, ge=0, le=1)


class ConfigSave(Model):
    config: InteractionConfig
    expected_version: int = Field(default=0, ge=0)
    apply_timing_to_open_batches: bool = False


class ScheduleChange(Model):
    expected_version: int = Field(ge=1)


class CandidateAction(Model):
    action: Literal["adopt", "skip", "defer", "edit", "verify", "verify_media"]
    versions: dict[str, int] = Field(min_length=1, max_length=100)
    account_id: str | None = None
    text: str | None = Field(default=None, max_length=20000)
    defer_until: float | None = None


class ImportedSource(Model):
    # Public data only. Permissions, model grades and coverage claims are not accepted.
    id: str | None = Field(default=None, strict=True, pattern=r"^[0-9]{1,30}$")
    url: str | None = Field(default=None, max_length=1000)
    text: str | None = Field(default=None, min_length=1, max_length=30000)
    legacy_candidate_id: str | None = Field(default=None, min_length=1, max_length=100)
    author_id: str = Field(min_length=1, max_length=100)
    source_event_at: float = Field(gt=0, allow_inf_nan=False)
    context: str = Field(default="", max_length=50000)
    context_status: Literal["complete", "missing", "inaccessible"] = "missing"
    language: str = Field(default="unknown", max_length=30)

    @field_validator("source_event_at", mode="before")
    @classmethod
    def timestamp_not_boolean(cls, value):
        if isinstance(value, bool):
            raise ValueError("原帖时间必须是明确 UTC 时间戳")
        return value

    @model_validator(mode="after")
    def public_source(self):
        if not self.legacy_candidate_id and not (self.id and self.url and self.text):
            raise ValueError("导入须有完整公开来源，或原候选引用及原帖元数据")
        return self


class SourceImport(Model):
    sources: list[ImportedSource] = Field(min_length=1, max_length=100)
