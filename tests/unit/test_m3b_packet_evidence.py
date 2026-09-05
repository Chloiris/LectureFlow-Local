from __future__ import annotations

import json
from pathlib import Path

import pytest

from lectureflow.errors import StateError
from lectureflow.evidence.service import validate_evidence
from lectureflow.hashing import hash_file, hash_object
from lectureflow.m3b_state import record_validated_artifact_stage
from lectureflow.packets.service import validate_analysis, validate_packet
from lectureflow.state import new_state


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def _write_packet(tmp_path: Path) -> Path:
    root = tmp_path / "中文 任务"
    packet = root / "packets/P0001"
    packet.mkdir(parents=True)
    transcript = {
        "schema_version": "1.0.0",
        "segment_id": "T000001",
        "start": 0,
        "end": 2,
        "text_raw": "文本",
        "text_clean": "文本",
        "source": "mlx_whisper",
        "language": "zh",
        "confidence": None,
        "speaker": None,
        "chapter_id": None,
        "part_id": "P1",
        "source_ref": {},
    }
    (packet / "transcript.jsonl").write_text(json.dumps(transcript) + "\n")
    _write_json(
        packet / "frame-manifest.json",
        {"frames": [{"frame_id": "F000001", "path": "frame.png"}]},
    )
    (packet / "frame.png").write_bytes(b"frame")
    artifacts = {
        "transcript.jsonl": hash_file(packet / "transcript.jsonl"),
        "frame-manifest.json": hash_file(packet / "frame-manifest.json"),
        "frame.png": hash_file(packet / "frame.png"),
    }
    identity = {
        "source_hash": "source",
        "artifacts": artifacts,
        "transcript_ids": ["T000001"],
        "frame_ids": ["F000001"],
    }
    _write_json(
        packet / "packet.json",
        {
            "schema_version": "1.0",
            "packet_id": "P0001",
            "time_range": [0.0, 300.0],
            **identity,
            "packet_hash": hash_object(identity),
        },
    )
    return root


def test_packet_validates_in_chinese_space_path(tmp_path: Path) -> None:
    root = _write_packet(tmp_path)
    assert validate_packet(root, "P0001")["valid"] is True


def test_packet_unknown_transcript_id_fails(tmp_path: Path) -> None:
    root = _write_packet(tmp_path)
    path = root / "packets/P0001/packet.json"
    value = json.loads(path.read_text())
    value["transcript_ids"] = ["T999999"]
    _write_json(path, value)
    with pytest.raises(StateError, match="nonexistent transcript ID"):
        validate_packet(root, "P0001")


def test_packet_unknown_frame_id_fails(tmp_path: Path) -> None:
    root = _write_packet(tmp_path)
    path = root / "packets/P0001/packet.json"
    value = json.loads(path.read_text())
    value["frame_ids"] = ["F999999"]
    _write_json(path, value)
    with pytest.raises(StateError, match="nonexistent frame ID"):
        validate_packet(root, "P0001")


def test_packet_artifact_and_identity_hash_tampering_fail(tmp_path: Path) -> None:
    root = _write_packet(tmp_path)
    packet_root = root / "packets/P0001"
    (packet_root / "frame.png").write_bytes(b"tampered")
    with pytest.raises(StateError, match="artifact hash mismatch"):
        validate_packet(root, "P0001")

    root = _write_packet(tmp_path / "second")
    packet_path = root / "packets/P0001/packet.json"
    packet = json.loads(packet_path.read_text())
    packet["packet_hash"] = "0" * 64
    _write_json(packet_path, packet)
    with pytest.raises(StateError, match="Packet hash mismatch"):
        validate_packet(root, "P0001")


def test_packet_rejects_unknown_packet_and_wrong_range(tmp_path: Path) -> None:
    root = _write_packet(tmp_path)
    with pytest.raises(StateError, match="only packet P0001"):
        validate_packet(root, "P0002")
    packet_path = root / "packets/P0001/packet.json"
    packet = json.loads(packet_path.read_text())
    packet["time_range"] = [1.0, 300.0]
    _write_json(packet_path, packet)
    with pytest.raises(StateError, match="time range"):
        validate_packet(root, "P0001")


def _write_evidence_bundle(root: Path, record: dict[str, object]) -> None:
    evidence = root / "evidence"
    evidence.mkdir()
    (evidence / "evidence-ledger.jsonl").write_text(json.dumps(record) + "\n")
    _write_json(
        evidence / "coverage.json",
        {
            "informational_ranges": [],
            "excluded_ranges": [],
            "covered_ranges": [],
            "uncovered_ranges": [],
            "max_uncovered_gap_seconds": 0,
            "speech_evidence_coverage_ratio": 0,
            "visual_evidence_coverage_ratio": 0,
            "joint_evidence_coverage_ratio": 0,
        },
    )


def _evidence(kind: str = "speech+slide") -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "evidence_id": "E000001",
        "start": 0,
        "end": 2,
        "topic": "topic",
        "claim": "claim",
        "transcript_ids": ["T000001"],
        "frame_ids": ["F000001"],
        "evidence_type": kind,
        "confidence": "high",
        "uncertainty": None,
    }


def test_evidence_without_any_source_fails(tmp_path: Path) -> None:
    root = _write_packet(tmp_path)
    record = _evidence()
    record["transcript_ids"] = []
    record["frame_ids"] = []
    _write_evidence_bundle(root, record)
    with pytest.raises(StateError, match="evidence must cite"):
        validate_evidence(root)


@pytest.mark.parametrize(
    ("missing", "message"), [("transcript_ids", "both"), ("frame_ids", "both")]
)
def test_speech_slide_missing_one_source_fails(tmp_path: Path, missing: str, message: str) -> None:
    root = _write_packet(tmp_path)
    record = _evidence()
    record[missing] = []
    _write_evidence_bundle(root, record)
    with pytest.raises(StateError, match=message):
        validate_evidence(root)


def _write_analysis(root: Path, *, external_api: bool = False) -> None:
    _write_json(
        root / "analyses/P0001-analysis.json",
        {
            "schema_version": "1.0",
            "packet_id": "P0001",
            "time_range": [0, 300],
            "images_opened": ["frames/originals/F000001.png"],
            "image_tool": "view_image",
            "sections": [
                {
                    "section_id": "S0001",
                    "title": "section",
                    "start": 0,
                    "end": 2,
                    "summary": "summary",
                    "transcript_evidence": ["T000001"],
                    "visual_evidence": ["F000001"],
                    "confidence": "high",
                    "uncertainties": [],
                }
            ],
            "concepts": [],
            "definitions": [],
            "course_information": [],
            "formulas": [],
            "code_blocks": [],
            "visual_findings": [],
            "asr_correction_suggestions": [],
            "examples": [],
            "pitfalls": [],
            "uncertainties": [],
            "coverage": {},
            "external_model_api_used": external_api,
            "ocr_used": False,
        },
    )


def test_analysis_claiming_opened_image_without_provenance_fails(tmp_path: Path) -> None:
    root = _write_packet(tmp_path)
    _write_analysis(root)
    with pytest.raises(StateError, match="require M3B provenance"):
        validate_analysis(root)


def test_external_model_api_true_invalidates_analysis(tmp_path: Path) -> None:
    root = _write_packet(tmp_path)
    _write_analysis(root, external_api=True)
    _write_json(
        root / "reports/m3b-provenance.json",
        {
            "images_opened": ["frames/originals/F000001.png"],
            "external_model_api_used": True,
        },
    )
    with pytest.raises(StateError, match="external model API"):
        validate_analysis(root)


def test_valid_analysis_requires_exact_opened_image_provenance(tmp_path: Path) -> None:
    root = _write_packet(tmp_path)
    _write_analysis(root)
    _write_json(
        root / "reports/m3b-provenance.json",
        {
            "images_opened": ["frames/originals/F000001.png"],
            "external_model_api_used": False,
        },
    )
    result = validate_analysis(root)
    assert result["valid"] is True
    assert result["section_count"] == 1

    provenance = root / "reports/m3b-provenance.json"
    _write_json(provenance, {"images_opened": [], "external_model_api_used": False})
    with pytest.raises(StateError, match="differ from provenance"):
        validate_analysis(root)


def test_valid_evidence_and_unknown_references(tmp_path: Path) -> None:
    root = _write_packet(tmp_path)
    _write_evidence_bundle(root, _evidence())
    assert validate_evidence(root)["evidence_count"] == 1

    record = _evidence()
    record["transcript_ids"] = ["T999999"]
    (root / "evidence/evidence-ledger.jsonl").write_text(json.dumps(record) + "\n")
    with pytest.raises(StateError, match="unknown transcript"):
        validate_evidence(root)

    record = _evidence()
    record["frame_ids"] = ["F999999"]
    (root / "evidence/evidence-ledger.jsonl").write_text(json.dumps(record) + "\n")
    with pytest.raises(StateError, match="unknown frame"):
        validate_evidence(root)


def test_evidence_rejects_empty_ledger_and_incomplete_coverage(tmp_path: Path) -> None:
    root = _write_packet(tmp_path)
    evidence = root / "evidence"
    evidence.mkdir()
    (evidence / "evidence-ledger.jsonl").write_text("")
    _write_json(evidence / "coverage.json", {})
    with pytest.raises(StateError, match="ledger is empty"):
        validate_evidence(root)

    (evidence / "evidence-ledger.jsonl").write_text(json.dumps(_evidence()) + "\n")
    with pytest.raises(StateError, match="coverage report is incomplete"):
        validate_evidence(root)


def test_validated_artifact_stage_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "job"
    artifact = root / "analyses/P0001-analysis.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}")
    files = type(
        "Files",
        (),
        {
            "root": root,
            "lock": root / ".lock",
            "state": root / "pipeline-state.json",
            "events": root / "reports/events.jsonl",
        },
    )()
    state = new_state("job")
    monkeypatch.setattr(
        "lectureflow.m3b_state.load_job_context",
        lambda *args, **kwargs: (files, None, state),
    )
    config = object()
    record_validated_artifact_stage(
        "job",
        config=config,  # type: ignore[arg-type]
        stage="analysis_ready",
        artifact=Path("analyses/P0001-analysis.json"),
        tool="current-codex-vision",
    )
    revision = state.revision
    record_validated_artifact_stage(
        "job",
        config=config,  # type: ignore[arg-type]
        stage="analysis_ready",
        artifact=Path("analyses/P0001-analysis.json"),
        tool="current-codex-vision",
    )
    assert state.stages["analysis_ready"].status.value == "succeeded"
    assert state.revision == revision
    assert "stage_cache_hit" in files.events.read_text()


def test_validated_artifact_stage_does_not_regress_milestone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "job"
    (root / "web").mkdir(parents=True)
    (root / "obsidian").mkdir()
    (root / "web/manifest.json").write_text("web")
    (root / "obsidian/manifest.json").write_text("obsidian")
    files = type(
        "Files",
        (),
        {
            "root": root,
            "lock": root / ".lock",
            "state": root / "pipeline-state.json",
            "events": root / "reports/events.jsonl",
        },
    )()
    state = new_state("job")
    monkeypatch.setattr(
        "lectureflow.m3b_state.load_job_context",
        lambda *args, **kwargs: (files, None, state),
    )
    config = object()
    record_validated_artifact_stage(
        "job",
        config=config,  # type: ignore[arg-type]
        stage="web_ready",
        artifact=Path("web/manifest.json"),
        tool="web",
    )
    record_validated_artifact_stage(
        "job",
        config=config,  # type: ignore[arg-type]
        stage="obsidian_ready",
        artifact=Path("obsidian/manifest.json"),
        tool="obsidian",
    )
    assert state.last_successful_milestone == "web_ready"
    assert state.current_milestone == "web_ready"
