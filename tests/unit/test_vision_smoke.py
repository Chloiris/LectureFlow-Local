from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from lectureflow.hashing import hash_file
from lectureflow.vision_smoke import (
    VisionSmokeProvenance,
    VisionSmokeValidationError,
    VisualObservation,
    validate_vision_smoke,
    write_visual_observation_schema,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def _write_jsonl(path: Path, values: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{json.dumps(value)}\n" for value in values))


def _observation(
    frame_id: str,
    image_path: str,
    sha256: str,
    image_kind: str,
    *,
    observation_id: str,
    duplicate_of: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "observation_id": observation_id,
        "frame_id": frame_id,
        "timestamp": 1.0,
        "image_path": image_path,
        "sha256": sha256,
        "image_opened": True,
        "image_tool": "view_image",
        "image_kind": image_kind,
        "visual_type": ["slide"],
        "visible_title": "Test",
        "visible_text": [],
        "formulas": [],
        "code_blocks": [],
        "diagram_findings": [],
        "visual_summary": "Visible test image.",
        "confidence": "high",
        "unreadable_regions": [],
        "uncertainties": [],
        "duplicate_of": duplicate_of,
        "source": "current_codex_vision",
        "original_high_resolution": image_kind != "contact_sheet",
        "contact_sheet_only": image_kind == "contact_sheet",
        "crop_needed": False,
        "analyzed_at": "2026-08-13T00:00:00Z",
    }


@pytest.fixture
def vision_job(tmp_path: Path) -> Path:
    artifacts = [
        ("F000001", "frames/originals/F000001.png", "final_frame"),
        ("C000001", "frames/candidates/C000001.png", "black_control"),
        ("C000002", "frames/candidates/C000002.png", "duplicate_control"),
        ("C000003", "frames/candidates/C000003.png", "duplicate_control"),
        ("CS001", "frames/contact-sheets/contact-sheet-001.jpg", "contact_sheet"),
    ]
    records: list[dict[str, object]] = []
    candidates: list[dict[str, object]] = []
    observations: list[dict[str, object]] = []
    for index, (frame_id, relative, kind) in enumerate(artifacts, 1):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8), (index * 20, 0, 0)).save(path)
        sha256 = hash_file(path)
        if frame_id.startswith("F"):
            records.append({"frame_id": frame_id, "path": relative, "sha256": sha256})
        elif frame_id.startswith("C"):
            candidates.append({"candidate_id": frame_id, "path": relative, "sha256": sha256})
        observations.append(
            _observation(
                frame_id,
                relative,
                sha256,
                kind,
                observation_id=f"V{index:06d}",
                duplicate_of="C000002" if frame_id == "C000003" else None,
            )
        )
    _write_jsonl(tmp_path / "frames/frames.jsonl", records)
    _write_jsonl(tmp_path / "frames/frame-candidates.jsonl", candidates)
    contact = observations[-1]
    _write_json(
        tmp_path / "frames/contact-sheet-manifest.json",
        {
            "sheets": [
                {
                    "sheet_id": "CS001",
                    "path": contact["image_path"],
                    "sha256": contact["sha256"],
                }
            ]
        },
    )
    analysis = tmp_path / "analyses/vision-smoke"
    _write_jsonl(analysis / "visual-observations.jsonl", observations)
    _write_json(
        analysis / "analysis-provenance.json",
        {
            "schema_version": "1.0",
            "job_id": "test-job",
            "source_video": "BVtest",
            "part": 1,
            "time_range": [0, 300],
            "contact_sheets_opened": [artifacts[-1][1]],
            "original_frames_opened": [artifacts[0][1]],
            "control_frames_opened": [item[1] for item in artifacts[1:4]],
            "crops_opened": [],
            "image_tool": "view_image",
            "external_model_api_used": False,
            "ocr_used": False,
            "asr_used": False,
            "video_redownloaded": False,
            "frames_reextracted": False,
            "model": "current Codex conversation model",
            "completed_at": "2026-08-13T00:00:00Z",
            "opened_image_count": 5,
            "true_black_control_available": True,
            "black_control_limitation": None,
        },
    )
    return tmp_path


def _load(path: Path) -> object:
    return json.loads(path.read_text())


def _rewrite_observations(job: Path, mutate: object) -> None:
    path = job / "analyses/vision-smoke/visual-observations.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    mutate(records)
    _write_jsonl(path, records)


def test_valid_visual_observation_bundle_passes(vision_job: Path) -> None:
    observations, provenance = validate_vision_smoke(vision_job)
    assert len(observations) == 5
    assert provenance.image_tool == "view_image"


def test_visual_observation_without_frame_id_fails() -> None:
    raw = _observation(
        "F000001", "frames/originals/F000001.png", "a" * 64, "final_frame", observation_id="V000001"
    )
    del raw["frame_id"]
    with pytest.raises(ValidationError):
        VisualObservation.model_validate(raw)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"image_path": "/tmp/frame.png"}, "job-relative"),
        ({"image_kind": "contact_sheet", "contact_sheet_only": False}, "contact_sheet_only"),
        ({"original_high_resolution": False}, "original resolution"),
        ({"image_kind": "crop"}, "parent_frame_id"),
    ],
)
def test_visual_observation_enforces_image_provenance(
    changes: dict[str, object], message: str
) -> None:
    raw = _observation(
        "F000001", "frames/originals/F000001.png", "a" * 64, "final_frame", observation_id="V000001"
    )
    raw.update(changes)
    with pytest.raises(ValidationError, match=message):
        VisualObservation.model_validate(raw)


def test_visual_observation_schema_can_be_written(tmp_path: Path) -> None:
    path = tmp_path / "visual-observations.schema.json"
    write_visual_observation_schema(path)
    schema = _load(path)
    assert isinstance(schema, dict)
    assert "frame_id" in schema["required"]


def test_reference_to_missing_image_fails(vision_job: Path) -> None:
    (vision_job / "frames/originals/F000001.png").unlink()
    with pytest.raises(VisionSmokeValidationError, match="does not exist"):
        validate_vision_smoke(vision_job)


def test_sha256_mismatch_fails(vision_job: Path) -> None:
    _rewrite_observations(vision_job, lambda rows: rows[0].update(sha256="0" * 64))
    with pytest.raises(VisionSmokeValidationError, match="path/SHA mismatch"):
        validate_vision_smoke(vision_job)


def test_changed_image_content_sha256_mismatch_fails(vision_job: Path) -> None:
    Image.new("RGB", (8, 8), "white").save(vision_job / "frames/originals/F000001.png")
    with pytest.raises(VisionSmokeValidationError, match="image SHA-256 mismatch"):
        validate_vision_smoke(vision_job)


def test_image_opened_false_cannot_be_evidence(vision_job: Path) -> None:
    _rewrite_observations(vision_job, lambda rows: rows[0].update(image_opened=False))
    with pytest.raises(VisionSmokeValidationError, match="image_opened=false"):
        validate_vision_smoke(vision_job)


def test_provenance_claim_without_observation_fails(vision_job: Path) -> None:
    path = vision_job / "analyses/vision-smoke/analysis-provenance.json"
    value = _load(path)
    assert isinstance(value, dict)
    value["crops_opened"] = ["analyses/vision-smoke/crops/not-observed.png"]
    value["opened_image_count"] = 6
    _write_json(path, value)
    with pytest.raises(VisionSmokeValidationError, match="differ from observations"):
        validate_vision_smoke(vision_job)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"time_range": [300, 0]}, "time_range"),
        ({"crops_opened": ["../outside.png"]}, "job-relative"),
    ],
)
def test_provenance_rejects_invalid_range_or_path(
    vision_job: Path, changes: dict[str, object], message: str
) -> None:
    path = vision_job / "analyses/vision-smoke/analysis-provenance.json"
    value = _load(path)
    assert isinstance(value, dict)
    value.update(changes)
    with pytest.raises(ValidationError, match=message):
        VisionSmokeProvenance.model_validate(value)


def test_malformed_observation_jsonl_fails_cleanly(vision_job: Path) -> None:
    path = vision_job / "analyses/vision-smoke/visual-observations.jsonl"
    path.write_text("{not-json}\n")
    with pytest.raises(VisionSmokeValidationError, match="cannot read JSONL"):
        validate_vision_smoke(vision_job)


def test_external_model_api_invalidates_m3a(vision_job: Path) -> None:
    path = vision_job / "analyses/vision-smoke/analysis-provenance.json"
    value = _load(path)
    assert isinstance(value, dict)
    value["external_model_api_used"] = True
    _write_json(path, value)
    with pytest.raises(VisionSmokeValidationError, match="external model API"):
        validate_vision_smoke(vision_job)


def test_missing_negative_control_fails(vision_job: Path) -> None:
    def remove_black(rows: list[dict[str, object]]) -> None:
        rows[:] = [row for row in rows if row["image_kind"] != "black_control"]

    _rewrite_observations(vision_job, remove_black)
    path = vision_job / "analyses/vision-smoke/analysis-provenance.json"
    value = _load(path)
    assert isinstance(value, dict)
    value["control_frames_opened"] = value["control_frames_opened"][1:]
    value["opened_image_count"] = 4
    _write_json(path, value)
    with pytest.raises(VisionSmokeValidationError, match="negative controls"):
        validate_vision_smoke(vision_job)
