from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lectureflow.schemas.transcript import TranscriptSegment

ASR_SCHEMA_VERSION = "1.0"


class ASRModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ASRBackendInfo(ASRModel):
    schema_version: Literal["1.0"] = ASR_SCHEMA_VERSION
    backend: Literal["mlx-whisper", "faster-whisper", "vibeasr-bitnet"]
    available: bool
    package_version: str | None
    model: str
    revision: str | None
    model_path: str | None
    model_cached: bool
    reason: str


class TranscriptionRequest(ASRModel):
    schema_version: Literal["1.0"] = ASR_SCHEMA_VERSION
    job_id: str
    audio_path: Path
    raw_output_path: Path
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    language: str = Field(min_length=1)
    model: str = Field(min_length=1)
    model_path: Path
    word_timestamps: bool = False
    context: str | None = Field(default=None, max_length=4096)
    task: Literal["transcribe"] = "transcribe"
    part_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_range(self) -> TranscriptionRequest:
        if self.end <= self.start:
            raise ValueError("transcription end must be greater than start")
        if self.context is not None and any(
            character in self.context for character in ("\0", "\n", "\r")
        ):
            raise ValueError("transcription context must be a single line without NUL")
        return self


class ASRTranscriptResult(ASRModel):
    schema_version: Literal["1.0"] = ASR_SCHEMA_VERSION
    backend: Literal["mlx-whisper", "vibeasr-bitnet"]
    model: str
    model_revision: str
    language: str
    duration_seconds: float = Field(gt=0, allow_inf_nan=False)
    segments: list[TranscriptSegment]
    raw_output_path: str
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)
    real_time_factor: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_segments(self) -> ASRTranscriptResult:
        if not self.segments:
            raise ValueError("ASR returned no transcript segments")
        for segment in self.segments:
            if segment.end > self.duration_seconds + 0.001:
                raise ValueError(f"segment exceeds requested duration: {segment.segment_id}")
        return self
