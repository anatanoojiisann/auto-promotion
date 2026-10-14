"""Inputs for reusable task settings and local evidence exports."""

from pydantic import Field, field_validator

from .models import Model, epoch


class TemplateSave(Model):
    source_task_id: str
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)


class TemplateTask(Model):
    name: str = Field(min_length=1, max_length=160)
    account_ids: list[str] = Field(min_length=1, max_length=12)
    starts_at: str
    ends_at: str
    target_allowlist: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("starts_at", "ends_at")
    @classmethod
    def explicit_time(cls, value):
        epoch(value)
        return value
