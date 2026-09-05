from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lectureflow.atomic import atomic_write_json
from lectureflow.hashing import hash_file

VISION_SMOKE_SCHEMA_VERSION = "1.0"


class VisionSmokeValidationError(ValueError):
    """Raised when M3A evidence is incomplete or cannot be audited."""


class VisualObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = VISION_SMOKE_SCHEMA_VERSION
    observation_id: str = Field(pattern=r"^V[0-9]{6,}$")
    frame_id: str = Field(pattern=r"^(?:F|C)[0-9]{6,}$|^CS[0-9]{3,}$")
    timestamp: float = Field(ge=0, allow_inf_nan=False)
    image_path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_opened: bool
    image_tool: str = Field(min_length=1)
    image_kind: Literal[
        "contact_sheet", "final_frame", "black_control", "duplicate_control", "crop"
    ]
    visual_type: list[
        Literal[
            "slide",
            "formula",
            "code",
            "diagram",
            "chart",
            "ui",
            "speaker",
            "black_frame",
            "other",
        ]
    ]
    visible_title: str | None = None
    visible_text: list[str] = Field(default_factory=list)
    formulas: list[str] = Field(default_factory=list)
    code_blocks: list[str] = Field(default_factory=list)
    diagram_findings: list[str] = Field(default_factory=list)
    visual_summary: str = Field(min_length=1)
    confidence: Literal["high", "medium", "low"]
    unreadable_regions: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    duplicate_of: str | None = None
    source: Literal["current_codex_vision"]
    original_high_resolution: bool
    contact_sheet_only: bool
    crop_needed: bool
    parent_frame_id: str | None = None
    crop_region: tuple[int, int, int, int] | None = None
    control_expectation: str | None = None
    m2_quality_flags: list[str] | None = None
    control_result: str | None = None
    analyzed_at: str

    @model_validator(mode="after")
    def validate_path_and_kind(self) -> VisualObservation:
        if Path(self.image_path).is_absolute() or ".." in Path(self.image_path).parts:
            raise ValueError("image_path must be job-relative")
        if self.image_kind == "contact_sheet" and not self.contact_sheet_only:
            raise ValueError("contact sheet observations must be marked contact_sheet_only")
        if self.image_kind == "final_frame" and not self.original_high_resolution:
            raise ValueError("final frames must be opened at original resolution")
        if self.image_kind == "crop" and (self.parent_frame_id is None or self.crop_region is None):
            raise ValueError("crop observations require parent_frame_id and crop_region")
        return self


class VisionSmokeProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = VISION_SMOKE_SCHEMA_VERSION
    job_id: str
    source_video: str
    part: int = Field(ge=1)
    time_range: tuple[float, float]
    contact_sheets_opened: list[str]
    original_frames_opened: list[str]
    control_frames_opened: list[str]
    crops_opened: list[str]
    image_tool: str = Field(min_length=1)
    external_model_api_used: bool
    ocr_used: bool
    asr_used: bool
    video_redownloaded: bool
    frames_reextracted: bool
    model: str
    completed_at: str
    opened_image_count: int = Field(ge=1)
    true_black_control_available: bool
    black_control_limitation: str | None = None

    @model_validator(mode="after")
    def validate_range(self) -> VisionSmokeProvenance:
        if self.time_range[1] <= self.time_range[0]:
            raise ValueError("time_range end must be greater than start")
        for path in self.opened_paths:
            if Path(path).is_absolute() or ".." in Path(path).parts:
                raise ValueError("opened image paths must be job-relative")
        return self

    @property
    def opened_paths(self) -> list[str]:
        return [
            *self.contact_sheets_opened,
            *self.original_frames_opened,
            *self.control_frames_opened,
            *self.crops_opened,
        ]


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError) as error:
        raise VisionSmokeValidationError(f"cannot read JSONL {path}: {error}") from error


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise VisionSmokeValidationError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise VisionSmokeValidationError(f"expected JSON object: {path}")
    return value


def write_visual_observation_schema(path: Path) -> None:
    atomic_write_json(path, VisualObservation.model_json_schema())


def _artifact_registry(job_root: Path) -> dict[str, tuple[str, str]]:
    registry: dict[str, tuple[str, str]] = {}
    for raw in _read_jsonl(job_root / "frames/frames.jsonl"):
        registry[str(raw["frame_id"])] = (str(raw["path"]), str(raw["sha256"]))
    for raw in _read_jsonl(job_root / "frames/frame-candidates.jsonl"):
        registry[str(raw["candidate_id"])] = (str(raw["path"]), str(raw["sha256"]))
    contact_manifest = _read_json(job_root / "frames/contact-sheet-manifest.json")
    for raw in contact_manifest.get("sheets", []):
        if not isinstance(raw, dict):
            raise VisionSmokeValidationError("invalid contact sheet manifest entry")
        registry[str(raw["sheet_id"])] = (str(raw["path"]), str(raw["sha256"]))
    return registry


def validate_vision_smoke(job_root: Path) -> tuple[list[VisualObservation], VisionSmokeProvenance]:
    """Validate the small, human/Codex-authored M3A evidence bundle."""
    analysis_root = job_root / "analyses/vision-smoke"
    try:
        observations = [
            VisualObservation.model_validate(value)
            for value in _read_jsonl(analysis_root / "visual-observations.jsonl")
        ]
        provenance = VisionSmokeProvenance.model_validate(
            _read_json(analysis_root / "analysis-provenance.json")
        )
    except (KeyError, ValueError) as error:
        if isinstance(error, VisionSmokeValidationError):
            raise
        raise VisionSmokeValidationError(f"invalid M3A schema: {error}") from error

    if not observations:
        raise VisionSmokeValidationError("M3A has no visual observations")
    if len({item.observation_id for item in observations}) != len(observations):
        raise VisionSmokeValidationError("observation_id values must be unique")
    if any(not item.image_opened for item in observations):
        raise VisionSmokeValidationError("image_opened=false cannot be visual evidence")

    registry = _artifact_registry(job_root)
    final_ids = {key for key in registry if key.startswith("F")}
    opened_final_ids = {item.frame_id for item in observations if item.image_kind == "final_frame"}
    if opened_final_ids != final_ids:
        missing = sorted(final_ids - opened_final_ids)
        extra = sorted(opened_final_ids - final_ids)
        raise VisionSmokeValidationError(
            f"final frame observations differ: missing={missing} extra={extra}"
        )

    observation_paths: set[str] = set()
    for item in observations:
        if item.image_kind == "crop":
            expected = (item.image_path, item.sha256)
        else:
            expected = registry.get(item.frame_id)
            if expected is None:
                raise VisionSmokeValidationError(f"unknown frame_id: {item.frame_id}")
        if expected != (item.image_path, item.sha256):
            raise VisionSmokeValidationError(f"manifest path/SHA mismatch for {item.frame_id}")
        image = job_root / item.image_path
        if not image.is_file():
            raise VisionSmokeValidationError(f"referenced image does not exist: {item.image_path}")
        if hash_file(image) != item.sha256:
            raise VisionSmokeValidationError(f"image SHA-256 mismatch: {item.image_path}")
        observation_paths.add(item.image_path)

    provenance_paths = provenance.opened_paths
    if len(provenance_paths) != len(set(provenance_paths)):
        raise VisionSmokeValidationError("provenance contains duplicate opened image paths")
    if set(provenance_paths) != observation_paths:
        raise VisionSmokeValidationError("provenance opened images differ from observations")
    if provenance.opened_image_count != len(provenance_paths):
        raise VisionSmokeValidationError("opened_image_count differs from provenance path count")
    if provenance.external_model_api_used:
        raise VisionSmokeValidationError("external model API use invalidates M3A")
    if provenance.ocr_used or provenance.asr_used:
        raise VisionSmokeValidationError("OCR or ASR use invalidates this M3A vision smoke")
    if provenance.video_redownloaded or provenance.frames_reextracted:
        raise VisionSmokeValidationError("M3A must reuse M2 media and frames")

    black_controls = [item for item in observations if item.image_kind == "black_control"]
    duplicate_controls = [item for item in observations if item.image_kind == "duplicate_control"]
    if not black_controls or len(duplicate_controls) < 2:
        raise VisionSmokeValidationError("negative controls are incomplete")
    if not any(item.duplicate_of is not None for item in duplicate_controls):
        raise VisionSmokeValidationError("duplicate control pair is not linked")
    return observations, provenance
