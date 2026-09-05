from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

FRAME_SCHEMA_VERSION = "1.0.0"


class FrameModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def require_supported_schema(self) -> FrameModel:
        value = getattr(self, "schema_version", FRAME_SCHEMA_VERSION)
        if value.split(".", 1)[0] != FRAME_SCHEMA_VERSION.split(".", 1)[0]:
            raise ValueError(f"unsupported frame schema version: {value}")
        return self


class MediaManifest(FrameModel):
    schema_version: str = FRAME_SCHEMA_VERSION
    source_kind: Literal["local", "bilibili"]
    source_id: str
    part_id: str | None = None
    selected_part: int | None = Field(default=None, ge=1)
    part_selection_reason: str | None = None
    source_url: str | None = None
    requested_start: float = Field(ge=0, allow_inf_nan=False)
    requested_end: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    media_path: str | None = None
    storage: Literal["external_local", "job_local"]
    container: str
    duration: float = Field(ge=0, allow_inf_nan=False)
    source_duration: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    file_size: int = Field(ge=0)
    sha256: str
    video_codec: str | None = None
    audio_included: bool
    format_selector: str | None = None
    tool_versions: dict[str, str | None]
    input_hash: str
    config_hash: str
    generated_at: str

    @model_validator(mode="after")
    def validate_range_and_path(self) -> MediaManifest:
        if self.requested_end is not None and self.requested_end <= self.requested_start:
            raise ValueError("requested_end must be greater than requested_start")
        if self.storage == "job_local" and not self.media_path:
            raise ValueError("job-local media requires a relative media_path")
        if self.media_path and (
            self.media_path.startswith("/") or ".." in self.media_path.split("/")
        ):
            raise ValueError("media_path must remain relative to the job")
        return self


class FrameCandidate(FrameModel):
    schema_version: str = FRAME_SCHEMA_VERSION
    candidate_id: str = Field(pattern=r"^C[0-9]{6,}$")
    timestamp: float = Field(ge=0, allow_inf_nan=False)
    path: str
    reason: list[Literal["scene_change", "subtitle_cue", "periodic"]]
    scene_score: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    cue_terms: list[str] = Field(default_factory=list)
    nearby_segment_ids: list[str] = Field(default_factory=list)
    cue_offsets: list[float] = Field(default_factory=list)
    cue_offset_types: list[Literal["before", "at", "after"]] = Field(default_factory=list)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    sha256: str
    perceptual_hash: str
    sharpness: float = Field(ge=0, allow_inf_nan=False)
    black_ratio: float = Field(ge=0, le=1, allow_inf_nan=False)
    mean_luma: float = Field(ge=0, le=255, allow_inf_nan=False)
    luma_stddev: float = Field(ge=0, allow_inf_nan=False)
    quality_flags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_relative_path(self) -> FrameCandidate:
        if self.path.startswith("/") or ".." in self.path.split("/"):
            raise ValueError("candidate path must be job-relative")
        if not self.reason:
            raise ValueError("candidate needs at least one reason")
        return self


class FrameRecord(FrameModel):
    schema_version: str = FRAME_SCHEMA_VERSION
    frame_id: str = Field(pattern=r"^F[0-9]{6,}$")
    candidate_id: str = Field(pattern=r"^C[0-9]{6,}$")
    timestamp: float = Field(ge=0, allow_inf_nan=False)
    path: str
    reason: list[Literal["scene_change", "subtitle_cue", "periodic"]]
    scene_score: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    cue_terms: list[str] = Field(default_factory=list)
    nearby_segment_ids: list[str] = Field(default_factory=list)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    sha256: str
    perceptual_hash: str
    selected: bool = True
    quality_flags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_relative_path(self) -> FrameRecord:
        if self.path.startswith("/") or ".." in self.path.split("/"):
            raise ValueError("frame path must be job-relative")
        return self


class FrameRunResult(FrameModel):
    schema_version: str = FRAME_SCHEMA_VERSION
    job_id: str
    status: Literal["frames_ready"]
    cache_hit: bool
    candidate_count: int
    deduplicated_count: int
    final_count: int
    repaired_frame_ids: list[str] = Field(default_factory=list)
    frames_path: str
    contact_sheet_paths: list[str]
    coverage_path: str
    details: dict[str, Any] = Field(default_factory=dict)
