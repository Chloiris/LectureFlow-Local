from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lectureflow.atomic import atomic_write_json, atomic_write_text
from lectureflow.hashing import hash_file, hash_object
from lectureflow.schemas.transcript import TranscriptDocument
from lectureflow.subtitles.formats import clean_segments, render_srt, render_txt, render_vtt
from lectureflow.subtitles.quality import build_quality_report

REQUIRED_PART_ARTIFACTS = (
    "original.json",
    "original.jsonl",
    "cleaned.jsonl",
    "transcript.txt",
    "transcript.srt",
    "transcript.vtt",
    "subtitle-source.json",
    "subtitle-selection-report.json",
    "subtitle-quality-report.json",
)


def _jsonl(records: list[dict[str, Any]]) -> str:
    return "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for record in records
    )


def export_document(
    document: TranscriptDocument,
    destination: Path,
    *,
    subtitle_source: dict[str, Any],
    selection_report: dict[str, Any],
    media_duration: float | None = None,
    media_start: float = 0.0,
) -> dict[str, Any]:
    destination.mkdir(parents=True, exist_ok=True)
    original_document = document.model_copy(deep=True)
    cleaned_document = document.model_copy(deep=True)
    if document.timed:
        cleaned_document.segments = clean_segments(document.segments)
    elif document.untimed is not None:
        cleaned_document.untimed = document.untimed.model_copy(
            update={"text_clean": document.untimed.text_clean}
        )
    atomic_write_json(
        destination / "original.json",
        original_document.model_dump(mode="json"),
    )
    if document.timed:
        original_records = [segment.model_dump(mode="json") for segment in document.segments]
        cleaned_records = [segment.model_dump(mode="json") for segment in cleaned_document.segments]
    else:
        assert document.untimed is not None
        assert cleaned_document.untimed is not None
        original_records = [document.untimed.model_dump(mode="json")]
        cleaned_records = [cleaned_document.untimed.model_dump(mode="json")]
    atomic_write_text(destination / "original.jsonl", _jsonl(original_records))
    atomic_write_text(destination / "cleaned.jsonl", _jsonl(cleaned_records))
    atomic_write_text(destination / "transcript.txt", render_txt(cleaned_document))
    atomic_write_text(destination / "transcript.srt", render_srt(cleaned_document))
    atomic_write_text(destination / "transcript.vtt", render_vtt(cleaned_document))
    atomic_write_json(destination / "subtitle-source.json", subtitle_source)
    atomic_write_json(destination / "subtitle-selection-report.json", selection_report)
    quality = build_quality_report(
        original_document,
        media_duration=media_duration,
        media_start=media_start,
    )
    atomic_write_json(destination / "subtitle-quality-report.json", quality)
    artifacts = {name: hash_file(destination / name) for name in REQUIRED_PART_ARTIFACTS}
    manifest = {
        "schema_version": "1.0.0",
        "part_id": document.part_id,
        "document_hash": hash_object(original_document.model_dump(mode="json")),
        "artifacts": artifacts,
    }
    atomic_write_json(destination / "artifact-manifest.json", manifest)
    return manifest


def validate_artifact_manifest(destination: Path) -> bool:
    path = destination / "artifact-manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        artifacts = manifest["artifacts"]
        return all(
            (destination / name).is_file() and hash_file(destination / name) == digest
            for name, digest in artifacts.items()
        )
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return False


def load_document(path: Path) -> TranscriptDocument:
    return TranscriptDocument.model_validate_json(path.read_text(encoding="utf-8"))
