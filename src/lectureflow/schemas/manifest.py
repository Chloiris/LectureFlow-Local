from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lectureflow.constants import PIPELINE_VERSION, SCHEMA_VERSION


class PersistedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TimeRange(PersistedModel):
    start: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    end: float | None = Field(default=None, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_order(self) -> TimeRange:
        if self.end is not None and self.end <= self.start:
            raise ValueError("end must be greater than start")
        return self


class SourceDescriptor(PersistedModel):
    kind: Literal["local", "bilibili", "subtitle"]
    normalized: str
    display_name: str
    bvid: str | None = None
    aid: int | None = None
    page: int | None = Field(default=None, ge=1)
    short_url: bool = False
    subtitle_format: Literal["bilibili_json", "srt", "vtt", "txt"] | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> SourceDescriptor:
        if self.kind == "local" and (self.bvid is not None or self.aid is not None):
            raise ValueError("local sources cannot have a Bilibili identity")
        if self.kind == "subtitle" and self.subtitle_format is None:
            raise ValueError("subtitle sources require subtitle_format")
        return self


class JobManifest(PersistedModel):
    schema_version: str = SCHEMA_VERSION
    pipeline_version: str = PIPELINE_VERSION
    job_id: str
    created_at: str
    updated_at: str
    source: SourceDescriptor
    requested_range: TimeRange
    config_hash: str
    source_fingerprint: dict[str, Any] | None = None
    metadata_path: str | None = None

    @field_validator("schema_version")
    @classmethod
    def require_supported_schema(cls, value: str) -> str:
        if value.split(".", 1)[0] != SCHEMA_VERSION.split(".", 1)[0]:
            raise ValueError(f"unsupported manifest schema version: {value}")
        return value
