from __future__ import annotations

import json
from pathlib import Path

import pytest

from lectureflow.errors import SubtitleParseError
from lectureflow.schemas.transcript import TranscriptSegment
from lectureflow.subtitles.export import REQUIRED_PART_ARTIFACTS, export_document, load_document
from lectureflow.subtitles.formats import (
    parse_bilibili_json,
    parse_srt,
    parse_txt,
    parse_vtt,
    render_srt,
    render_vtt,
)
from lectureflow.subtitles.quality import build_quality_report

FIXTURES = Path(__file__).parents[1] / "fixtures/subtitles"
BILI_FIXTURES = Path(__file__).parents[1] / "fixtures/bilibili"


def _source_report() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "source": "local_file",
        "language": "zh-CN",
        "generated_at": "2026-01-01T00:00:00Z",
        "model": None,
        "raw_file": "raw/subtitles/original.srt",
    }


@pytest.mark.parametrize(
    ("name", "parser", "renderer"),
    [("sample.srt", parse_srt, render_srt), ("sample.vtt", parse_vtt, render_vtt)],
)
def test_timed_subtitle_round_trip_is_stable(
    name: str,
    parser: object,
    renderer: object,
) -> None:
    value = (FIXTURES / name).read_text(encoding="utf-8")
    document = parser(value, language="zh-CN")  # type: ignore[operator]
    first = renderer(document)  # type: ignore[operator]
    reparsed = parser(first, language="zh-CN")  # type: ignore[operator]
    assert renderer(reparsed) == first  # type: ignore[operator]
    assert document.segments[0].text_raw.startswith("  原始")
    assert (
        document.segments[0].text_clean == "原始 字幕，保留空白"
        or document.segments[0].text_clean == "原始 VTT，保留空白"
    )
    assert [(item.start, item.end) for item in document.segments] == [
        (0.25, 2.75),
        (3.5, 6.0),
    ]


def test_bilibili_json_exports_all_formats_without_overwriting_raw(tmp_path: Path) -> None:
    raw = (BILI_FIXTURES / "subtitle_uploader.json").read_bytes()
    document = parse_bilibili_json(
        raw,
        source="uploader",
        language="zh-CN",
        part_id="BV1xx411c7mD-p1",
    )
    destination = tmp_path / "导出 空格"
    export_document(
        document,
        destination,
        subtitle_source=_source_report(),
        selection_report={"schema_version": "1.0.0", "selected_source": "uploader"},
        media_duration=10,
    )
    assert all((destination / name).is_file() for name in REQUIRED_PART_ARTIFACTS)
    loaded = load_document(destination / "original.json")
    assert loaded.segments[0].text_raw == "  第一段  原始字幕 "
    original_record = json.loads(
        (destination / "original.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    cleaned_record = json.loads(
        (destination / "cleaned.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert original_record["text_raw"] == "  第一段  原始字幕 "
    assert cleaned_record["text_raw"] == original_record["text_raw"]
    assert cleaned_record["text_clean"] == "第一段 原始字幕"
    assert "00:00:00,250 --> 00:00:02,750" in (destination / "transcript.srt").read_text()
    assert "00:00:00.250 --> 00:00:02.750" in (destination / "transcript.vtt").read_text()
    assert "[00:00:00.250 - 00:00:02.750]" in (destination / "transcript.txt").read_text()


def test_txt_remains_explicitly_untimed(tmp_path: Path) -> None:
    raw = (FIXTURES / "sample.txt").read_text(encoding="utf-8")
    document = parse_txt(raw, language="zh-CN")
    destination = tmp_path / "untimed"
    export_document(
        document,
        destination,
        subtitle_source=_source_report(),
        selection_report={"schema_version": "1.0.0"},
    )
    assert document.timed is False
    assert (destination / "transcript.srt").read_text() == ""
    assert (destination / "transcript.vtt").read_text() == "WEBVTT\n"
    assert (destination / "transcript.txt").read_text().startswith("[UNTIMED]")
    quality = json.loads((destination / "subtitle-quality-report.json").read_text())
    assert quality["timed"] is False
    assert "untimed_transcript" in quality["warnings"]


def test_invalid_time_is_rejected_and_quality_finds_overlap_duplicate() -> None:
    with pytest.raises(SubtitleParseError, match="end must be greater"):
        parse_srt("1\n00:00:05,000 --> 00:00:04,000\nbad\n")

    document = parse_srt(
        "1\n00:00:00,000 --> 00:00:03,000\n重复\n\n2\n00:00:02,500 --> 00:00:05,000\n重复\n",
        language="zh-CN",
    )
    report = build_quality_report(document, media_duration=10)
    assert report["subtitle_coverage_duration"] == 5.0
    assert report["coverage_ratio"] == 0.5
    assert report["overlaps"] == [{"first": "T000001", "second": "T000002", "seconds": 0.5}]
    assert report["duplicates"] == [{"first": "T000001", "second": "T000002"}]


def test_schema_rejects_non_finite_and_negative_timeline() -> None:
    values = {
        "segment_id": "T000001",
        "start": 0,
        "end": 1,
        "text_raw": "x",
        "text_clean": "x",
        "source": "local_file",
        "language": "zh-CN",
        "part_id": "local",
    }
    with pytest.raises(ValueError):
        TranscriptSegment.model_validate({**values, "start": -1})
    with pytest.raises(ValueError):
        TranscriptSegment.model_validate({**values, "end": float("inf")})
    with pytest.raises(ValueError):
        TranscriptSegment.model_validate({**values, "segment_id": "bad-id"})
    with pytest.raises(ValueError, match="unsupported transcript schema"):
        TranscriptSegment.model_validate({**values, "schema_version": "2.0.0"})


def test_vtt_header_metadata_is_supported() -> None:
    document = parse_vtt(
        "WEBVTT\nKind: captions\nLanguage: zh-CN\n\n00:00.000 --> 00:01.000\n内容\n"
    )
    assert len(document.segments) == 1
    assert document.segments[0].text_raw == "内容"
