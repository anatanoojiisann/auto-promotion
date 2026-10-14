"""Inputs for business measurement; none of these inputs authorize publication."""

import re
from typing import Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator

from .models import Model, OpportunityInput, epoch


class GrowthGoal(Model):
    name: str = Field(min_length=1, max_length=160)
    app_store_url: str
    mode: Literal["live", "simulation"] = "live"
    starts_at: str
    ends_at: str
    timezone: str = "Asia/Shanghai"
    countries: list[str] = Field(default_factory=list, max_length=100)
    campaign_ids: list[str] = Field(default_factory=list, max_length=1000)
    daily_interactions: int | None = Field(default=None, ge=1, le=100000)
    downloads_target: int | None = Field(default=None, ge=1)
    paid_target: int | None = Field(default=None, ge=1)
    max_cac_minor: int | None = Field(default=None, ge=0)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")

    @field_validator("app_store_url")
    @classmethod
    def store_url(cls, value):
        url = urlparse(value)
        if (
            url.scheme != "https"
            or url.netloc != "apps.apple.com"
            or not re.fullmatch(r"/(?:[a-z]{2}/)?app/(?:[^/]+/)?id[0-9]+", url.path)
            or url.query
            or url.fragment
        ):
            raise ValueError("请填写不带渠道参数的官方 App Store 应用链接")
        return value

    @model_validator(mode="after")
    def window(self):
        ZoneInfo(self.timezone)
        if epoch(self.ends_at) <= epoch(self.starts_at):
            raise ValueError("结束时间必须晚于开始时间")
        if len(set(self.campaign_ids)) != len(self.campaign_ids):
            raise ValueError("活动不能重复")
        if any(not re.fullmatch(r"[A-Z]{2}", c) for c in self.countries):
            raise ValueError("国家／地区使用大写两位代码")
        return self


class GrowthChannel(Model):
    name: str = Field(min_length=1, max_length=160)
    campaign_token: str = Field(pattern=r"^[A-Za-z0-9_-]{1,30}$")
    provider_token: str = Field(default="", pattern=r"^(?:|[0-9]{1,30})$")
    campaign_id: str = ""
    account_id: str = ""


class ConversionEvent(Model):
    source: str = Field(min_length=1, max_length=80)
    event_id: str = Field(min_length=1, max_length=160)
    kind: Literal["first_download", "first_generation", "trial_start", "first_charge", "refund"]
    subject_key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,160}$")
    channel_id: str = ""
    occurred_at: str
    evidence_reference: str = Field(min_length=1, max_length=1000)
    country: str = Field(default="", pattern=r"^(?:|[A-Z]{2})$")
    trial_ends_at: str | None = None
    transaction_id: str = Field(default="", max_length=160)
    amount_minor: int = Field(default=0, ge=0, le=10**12)
    currency: str = Field(default="", pattern=r"^(?:|[A-Z]{3})$")

    @model_validator(mode="after")
    def event(self):
        at = epoch(self.occurred_at)
        if self.kind == "trial_start":
            if not self.trial_ends_at or epoch(self.trial_ends_at) <= at:
                raise ValueError("试用事件需要实际试用结束时间")
        elif self.trial_ends_at:
            raise ValueError("仅试用事件填写 trial_ends_at")
        if self.kind in {"first_charge", "refund"}:
            if not self.transaction_id or not self.currency or self.amount_minor <= 0:
                raise ValueError("扣费／退款需要交易编号、币种和真实正金额")
        elif self.transaction_id or self.currency or self.amount_minor:
            raise ValueError("非交易事件不能包含交易金额")
        return self


class ConversionImport(Model):
    items: list[ConversionEvent] = Field(min_length=1, max_length=5000)


class StoreMetric(Model):
    channel_id: str
    day: str
    source: str = Field(min_length=1, max_length=80)
    evidence_reference: str = Field(min_length=1, max_length=1000)
    product_page_views: int | None = Field(default=None, ge=0)
    first_downloads: int | None = Field(default=None, ge=0)
    spend_minor: int | None = Field(default=None, ge=0, le=10**12)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    country: str = Field(default="", pattern=r"^(?:|[A-Z]{2})$")

    @model_validator(mode="after")
    def metric(self):
        from datetime import date

        date.fromisoformat(self.day)
        if all(
            v is None for v in (self.product_page_views, self.first_downloads, self.spend_minor)
        ):
            raise ValueError("至少提供一项指标；未提供的数据不能填零")
        return self


class StoreMetricImport(Model):
    items: list[StoreMetric] = Field(min_length=1, max_length=5000)


class InboxUpdate(Model):
    state: Literal["open", "reviewing", "dismissed", "responded"]
    note: str = Field(default="", max_length=2000)
    followup_task_id: str = ""


class FeedInput(Model):
    account_id: str
    target: str = Field(pattern=r"^@[A-Za-z0-9_]{5,32}$")
    interval_seconds: int = Field(default=1800, ge=900, le=86400)
    limit: int = Field(default=100, ge=1, le=1000)
    enabled: bool = False


class ContentReview(Model):
    payload: OpportunityInput
    expected_revision: int = Field(ge=1)
