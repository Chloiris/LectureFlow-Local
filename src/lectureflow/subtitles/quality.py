from __future__ import annotations

import re
from typing import Any

from lectureflow.schemas.transcript import TranscriptDocument, TranscriptSegment


def _union_duration(
    segments: list[TranscriptSegment],
    *,
    lower: float = 0.0,
    upper: float | None = None,
) -> float:
    intervals = [
        (max(segment.start, lower), min(segment.end, upper) if upper is not None else segment.end)
        for segment in segments
    ]
    intervals = [(start, end) for start, end in intervals if end >= start]
    if not intervals:
        return 0.0
    intervals.sort()
    total = 0.0
    start, end = intervals[0]
    for next_start, next_end in intervals[1:]:
        if next_start <= end:
            end = max(end, next_end)
        else:
            total += end - start
            start, end = next_start, next_end
    return total + end - start


def _suspicious_text(value: str) -> bool:
    if "\ufffd" in value or "锟斤拷" in value or "ï»¿" in value:
        return True
    control_count = sum(ord(char) < 32 and char not in "\n\t\r" for char in value)
    mojibake_runs = len(re.findall(r"(?:Ã.|Â.|â€|æ.|å.|ä.){2,}", value))
    return control_count > 0 or mojibake_runs > 0


def build_quality_report(
    document: TranscriptDocument,
    *,
    media_duration: float | None = None,
    media_start: float = 0.0,
    long_segment_chars: int = 180,
    long_segment_seconds: float = 20.0,
) -> dict[str, Any]:
    if not document.timed:
        assert document.untimed is not None
        text = document.untimed.text_raw
        return {
            "schema_version": "1.0.0",
            "part_id": document.part_id,
            "timed": False,
            "range_start": media_start,
            "total_duration": media_duration,
            "subtitle_coverage_duration": 0.0,
            "coverage_ratio": None,
            "character_count": len(re.sub(r"\s+", "", text)),
            "characters_per_minute": None,
            "empty_segments": [],
            "invalid_time_segments": [],
            "overlaps": [],
            "duplicates": [],
            "very_long_segments": [],
            "suspicious_garbled_segments": ["UNTIMED"] if _suspicious_text(text) else [],
            "warnings": [
                "untimed_transcript",
                "SRT/VTT contain no fabricated cues; provide an explicit timing strategy later.",
            ],
        }

    segments = document.segments
    timeline_end = max((segment.end for segment in segments), default=0.0)
    total_duration = (
        media_duration if media_duration is not None else max(0.0, timeline_end - media_start)
    )
    coverage = _union_duration(
        segments,
        lower=media_start,
        upper=media_start + total_duration if total_duration is not None else None,
    )
    ratio = coverage / total_duration if total_duration and total_duration > 0 else None
    characters = sum(len(re.sub(r"\s+", "", segment.text_raw)) for segment in segments)
    per_minute = (
        characters / (total_duration / 60) if total_duration and total_duration > 0 else None
    )
    empty: list[str] = []
    invalid: list[str] = []
    overlaps: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    very_long: list[str] = []
    suspicious: list[str] = []
    previous: TranscriptSegment | None = None
    for segment in segments:
        if not segment.text_raw.strip():
            empty.append(segment.segment_id)
        if segment.start < 0 or segment.end < segment.start:
            invalid.append(segment.segment_id)
        if previous is not None:
            if segment.start < previous.end:
                overlaps.append(
                    {
                        "first": previous.segment_id,
                        "second": segment.segment_id,
                        "seconds": round(previous.end - segment.start, 3),
                    }
                )
            if segment.text_clean and segment.text_clean == previous.text_clean:
                duplicates.append({"first": previous.segment_id, "second": segment.segment_id})
        if (
            len(segment.text_raw) > long_segment_chars
            or segment.end - segment.start > long_segment_seconds
        ):
            very_long.append(segment.segment_id)
        if _suspicious_text(segment.text_raw):
            suspicious.append(segment.segment_id)
        previous = segment
    warnings: list[str] = []
    if empty:
        warnings.append("empty_segments")
    if invalid:
        warnings.append("invalid_times")
    if overlaps:
        warnings.append("overlapping_segments")
    if duplicates:
        warnings.append("duplicate_segments")
    if very_long:
        warnings.append("very_long_segments")
    if suspicious:
        warnings.append("suspicious_garbled_text")
    if total_duration >= 1800 and ((ratio is not None and ratio < 0.2) or (per_minute or 0) < 20):
        warnings.append("long_video_subtitles_abnormally_sparse")
    return {
        "schema_version": "1.0.0",
        "part_id": document.part_id,
        "timed": True,
        "range_start": media_start,
        "total_duration": round(total_duration, 3),
        "subtitle_coverage_duration": round(coverage, 3),
        "coverage_ratio": round(ratio, 6) if ratio is not None else None,
        "character_count": characters,
        "characters_per_minute": round(per_minute, 3) if per_minute is not None else None,
        "empty_segments": empty,
        "invalid_time_segments": invalid,
        "overlaps": overlaps,
        "duplicates": duplicates,
        "very_long_segments": very_long,
        "suspicious_garbled_segments": suspicious,
        "warnings": warnings,
    }
