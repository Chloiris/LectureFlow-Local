from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable
from typing import Any

from lectureflow.errors import SubtitleParseError
from lectureflow.schemas.transcript import (
    TranscriptDocument,
    TranscriptSegment,
    TranscriptSource,
    UntimedTranscript,
)
from lectureflow.subtitles.timecode import (
    format_clock,
    format_srt_time,
    format_vtt_time,
    parse_timecode,
)

_TIMING = re.compile(
    r"^(?P<start>(?:\d{1,3}:)?\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
    r"(?P<end>(?:\d{1,3}:)?\d{2}:\d{2}[,.]\d{3})(?:\s+.*)?$"
)
_PUNCTUATION = str.maketrans(
    {
        "﹐": "，",
        "﹑": "、",
        "﹒": "。",
        "︰": "：",
        "﹔": "；",
        "﹖": "？",
        "﹗": "！",
    }
)


def clean_text(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    normalized = normalized.translate(_PUNCTUATION)
    normalized = normalized.replace("\ufeff", "").replace("\u200b", "")
    normalized = re.sub(r"[\t\r\n\u00a0]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    normalized = re.sub(r"\s+([，。！？；：、,.!?;:])", r"\1", normalized)
    normalized = re.sub(r"([，。！？；：、])\s+", r"\1", normalized)
    return normalized


def _segment(
    index: int,
    start: float,
    end: float,
    text: str,
    *,
    source: TranscriptSource,
    language: str,
    part_id: str,
    source_ref: dict[str, Any] | None = None,
) -> TranscriptSegment:
    return TranscriptSegment(
        segment_id=f"T{index:06d}",
        start=start,
        end=end,
        text_raw=text,
        text_clean=clean_text(text),
        source=source,
        language=language,
        part_id=part_id,
        source_ref=source_ref or {},
    )


def parse_bilibili_json(
    payload: bytes | str | dict[str, Any],
    *,
    source: TranscriptSource,
    language: str,
    part_id: str,
    source_ref: dict[str, Any] | None = None,
) -> TranscriptDocument:
    try:
        if isinstance(payload, bytes):
            data = json.loads(payload.decode("utf-8-sig"))
        elif isinstance(payload, str):
            data = json.loads(payload.lstrip("\ufeff"))
        else:
            data = payload
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SubtitleParseError(f"Invalid Bilibili subtitle JSON: {exc}") from exc
    body = data.get("body") if isinstance(data, dict) else None
    if not isinstance(body, list):
        raise SubtitleParseError("Bilibili subtitle JSON must contain a body array.")
    segments: list[TranscriptSegment] = []
    for index, item in enumerate(body, start=1):
        if not isinstance(item, dict):
            raise SubtitleParseError(f"Bilibili subtitle body item {index} must be an object.")
        try:
            start = float(item["from"])
            end = float(item["to"])
            text_value = item["content"]
            if not isinstance(text_value, str):
                raise TypeError("content must be a string")
            text = text_value
        except (KeyError, TypeError, ValueError) as exc:
            raise SubtitleParseError(f"Invalid Bilibili subtitle item {index}: {exc}") from exc
        try:
            segments.append(
                _segment(
                    index,
                    start,
                    end,
                    text,
                    source=source,
                    language=language,
                    part_id=part_id,
                    source_ref={**(source_ref or {}), "body_index": index - 1},
                )
            )
        except ValueError as exc:
            raise SubtitleParseError(f"Invalid Bilibili subtitle item {index}: {exc}") from exc
    return TranscriptDocument(
        timed=True,
        source=source,
        language=language,
        part_id=part_id,
        source_ref=source_ref or {},
        segments=segments,
    )


def _parse_timed_blocks(
    value: str,
    *,
    source: TranscriptSource,
    language: str,
    part_id: str,
    is_vtt: bool,
    source_ref: dict[str, Any] | None = None,
) -> TranscriptDocument:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    if is_vtt:
        lines = normalized.splitlines()
        if not lines or not lines[0].strip().startswith("WEBVTT"):
            raise SubtitleParseError("VTT file must begin with WEBVTT.")
        normalized = "\n".join(lines[1:])
    blocks = re.split(r"\n\s*\n", normalized.strip()) if normalized.strip() else []
    parsed: list[tuple[float, float, str]] = []
    for block_number, block in enumerate(blocks, start=1):
        lines = block.splitlines()
        if not lines:
            continue
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            if is_vtt and (
                lines[0].lstrip().startswith(("NOTE", "STYLE", "REGION", "X-TIMESTAMP-MAP"))
                or all(":" in line for line in lines if line.strip())
            ):
                continue
            raise SubtitleParseError(f"Subtitle block {block_number} has no timing line.")
        timing = _TIMING.fullmatch(lines[timing_index].strip())
        if timing is None:
            raise SubtitleParseError(f"Invalid timing in subtitle block {block_number}.")
        start = parse_timecode(timing.group("start").replace(",", "."))
        end = parse_timecode(timing.group("end").replace(",", "."))
        text = "\n".join(lines[timing_index + 1 :]).strip("\n")
        parsed.append((start, end, text))
    segments: list[TranscriptSegment] = []
    for index, (start, end, text) in enumerate(parsed, start=1):
        try:
            segments.append(
                _segment(
                    index,
                    start,
                    end,
                    text,
                    source=source,
                    language=language,
                    part_id=part_id,
                    source_ref={**(source_ref or {}), "cue_index": index - 1},
                )
            )
        except ValueError as exc:
            raise SubtitleParseError(f"Invalid subtitle cue {index}: {exc}") from exc
    return TranscriptDocument(
        timed=True,
        source=source,
        language=language,
        part_id=part_id,
        source_ref=source_ref or {},
        segments=segments,
    )


def parse_srt(
    value: str,
    *,
    source: TranscriptSource = "local_file",
    language: str = "und",
    part_id: str = "local",
    source_ref: dict[str, Any] | None = None,
) -> TranscriptDocument:
    return _parse_timed_blocks(
        value,
        source=source,
        language=language,
        part_id=part_id,
        is_vtt=False,
        source_ref=source_ref,
    )


def parse_vtt(
    value: str,
    *,
    source: TranscriptSource = "local_file",
    language: str = "und",
    part_id: str = "local",
    source_ref: dict[str, Any] | None = None,
) -> TranscriptDocument:
    return _parse_timed_blocks(
        value,
        source=source,
        language=language,
        part_id=part_id,
        is_vtt=True,
        source_ref=source_ref,
    )


def parse_txt(
    value: str,
    *,
    language: str = "und",
    part_id: str = "local",
    source_ref: dict[str, Any] | None = None,
) -> TranscriptDocument:
    raw = value.lstrip("\ufeff")
    return TranscriptDocument(
        timed=False,
        source="local_file",
        language=language,
        part_id=part_id,
        source_ref=source_ref or {},
        untimed=UntimedTranscript(
            text_raw=raw,
            text_clean=clean_text(raw),
            source="local_file",
            language=language,
            part_id=part_id,
            source_ref=source_ref or {},
        ),
    )


def clean_segments(segments: Iterable[TranscriptSegment]) -> list[TranscriptSegment]:
    cleaned: list[TranscriptSegment] = []
    for segment in segments:
        updated = segment.model_copy(update={"text_clean": clean_text(segment.text_raw)})
        previous = cleaned[-1] if cleaned else None
        adjacent_duplicate = (
            previous is not None
            and previous.text_clean == updated.text_clean
            and updated.start <= previous.end + 0.5
        )
        if adjacent_duplicate:
            continue
        cleaned.append(updated)
    return cleaned


def assign_stable_segment_ids(
    document: TranscriptDocument,
    *,
    first_index: int = 1,
) -> tuple[TranscriptDocument, int]:
    """Assign task-global IDs while preserving part-local timelines and source order."""
    if not document.timed:
        return document, first_index
    segments = [
        segment.model_copy(update={"segment_id": f"T{index:06d}"})
        for index, segment in enumerate(document.segments, start=first_index)
    ]
    return document.model_copy(update={"segments": segments}), first_index + len(segments)


def render_txt(document: TranscriptDocument, *, cleaned: bool = True) -> str:
    if not document.timed:
        assert document.untimed is not None
        text = document.untimed.text_clean if cleaned else document.untimed.text_raw
        return f"[UNTIMED] {text}\n"
    lines = []
    for segment in document.segments:
        text = segment.text_clean if cleaned else segment.text_raw
        lines.append(f"[{format_clock(segment.start)} - {format_clock(segment.end)}] {text}")
    return "\n".join(lines) + ("\n" if lines else "")


def render_srt(document: TranscriptDocument, *, cleaned: bool = True) -> str:
    if not document.timed:
        return ""
    blocks = []
    for index, segment in enumerate(document.segments, start=1):
        text = segment.text_clean if cleaned else segment.text_raw
        blocks.append(
            f"{index}\n{format_srt_time(segment.start)} --> {format_srt_time(segment.end)}\n{text}"
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def render_vtt(document: TranscriptDocument, *, cleaned: bool = True) -> str:
    blocks = ["WEBVTT"]
    if document.timed:
        for segment in document.segments:
            text = segment.text_clean if cleaned else segment.text_raw
            blocks.append(
                f"{segment.segment_id}\n{format_vtt_time(segment.start)} --> "
                f"{format_vtt_time(segment.end)}\n{text}"
            )
    return "\n\n".join(blocks) + "\n"
