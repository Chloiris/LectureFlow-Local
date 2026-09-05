from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TRANSCRIPT_SCHEMA_VERSION = "1.0.0"
TranscriptSource = Literal[
    "uploader",
    "bilibili_ai",
    "local_file",
    "mlx_whisper",
    "faster_whisper",
    "vibeasr_bitnet",
]


class TranscriptModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def require_supported_schema(self) -> TranscriptModel:
        value = getattr(self, "schema_version", None)
        if (
            value is not None
            and value.split(".", 1)[0] != TRANSCRIPT_SCHEMA_VERSION.split(".", 1)[0]
        ):
            raise ValueError(f"unsupported transcript schema version: {value}")
        return self


class TranscriptSegment(TranscriptModel):
    schema_version: str = TRANSCRIPT_SCHEMA_VERSION
    segment_id: str = Field(pattern=r"^T[0-9]{6,}$")
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(ge=0, allow_inf_nan=False)
    text_raw: str
    text_clean: str
    source: TranscriptSource
    language: str = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    speaker: str | None = None
    chapter_id: str | None = None
    part_id: str = Field(min_length=1)
    source_ref: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_time_order(self) -> TranscriptSegment:
        if self.end < self.start:
            raise ValueError("segment end must be greater than or equal to start")
        return self


class UntimedTranscript(TranscriptModel):
    schema_version: str = TRANSCRIPT_SCHEMA_VERSION
    kind: Literal["untimed_transcript"] = "untimed_transcript"
    text_raw: str
    text_clean: str
    source: TranscriptSource
    language: str = Field(min_length=1)
    part_id: str = Field(min_length=1)
    source_ref: dict[str, Any] = Field(default_factory=dict)


class TranscriptDocument(TranscriptModel):
    schema_version: str = TRANSCRIPT_SCHEMA_VERSION
    timed: bool
    source: TranscriptSource
    language: str = Field(min_length=1)
    part_id: str = Field(min_length=1)
    source_ref: dict[str, Any] = Field(default_factory=dict)
    segments: list[TranscriptSegment] = Field(default_factory=list)
    untimed: UntimedTranscript | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> TranscriptDocument:
        if self.timed and self.untimed is not None:
            raise ValueError("timed documents cannot contain an untimed payload")
        if not self.timed and (self.segments or self.untimed is None):
            raise ValueError("untimed documents require only an untimed payload")
        if any(segment.part_id != self.part_id for segment in self.segments):
            raise ValueError("all segments must belong to the document part")
        if any(segment.source != self.source for segment in self.segments):
            raise ValueError("all segments must match the document source")
        if any(segment.language != self.language for segment in self.segments):
            raise ValueError("all segments must match the document language")
        if len({segment.segment_id for segment in self.segments}) != len(self.segments):
            raise ValueError("segment IDs must be unique within a document")
        if self.untimed is not None and (
            self.untimed.source != self.source
            or self.untimed.language != self.language
            or self.untimed.part_id != self.part_id
        ):
            raise ValueError("untimed payload identity must match its document")
        return self


class TranscriptResult(TranscriptModel):
    schema_version: str = TRANSCRIPT_SCHEMA_VERSION
    backend: Literal["mlx_whisper", "faster_whisper", "vibeasr_bitnet"]
    model: str | None = None
    language: str = Field(min_length=1)
    documents: list[TranscriptDocument]
    generated_at: str


class SubtitleCandidate(TranscriptModel):
    candidate_id: str
    source: Literal["uploader", "bilibili_ai", "local_file"]
    language: str
    language_name: str | None = None
    part_id: str
    part_number: int | None = None
    cid: int | None = None
    subtitle_url: str | None = None
    published_at: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SubtitleSelection(TranscriptModel):
    part_id: str
    selected_candidate_id: str | None
    selected_source: Literal["uploader", "bilibili_ai", "local_file"] | None
    reason: str
    candidates: list[SubtitleCandidate]
