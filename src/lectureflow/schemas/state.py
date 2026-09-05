from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lectureflow.constants import MILESTONES, PIPELINE_VERSION, SCHEMA_VERSION


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INVALIDATED = "invalidated"
    SKIPPED = "skipped"


class StateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class StageAttempt(StateModel):
    attempt_id: str
    started_at: str
    finished_at: str | None = None
    input_hash: str
    output_hash: str | None = None
    config_hash: str
    tool: str
    tool_version: str | None = None
    error: str | None = None
    recoverable: bool = True


class StageRecord(StateModel):
    status: StageStatus = StageStatus.PENDING
    attempts: list[StageAttempt] = Field(default_factory=list)


class PipelineState(StateModel):
    schema_version: str = SCHEMA_VERSION
    pipeline_version: str = PIPELINE_VERSION
    job_id: str
    revision: int = Field(default=0, ge=0)
    overall_status: str = "active"
    current_milestone: str | None = None
    last_successful_milestone: str | None = None
    updated_at: str
    stages: dict[str, StageRecord] = Field(
        default_factory=lambda: {name: StageRecord() for name in MILESTONES}
    )

    @field_validator("schema_version")
    @classmethod
    def require_supported_schema(cls, value: str) -> str:
        if value.split(".", 1)[0] != SCHEMA_VERSION.split(".", 1)[0]:
            raise ValueError(f"unsupported pipeline-state schema version: {value}")
        return value

    @model_validator(mode="after")
    def require_all_and_only_known_stages(self) -> PipelineState:
        if set(self.stages) != set(MILESTONES):
            raise ValueError("pipeline state must contain all and only the known stages")
        return self
