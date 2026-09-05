from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

import lectureflow.m4a.service as m4a
from lectureflow.errors import StateError
from lectureflow.hashing import hash_file
from lectureflow.schemas.m4a import Correction, Paragraph, Section

from ..helpers_m4a import m4a_fixture, write_jsonl


def _patch_context(
    monkeypatch: pytest.MonkeyPatch, files: object, manifest: object, state: object
) -> None:
    monkeypatch.setattr(m4a, "load_job_context", lambda *args, **kwargs: (files, manifest, state))


def test_paragraph_build_covers_all_17_segments_once_and_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, files, manifest, state = m4a_fixture(tmp_path)
    _patch_context(monkeypatch, files, manifest, state)
    original_hash = hash_file(files.root / "transcript/asr/original.jsonl")
    first = m4a.build_paragraphs("m4a-fixture", config=config)
    second = m4a.build_paragraphs("m4a-fixture", config=config)
    validated = m4a.validate_paragraphs(files.root)
    paragraphs = m4a._load_jsonl(files.root / "transcript/paragraphs/paragraphs.jsonl")
    assignments = [item for paragraph in paragraphs for item in paragraph["source_segment_ids"]]
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert validated == {
        "schema_version": "1.0",
        "valid": True,
        "paragraph_count": 6,
        "segment_count": 17,
        "missing_segments": [],
        "duplicate_segments": [],
    }
    assert assignments == [f"T{index:06d}" for index in range(1, 18)]
    assert len(assignments) == len(set(assignments))
    assert hash_file(files.root / "transcript/asr/original.jsonl") == original_hash


def test_paragraph_boundaries_equal_first_and_last_source_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, files, manifest, state = m4a_fixture(tmp_path)
    _patch_context(monkeypatch, files, manifest, state)
    m4a.build_paragraphs("m4a-fixture", config=config)
    segments = {item.segment_id: item for item in m4a._read_segments(files.root)}
    paragraphs = [
        Paragraph.model_validate(item)
        for item in m4a._load_jsonl(files.root / "transcript/paragraphs/paragraphs.jsonl")
    ]
    for paragraph in paragraphs:
        assert paragraph.start == segments[paragraph.source_segment_ids[0]].start
        assert paragraph.end == segments[paragraph.source_segment_ids[-1]].end
        expected = "".join(segments[item].text_raw for item in paragraph.source_segment_ids)
        assert paragraph.text_raw == expected


def test_non_contiguous_segment_merge_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, files, manifest, state = m4a_fixture(tmp_path)
    _patch_context(monkeypatch, files, manifest, state)
    m4a.build_paragraphs("m4a-fixture", config=config)
    path = files.root / "transcript/paragraphs/paragraphs.jsonl"
    values = m4a._load_jsonl(path)
    values[0]["source_segment_ids"] = ["T000001", "T000003"]
    write_jsonl(path, values)
    with pytest.raises(StateError, match="non-contiguous"):
        m4a.validate_paragraphs(files.root)


def test_paragraph_time_mismatch_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, files, manifest, state = m4a_fixture(tmp_path)
    _patch_context(monkeypatch, files, manifest, state)
    m4a.build_paragraphs("m4a-fixture", config=config)
    path = files.root / "transcript/paragraphs/paragraphs.jsonl"
    values = m4a._load_jsonl(path)
    values[0]["start"] = 1.0
    write_jsonl(path, values)
    with pytest.raises(StateError, match="time does not match"):
        m4a.validate_paragraphs(files.root)


@pytest.mark.parametrize(
    "changes",
    [
        {"end": 100.0},
        {"text_raw": ""},
        {"source_segment_ids": []},
        {"start": 10.0, "end": 10.0},
    ],
)
def test_paragraph_schema_rejects_overlong_empty_sourceless_and_invalid_time(
    changes: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "paragraph_id": "PAR0001",
        "start": 0.0,
        "end": 10.0,
        "title": "title",
        "text_raw": "raw",
        "text_clean": "clean",
        "text_corrected": "corrected",
        "source_segment_ids": ["T000001"],
        "correction_ids": [],
        "evidence_ids": [],
        "frame_ids": [],
        "confidence": "high",
        "uncertainties": [],
    }
    values.update(changes)
    with pytest.raises(ValidationError):
        Paragraph.model_validate(values)


def test_corrections_apply_only_accepted_and_keep_pending_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, files, manifest, state = m4a_fixture(tmp_path)
    _patch_context(monkeypatch, files, manifest, state)
    raw_hash = hash_file(files.root / "transcript/asr/original.jsonl")
    result = m4a.build_corrections("m4a-fixture", config=config)
    paragraphs = [
        Paragraph.model_validate(item)
        for item in m4a._load_jsonl(files.root / "transcript/paragraphs/paragraphs.jsonl")
    ]
    corrected = {item.paragraph_id: item.text_corrected for item in paragraphs}
    assert result["accepted"] == 1 and result["pending"] == 1 and result["rejected"] == 0
    assert "大语言模型" in corrected["PAR0003"]
    assert "本课程" not in corrected["PAR0001"]
    assert hash_file(files.root / "transcript/asr/original.jsonl") == raw_hash
    report = (files.root / "transcript/corrections/correction-report.md").read_text()
    assert "- 大元模型" in report and "+ 大语言模型" in report


def test_unproven_accepted_correction_fails_schema() -> None:
    with pytest.raises(ValidationError, match="high-confidence"):
        Correction(
            correction_id="C000001",
            paragraph_id="PAR0001",
            source_segment_ids=["T000001"],
            start=0,
            end=10,
            original_text="原词",
            suggested_text="新词",
            change_type="term",
            reason="no proof",
            transcript_evidence=["T000001"],
            visual_evidence=[],
            confidence="high",
            decision="accepted",
            applied=True,
        )


def test_correction_unknown_frame_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, files, manifest, state = m4a_fixture(tmp_path)
    _patch_context(monkeypatch, files, manifest, state)
    path = files.root / "analyses/m4a/correction-plan.json"
    plan = json.loads(path.read_text())
    plan["corrections"][0]["visual_evidence"] = ["F999999"]
    path.write_text(json.dumps(plan))
    with pytest.raises(StateError, match="unknown frame"):
        m4a.build_corrections("m4a-fixture", config=config)


def test_glossary_traces_term_to_correction_and_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, files, manifest, state = m4a_fixture(tmp_path)
    _patch_context(monkeypatch, files, manifest, state)
    m4a.build_corrections("m4a-fixture", config=config)
    glossary = json.loads((files.root / "transcript/corrections/course-glossary.json").read_text())
    term = next(item for item in glossary["terms"] if item["term"] == "大语言模型")
    assert term["source_correction_id"] == "C000001"
    assert term["transcript_evidence"] == ["T000007"]
    assert term["visual_evidence"] == ["F000002"]


def test_render_builds_evidenced_sections_and_valid_mindmap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, files, manifest, state = m4a_fixture(tmp_path)
    _patch_context(monkeypatch, files, manifest, state)
    first = m4a.render_m4a("m4a-fixture", config=config)
    second = m4a.render_m4a("m4a-fixture", config=config)
    validated = m4a.validate_render(files.root)
    assert first["section_count"] == 5 and first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert validated["valid"] is True
    assert (files.root / "analyses/m4a/mindmap.md").is_file()
    assert (files.root / "analyses/m4a/mindmap.mmd").read_text().startswith("mindmap\n")


def test_section_without_evidence_fails() -> None:
    with pytest.raises(ValidationError, match="requires evidence"):
        Section(
            section_id="SEC0001",
            title="title",
            start=0,
            end=10,
            paragraph_ids=["PAR0001"],
            summary="unsupported",
            evidence_ids=[],
            frame_ids=[],
            confidence="low",
            uncertainties=[],
        )


def test_mindmap_unknown_target_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, files, manifest, state = m4a_fixture(tmp_path)
    _patch_context(monkeypatch, files, manifest, state)
    m4a.render_m4a("m4a-fixture", config=config)
    path = files.root / "analyses/m4a/mindmap.json"
    value = json.loads(path.read_text())
    value["children"][0]["target_id"] = "SEC9999"
    path.write_text(json.dumps(value))
    with pytest.raises(StateError, match="unknown target"):
        m4a.validate_render(files.root)
