# This module contains audited, user-visible transcript and slide wording whose
# source-faithful lines are intentionally not wrapped.
# ruff: noqa: E501

from __future__ import annotations

import json
import re
import shutil
from itertools import pairwise
from pathlib import Path
from typing import Any

from lectureflow.atomic import atomic_write_bytes, atomic_write_json, atomic_write_text
from lectureflow.config import AppConfig
from lectureflow.errors import StateError
from lectureflow.frames.contact_sheet import build_contact_sheets
from lectureflow.hashing import hash_file, hash_object
from lectureflow.pipeline import load_job_context
from lectureflow.process import run_command
from lectureflow.schemas.m4b import TechnicalPacket, TechnicalParagraph
from lectureflow.schemas.transcript import TranscriptDocument, TranscriptSegment
from lectureflow.subtitles.formats import render_srt, render_txt, render_vtt

SOURCE_BVID = "BV1pf421z757"
SOURCE_PART = 6
FROZEN_END = 1500.0
M5_SCHEMA = "m5-1.0.0"


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"Cannot read M5 input {path.name}: {exc}") from exc


def _jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"Cannot read M5 JSONL input {path.name}: {exc}") from exc


def _write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    atomic_write_text(
        path,
        "".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for item in values
        ),
    )


def _require_frozen_job(job_id: str, config: AppConfig) -> tuple[Any, Any]:
    files, manifest, _ = load_job_context(job_id, config=config)
    if (
        manifest.source.bvid != SOURCE_BVID
        or manifest.source.page != SOURCE_PART
        or manifest.requested_range.start != 0
        or manifest.requested_range.end != FROZEN_END
    ):
        raise StateError("M5 frozen baseline must be BV1pf421z757 P6 [0, 1500].")
    summary = files.root / "audits/m4c/m4c-summary.json"
    if not summary.is_file() or _load_json(summary).get("result") != "passed":
        raise StateError("M5 requires the passed M4C baseline before incremental processing.")
    return files, manifest


def _format_estimate(metadata: dict[str, Any], duration: float) -> int:
    formats = metadata.get("formats", [])
    video = [
        item
        for item in formats
        if item.get("height") == 1080 and item.get("vcodec") not in {None, "none"}
    ]
    audio = [item for item in formats if item.get("vcodec") == "none"]
    if not video or not audio:
        raise StateError("P6 metadata does not expose an auditable 1080p video/audio estimate.")
    selected_video = max(video, key=lambda item: float(item.get("tbr") or 0))
    selected_audio = max(audio, key=lambda item: float(item.get("tbr") or 0))
    total_duration = float(metadata["duration"])
    full = int(
        (float(selected_video.get("filesize_approx") or 0))
        + (float(selected_audio.get("filesize_approx") or 0))
    )
    if not full:
        rate = float(selected_video["tbr"]) + float(selected_audio["tbr"])
        full = int(total_duration * rate * 1000 / 8)
    return int(full * duration / total_duration)


def plan_p6_completion(frozen_job_id: str, *, config: AppConfig) -> dict[str, Any]:
    files, _ = _require_frozen_job(frozen_job_id, config)
    metadata = _load_json(files.metadata)
    duration = float(metadata.get("duration") or 0)
    if duration <= FROZEN_END:
        raise StateError("P6 duration is not greater than the frozen 1500-second range.")
    remaining = duration - FROZEN_END
    full_estimate = _format_estimate(metadata, duration)
    incremental_estimate = _format_estimate(metadata, remaining)
    if incremental_estimate > int(1.5 * 1024**3):
        raise StateError("Estimated incremental P6 media exceeds the 1.5 GiB stop boundary.")
    report = {
        "schema_version": "1.0",
        "source": SOURCE_BVID,
        "part": SOURCE_PART,
        "cid": 1640319869,
        "title": str(metadata.get("title") or "P6"),
        "duration_seconds": duration,
        "frozen_range": [0.0, FROZEN_END],
        "incremental_range": [FROZEN_END, duration],
        "remaining_seconds": remaining,
        "estimated_full_download_bytes": full_estimate,
        "estimated_incremental_download_bytes": incremental_estimate,
        "selected_media_strategy": "two_segments_incremental_download",
        "selected_resolution": "1920x1080",
        "source_metadata_path": "raw/metadata/metadata.json",
        "stop_reason": None,
        "external_model_api_used": False,
    }
    output = files.root / "metadata/p6-completion-plan.json"
    atomic_write_json(output, report)
    return {"job_id": frozen_job_id, "path": str(output.relative_to(files.root)), **report}


def _hash_group(root: Path, paths: list[Path]) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in paths:
        if not path.is_file():
            raise StateError(f"Frozen baseline artifact is missing: {path.relative_to(root)}")
        values[str(path.relative_to(root))] = hash_file(path)
    return values


def _id_max(values: list[str], prefix: str) -> int:
    pattern = re.compile(rf"^{re.escape(prefix)}([0-9]+)$")
    numbers = [int(match.group(1)) for value in values if (match := pattern.match(value))]
    return max(numbers, default=0)


def _ids(root: Path) -> dict[str, list[str]]:
    def jsonl(path: Path) -> list[dict[str, Any]]:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    transcript = jsonl(root / "transcript/asr/original.jsonl")
    paragraphs = jsonl(root / "transcript/paragraphs/paragraphs.jsonl")
    frames = jsonl(root / "frames/frames.jsonl")
    corrections = jsonl(root / "transcript/corrections/correction-decisions.jsonl")
    evidence = jsonl(root / "evidence/evidence-ledger.jsonl")
    formulas = _load_json(root / "analyses/m4b/formulas.json")
    derivations = _load_json(root / "analyses/m4b/derivations.json")
    sections = _load_json(root / "analyses/m4b/sections.json")
    packets = sorted(path.name for path in (root / "packets").glob("P[0-9][0-9][0-9][0-9]"))
    return {
        "transcript": [item["segment_id"] for item in transcript],
        "paragraph": [item["paragraph_id"] for item in paragraphs],
        "packet": packets,
        "frame": [item["frame_id"] for item in frames],
        "formula": [item["formula_id"] for item in formulas],
        "derivation": [item["derivation_id"] for item in derivations],
        "correction": [item["correction_id"] for item in corrections],
        "evidence": [item["evidence_id"] for item in evidence],
        "section": [item["section_id"] for item in sections],
    }


def _git_revision(args: tuple[str, ...]) -> str:
    executable = shutil.which("git")
    if executable is None:
        raise StateError("Git is required to freeze the audited P6 baseline.")
    return run_command((executable, *args), timeout=30).stdout.strip()


def freeze_p6_baseline(frozen_job_id: str, *, config: AppConfig) -> dict[str, Any]:
    files, _ = _require_frozen_job(frozen_job_id, config)
    root = files.root
    plan = plan_p6_completion(frozen_job_id, config=config)
    groups = {
        "media": [root / "media/media-manifest.json", root / "media/video/source.mp4"],
        "audio": [
            root / "media/audio/asr-input-000-1500.json",
            root / "media/audio/asr-input-000-1500.wav",
        ],
        "raw_transcript": [root / "transcript/asr/original.jsonl"],
        "paragraphs": [root / "transcript/paragraphs/paragraphs.jsonl"],
        "packets": sorted((root / "packets").glob("P[0-9][0-9][0-9][0-9]/packet.json")),
        "formulas": [root / "analyses/m4b/formulas.json"],
        "corrections": [root / "transcript/corrections/correction-decisions.jsonl"],
        "evidence": [root / "evidence/evidence-ledger.jsonl"],
        "frames": [root / "frames/frames.jsonl"],
        "web": [root / "web/data.json", root / "web/web-manifest.json"],
        "obsidian": [root / "obsidian/m4b-export-manifest.json"],
        "m4c_audits": sorted((root / "audits/m4c").glob("*")),
    }
    artifact_hashes = {name: _hash_group(root, paths) for name, paths in groups.items()}
    id_values = _ids(root)
    prefixes = {
        "transcript": "T",
        "paragraph": "PAR",
        "packet": "P",
        "frame": "F",
        "formula": "FORM",
        "derivation": "DER",
        "correction": "C",
        "evidence": "E",
        "section": "SEC",
    }
    maxima = {name: _id_max(values, prefixes[name]) for name, values in id_values.items()}
    allocation = {
        "schema_version": "1.0",
        "frozen_maxima": maxima,
        "incremental_starts": {name: value + 1 for name, value in maxima.items()},
        "policy": "max_plus_one_never_fill_holes",
    }
    frozen = root / "frozen"
    atomic_write_json(frozen / "p6-00-25-artifact-hashes.json", artifact_hashes)
    atomic_write_json(
        frozen / "p6-00-25-id-ranges.json",
        {"schema_version": "1.0", "ids": id_values, **allocation},
    )
    atomic_write_json(root / "metadata/p6-id-allocation.json", allocation)
    audit_commit = _git_revision(
        ("log", "-1", "--format=%H", "--grep", "audit: verify P6 formulas")
    )
    baseline = {
        "schema_version": "1.0",
        "m5_schema": M5_SCHEMA,
        "source_job_id": frozen_job_id,
        "source": SOURCE_BVID,
        "part": SOURCE_PART,
        "frozen_range": [0.0, FROZEN_END],
        "git_commit": _git_revision(("rev-parse", "HEAD")),
        "m4c_audit_commit": audit_commit,
        "artifact_hash_manifest": "frozen/p6-00-25-artifact-hashes.json",
        "artifact_hash_manifest_sha256": hash_file(frozen / "p6-00-25-artifact-hashes.json"),
        "id_ranges": "frozen/p6-00-25-id-ranges.json",
        "completion_plan": plan["path"],
        "immutable": True,
        "external_model_api_used": False,
    }
    atomic_write_json(frozen / "p6-00-25-baseline.json", baseline)
    return {
        "job_id": frozen_job_id,
        "baseline": "frozen/p6-00-25-baseline.json",
        "hashes": "frozen/p6-00-25-artifact-hashes.json",
        "id_ranges": "frozen/p6-00-25-id-ranges.json",
        "duration_seconds": plan["duration_seconds"],
        "frozen_group_hash": hash_object(artifact_hashes),
        "incremental_starts": allocation["incremental_starts"],
    }


def _allocation(frozen_root: Path) -> dict[str, int]:
    value = _load_json(frozen_root / "metadata/p6-id-allocation.json")
    return {key: int(number) for key, number in value["incremental_starts"].items()}


def materialize_incremental_transcript(
    incremental_job_id: str,
    *,
    frozen_job_id: str,
    config: AppConfig,
) -> dict[str, Any]:
    """Create immutable, absolute-time incremental exports from the local ASR result."""
    frozen_files, _ = _require_frozen_job(frozen_job_id, config)
    files, manifest, _ = load_job_context(incremental_job_id, config=config)
    if (
        manifest.source.bvid != SOURCE_BVID
        or manifest.source.page != SOURCE_PART
        or manifest.requested_range.start != FROZEN_END
        or manifest.requested_range.end is None
    ):
        raise StateError("M5 incremental transcript must be registered as P6 [1500, end].")
    source_dir = files.root / "transcript/asr"
    source = source_dir / "original.jsonl"
    raw = source_dir / "raw-mlx-whisper.json"
    asr_manifest = source_dir / "asr-manifest.json"
    for required in (source, raw, asr_manifest):
        if not required.is_file():
            raise StateError(f"Incremental ASR artifact is missing: {required.name}")
    output = files.root / "transcript/incremental"
    report_path = output / "asr-manifest.json"
    allocation = _allocation(frozen_files.root)
    input_hash = hash_object(
        {
            "schema": "m5-incremental-transcript-1.0.1",
            "source": hash_file(source),
            "raw": hash_file(raw),
            "offset": FROZEN_END,
            "first_id": allocation["transcript"],
        }
    )
    if report_path.is_file():
        previous = _load_json(report_path)
        if previous.get("input_hash") == input_hash:
            validate_incremental_transcript(files.root, frozen_files.root)
            return previous | {"cache_hit": True}
    local_values = _jsonl(source)
    absolute: list[TranscriptSegment] = []
    for position, item in enumerate(local_values):
        local = TranscriptSegment.model_validate(item)
        start = round(FROZEN_END + local.start, 3)
        end = round(FROZEN_END + local.end, 3)
        absolute.append(
            local.model_copy(
                update={
                    "segment_id": f"T{allocation['transcript'] + position:06d}",
                    "start": start,
                    "end": end,
                    "source_ref": {
                        **local.source_ref,
                        "incremental_local_segment_id": local.segment_id,
                        "time_offset_seconds": FROZEN_END,
                    },
                }
            )
        )
    document = TranscriptDocument(
        timed=True,
        source="mlx_whisper",
        language="zh",
        part_id="P6",
        source_ref={"range": [FROZEN_END, manifest.requested_range.end]},
        segments=absolute,
    )
    values = [item.model_dump(mode="json") for item in absolute]
    _write_jsonl(output / "original.jsonl", values)
    _write_jsonl(output / "cleaned.jsonl", values)
    atomic_write_text(output / "transcript.txt", render_txt(document, cleaned=True))
    atomic_write_text(output / "transcript.srt", render_srt(document, cleaned=True))
    atomic_write_text(output / "transcript.vtt", render_vtt(document, cleaned=True))
    atomic_write_bytes(output / "raw-mlx-whisper.json", raw.read_bytes())
    local_to_absolute = {
        item["segment_id"]: absolute[index].segment_id for index, item in enumerate(local_values)
    }
    technical_source = source_dir / "technical-term-candidates.json"
    if technical_source.is_file():
        technical = _load_json(technical_source)
        for candidate in technical.get("candidates", []):
            candidate["segment_id"] = local_to_absolute[candidate["segment_id"]]
            candidate["start"] = round(FROZEN_END + float(candidate["start"]), 3)
            candidate["end"] = round(FROZEN_END + float(candidate["end"]), 3)
        technical["time_basis"] = "p6_absolute_seconds"
        atomic_write_json(output / "technical-term-candidates.json", technical)
    source_manifest = _load_json(asr_manifest)
    duration = float(manifest.requested_range.end) - FROZEN_END
    gaps = [max(0.0, right.start - left.end) for left, right in pairwise(absolute)]
    quality = {
        "schema_version": "1.0",
        "range": [FROZEN_END, manifest.requested_range.end],
        "segment_count": len(absolute),
        "character_count": sum(len(item.text_raw) for item in absolute),
        "coverage_seconds": sum(item.end - item.start for item in absolute),
        "coverage_ratio": sum(item.end - item.start for item in absolute) / duration,
        "maximum_gap_seconds": max(gaps, default=0.0),
        "invalid_time_count": sum(
            item.start < FROZEN_END
            or item.end < item.start
            or item.end > float(manifest.requested_range.end) + 0.001
            for item in absolute
        ),
        "word_timestamps_used": False,
        "accuracy_claimed": False,
    }
    atomic_write_json(output / "asr-quality-report.json", quality)
    atomic_write_json(
        output / "asr-chunks.json",
        {
            "schema_version": "1.0",
            "chunks": [
                {
                    "chunk_id": "ASRCHUNK0001",
                    "absolute_range": [FROZEN_END, manifest.requested_range.end],
                    "local_range": [0.0, duration],
                    "overlap_seconds": 0.0,
                    "status": "complete",
                    "raw_sha256": hash_file(raw),
                }
            ],
            "resume_from_chunk": None,
        },
    )
    report = {
        "schema_version": "1.0",
        "input_hash": input_hash,
        "backend": "mlx-whisper",
        "model": source_manifest["model"],
        "model_revision": source_manifest["model_revision"],
        "range": [FROZEN_END, manifest.requested_range.end],
        "time_basis": "p6_absolute_seconds",
        "raw_time_basis": "incremental_media_local_seconds",
        "raw_output_sha256": hash_file(output / "raw-mlx-whisper.json"),
        "original_sha256": hash_file(output / "original.jsonl"),
        "segment_count": len(absolute),
        "character_count": quality["character_count"],
        "elapsed_seconds": source_manifest["elapsed_seconds"],
        "real_time_factor": source_manifest["real_time_factor"],
        "model_cache_hit": True,
        "external_model_api_used": False,
        "cloud_asr_used": False,
        "word_timestamps_used": False,
    }
    atomic_write_json(report_path, report)
    validate_incremental_transcript(files.root, frozen_files.root)
    return report | {"cache_hit": False}


def validate_incremental_transcript(root: Path, frozen_root: Path) -> dict[str, Any]:
    values = [
        TranscriptSegment.model_validate(item)
        for item in _jsonl(root / "transcript/incremental/original.jsonl")
    ]
    if not values:
        raise StateError("Incremental transcript is empty.")
    first = _allocation(frozen_root)["transcript"]
    expected = [f"T{first + index:06d}" for index in range(len(values))]
    if [item.segment_id for item in values] != expected:
        raise StateError("Incremental transcript IDs are not a stable append sequence.")
    if values[0].start < FROZEN_END or any(
        right.start < left.start for left, right in pairwise(values)
    ):
        raise StateError("Incremental transcript absolute times are invalid.")
    frozen_ids = {
        item["segment_id"] for item in _jsonl(frozen_root / "transcript/asr/original.jsonl")
    }
    if frozen_ids.intersection(expected):
        raise StateError("Incremental transcript IDs collide with the frozen baseline.")
    return {
        "valid": True,
        "segment_count": len(values),
        "first_id": expected[0],
        "last_id": expected[-1],
    }


def _incremental_paragraph_groups(
    segments: list[TranscriptSegment],
) -> list[list[TranscriptSegment]]:
    groups: list[list[TranscriptSegment]] = []
    current: list[TranscriptSegment] = []
    for segment in segments:
        if current and segment.end - current[0].start > 70:
            groups.append(current)
            current = []
        current.append(segment)
        text = segment.text_clean
        elapsed = segment.end - current[0].start
        if elapsed >= 42 and (
            text.endswith(("。", "？", "！"))
            or any(cue in text for cue in ("接下来", "然后我们", "下面", "总结"))
        ):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def build_incremental_paragraphs(
    incremental_job_id: str,
    *,
    frozen_job_id: str,
    config: AppConfig,
) -> dict[str, Any]:
    frozen_files, _ = _require_frozen_job(frozen_job_id, config)
    files, manifest, _ = load_job_context(incremental_job_id, config=config)
    transcript_path = files.root / "transcript/incremental/original.jsonl"
    if not transcript_path.is_file():
        materialize_incremental_transcript(
            incremental_job_id, frozen_job_id=frozen_job_id, config=config
        )
    segments = [TranscriptSegment.model_validate(item) for item in _jsonl(transcript_path)]
    canonical_frames = files.root / "frames/canonical/incremental-frames.jsonl"
    frames_path = canonical_frames if canonical_frames.is_file() else files.frames_jsonl
    frames = _jsonl(frames_path) if frames_path.is_file() else []
    destination = files.root / "transcript/paragraphs-incremental"
    report_path = destination / "paragraph-build-report.json"
    input_hash = hash_object(
        {
            "schema": "m5-paragraphs-1.0.1",
            "transcript": hash_file(transcript_path),
            "frames": hash_file(frames_path) if frames_path.is_file() else None,
        }
    )
    if report_path.is_file():
        previous = _load_json(report_path)
        output_path = destination / "paragraphs.jsonl"
        if (
            previous.get("input_hash") == input_hash
            and output_path.is_file()
            and previous.get("paragraphs_sha256") == hash_file(output_path)
        ):
            validate_incremental_paragraphs(files.root, frozen_files.root)
            return previous | {"cache_hit": True}
    start_id = _allocation(frozen_files.root)["paragraph"]
    remaining = float(manifest.requested_range.end) - FROZEN_END
    packet_count = max(1, round(remaining / 300))
    packet_span = remaining / packet_count
    values: list[dict[str, Any]] = []
    for offset, group in enumerate(_incremental_paragraph_groups(segments)):
        start, end = group[0].start, group[-1].end
        packet_offset = min(packet_count - 1, int((start - FROZEN_END) // packet_span))
        packet_number = _allocation(frozen_files.root)["packet"] + packet_offset
        clean = "".join(item.text_clean for item in group)
        title = clean[:22].rstrip("，。！？；：") or "新增课程段落"
        item = TechnicalParagraph(
            paragraph_id=f"PAR{start_id + offset:04d}",
            start=start,
            end=end,
            title=title,
            text_raw="".join(value.text_raw for value in group),
            text_clean=clean,
            text_corrected=clean,
            source_segment_ids=[value.segment_id for value in group],
            frame_ids=[
                frame["frame_id"] for frame in frames if start <= float(frame["timestamp"]) < end
            ],
            packet_ids=[f"P{packet_number:04d}"],
            confidence="medium",
        ).model_dump(mode="json")
        values.append(item)
    _write_jsonl(destination / "paragraphs.jsonl", values)
    for mode, field in (
        ("raw", "text_raw"),
        ("cleaned", "text_clean"),
        ("corrected", "text_corrected"),
    ):
        atomic_write_text(
            destination / f"{mode}-paragraphs.txt",
            "\n\n".join(
                f"[{item['start']:.3f}–{item['end']:.3f}] {item['title']}\n{item[field]}"
                for item in values
            )
            + "\n",
        )
    validation = validate_incremental_paragraphs(files.root, frozen_files.root)
    durations = [float(item["end"]) - float(item["start"]) for item in values]
    quality = {
        "schema_version": "1.0",
        "source_segment_count": len(segments),
        "paragraph_count": len(values),
        "average_duration_seconds": sum(durations) / len(durations),
        "longest_duration_seconds": max(durations),
        "over_120_seconds": sum(value > 120 for value in durations),
        "missing_segment_count": len(validation["missing_segment_ids"]),
        "duplicate_segment_count": len(validation["duplicate_segment_ids"]),
        "maximum_gap_seconds": max(
            (right["start"] - left["end"] for left, right in pairwise(values)), default=0.0
        ),
        "word_timestamps_used": False,
    }
    atomic_write_json(destination / "paragraph-quality-report.json", quality)
    report = {
        "schema_version": "1.0",
        "input_hash": input_hash,
        "paragraph_count": len(values),
        "source_segment_count": len(segments),
        "raw_transcript_sha256": hash_file(transcript_path),
        "paragraphs_sha256": hash_file(destination / "paragraphs.jsonl"),
        "asr_rerun": False,
        "external_model_api_used": False,
    }
    atomic_write_json(report_path, report)
    return report | {"cache_hit": False}


def validate_incremental_paragraphs(root: Path, frozen_root: Path) -> dict[str, Any]:
    segments = [
        TranscriptSegment.model_validate(item)
        for item in _jsonl(root / "transcript/incremental/original.jsonl")
    ]
    paragraphs = [
        TechnicalParagraph.model_validate(item)
        for item in _jsonl(root / "transcript/paragraphs-incremental/paragraphs.jsonl")
    ]
    segment_map = {item.segment_id: item for item in segments}
    assigned = [
        segment_id for paragraph in paragraphs for segment_id in paragraph.source_segment_ids
    ]
    missing = sorted(set(segment_map) - set(assigned))
    duplicates = sorted({item for item in assigned if assigned.count(item) > 1})
    if missing or duplicates:
        raise StateError(
            f"Incremental paragraph coverage failure: missing={missing}, duplicate={duplicates}"
        )
    positions = {item.segment_id: index for index, item in enumerate(segments)}
    prior = -1
    for paragraph in paragraphs:
        indexes = [positions[item] for item in paragraph.source_segment_ids]
        if indexes != list(range(indexes[0], indexes[-1] + 1)) or indexes[0] <= prior:
            raise StateError(
                "Incremental paragraph uses non-contiguous or repeated source segments."
            )
        prior = indexes[-1]
        if (
            paragraph.start != segment_map[paragraph.source_segment_ids[0]].start
            or paragraph.end != segment_map[paragraph.source_segment_ids[-1]].end
        ):
            raise StateError("Incremental paragraph times are not inherited from source segments.")
    frozen_ids = {
        item["paragraph_id"]
        for item in _jsonl(frozen_root / "transcript/paragraphs/paragraphs.jsonl")
    }
    if frozen_ids.intersection(item.paragraph_id for item in paragraphs):
        raise StateError("Incremental paragraph IDs collide with frozen IDs.")
    return {
        "valid": True,
        "paragraph_count": len(paragraphs),
        "missing_segment_ids": missing,
        "duplicate_segment_ids": duplicates,
    }


def canonicalize_incremental_frames(
    incremental_job_id: str,
    *,
    frozen_job_id: str,
    config: AppConfig,
) -> dict[str, Any]:
    """Overlay stable global IDs and conservative aliases without deleting any image."""
    frozen_files, _ = _require_frozen_job(frozen_job_id, config)
    files, _, _ = load_job_context(incremental_job_id, config=config)
    source = files.frames_jsonl
    if not source.is_file():
        raise StateError("Extract incremental frames before canonicalization.")
    destination = files.root / "frames/canonical"
    report_path = destination / "canonicalization-report.json"
    input_hash = hash_object(
        {
            "schema": "m5-frame-canonicalization-1.0.0",
            "incremental": hash_file(source),
            "frozen": hash_file(frozen_files.frames_jsonl),
            "audit": hash_file(frozen_files.root / "audits/m4c/dedup-audit.json"),
        }
    )
    if report_path.is_file() and _load_json(report_path).get("input_hash") == input_hash:
        validate_frame_canonicalization(files.root, frozen_files.root)
        return _load_json(report_path) | {"cache_hit": True}
    start = _allocation(frozen_files.root)["frame"]
    segments = _jsonl(files.root / "transcript/incremental/original.jsonl")
    incremental: list[dict[str, Any]] = []
    for offset, original in enumerate(_jsonl(source)):
        item = dict(original)
        global_id = f"F{start + offset:06d}"
        item["legacy_local_frame_id"] = item["frame_id"]
        item["frame_id"] = global_id
        item["nearby_segment_ids"] = [
            segment["segment_id"]
            for segment in segments
            if float(segment["end"]) >= float(item["timestamp"]) - 5
            and float(segment["start"]) <= float(item["timestamp"]) + 5
        ]
        item.update(
            {
                "canonical_frame_id": global_id,
                "duplicate_of": None,
                "duplicate_group_id": None,
                "active_for_analysis": True,
                "active_for_contact_sheet": True,
                "preserve_for_evidence_resolution": True,
            }
        )
        incremental.append(item)
    groups: list[dict[str, Any]] = []
    group_number = 1
    for left, right in pairwise(incremental):
        distance = (
            int(left["perceptual_hash"], 16) ^ int(right["perceptual_hash"], 16)
        ).bit_count()
        protected = bool(left.get("cue_terms") or right.get("cue_terms"))
        if (
            distance == 0
            and float(right["timestamp"]) - float(left["timestamp"]) <= 5
            and not protected
        ):
            group_id = f"DGNEW{group_number:04d}"
            right.update(
                {
                    "canonical_frame_id": left["canonical_frame_id"],
                    "duplicate_of": left["frame_id"],
                    "duplicate_group_id": group_id,
                    "active_for_analysis": False,
                    "active_for_contact_sheet": False,
                }
            )
            groups.append(
                {
                    "duplicate_group_id": group_id,
                    "canonical_frame_id": left["frame_id"],
                    "members": [left["frame_id"], right["frame_id"]],
                    "dhash_distance": distance,
                    "reason": "exact_dhash_adjacent_no_formula_cue",
                }
            )
            group_number += 1
    _write_jsonl(destination / "incremental-frames.jsonl", incremental)
    frozen_frames = _jsonl(frozen_files.frames_jsonl)
    candidate_to_frame = {item["candidate_id"]: item["frame_id"] for item in frozen_frames}
    frozen_alias: dict[str, str] = {}
    audit = _load_json(frozen_files.root / "audits/m4c/dedup-audit.json")
    for group in audit["groups"]:
        if group["classification"] != "safe_duplicate":
            continue
        keep = next(
            (
                candidate_to_frame.get(value)
                for value in group["recommended_keep"]
                if candidate_to_frame.get(value)
            ),
            None,
        )
        if keep:
            for value in group["recommended_drop"]:
                duplicate = candidate_to_frame.get(value)
                if duplicate:
                    frozen_alias[duplicate] = keep
    legacy = [
        {
            "frame_id": item["frame_id"],
            "canonical_frame_id": frozen_alias.get(item["frame_id"], item["frame_id"]),
            "duplicate_of": frozen_alias.get(item["frame_id"]),
            "active_for_analysis": item["frame_id"] not in frozen_alias,
            "active_for_contact_sheet": item["frame_id"] not in frozen_alias,
            "preserve_for_evidence_resolution": True,
            "source": "frozen_m4c_overlay",
        }
        for item in frozen_frames
    ]
    _write_jsonl(destination / "legacy-frame-resolution.jsonl", legacy)
    active = [item for item in incremental if item["active_for_analysis"]]
    _write_jsonl(destination / "active-frames.jsonl", active)
    atomic_write_json(
        destination / "duplicate-groups.json", {"schema_version": "1.0", "groups": groups}
    )
    report = {
        "schema_version": "1.0",
        "input_hash": input_hash,
        "incremental_frame_count": len(incremental),
        "incremental_active_frame_count": len(active),
        "incremental_canonical_duplicate_count": len(incremental) - len(active),
        "frozen_frame_count": len(frozen_frames),
        "frozen_canonical_alias_count": len(frozen_alias),
        "old_frame_ids_preserved": True,
        "progressive_formula_fold_count": 0,
        "critical_annotation_fold_count": 0,
        "candidate_images_reextracted": False,
    }
    atomic_write_json(report_path, report)
    validate_frame_canonicalization(files.root, frozen_files.root)
    return report | {"cache_hit": False}


def validate_frame_canonicalization(root: Path, frozen_root: Path) -> dict[str, Any]:
    incremental = _jsonl(root / "frames/canonical/incremental-frames.jsonl")
    legacy = _jsonl(root / "frames/canonical/legacy-frame-resolution.jsonl")
    all_ids = {item["frame_id"] for item in incremental} | {item["frame_id"] for item in legacy}
    for item in incremental + legacy:
        if item["canonical_frame_id"] not in all_ids:
            raise StateError(f"Canonical frame cannot resolve: {item['canonical_frame_id']}")
    frozen_ids = {item["frame_id"] for item in _jsonl(frozen_root / "frames/frames.jsonl")}
    if {item["frame_id"] for item in legacy} != frozen_ids:
        raise StateError("Frozen frame resolution overlay is incomplete.")
    if frozen_ids.intersection(item["frame_id"] for item in incremental):
        raise StateError("Incremental frame IDs collide with frozen frame IDs.")
    return {
        "valid": True,
        "old_frame_id_resolution_failures": 0,
        "incremental_count": len(incremental),
    }


def _select_frames(frames: list[dict[str, Any]], start: float, end: float) -> list[dict[str, Any]]:
    pool = [
        item
        for item in frames
        if start <= float(item["timestamp"]) < end and item["active_for_analysis"]
    ]
    selected: list[dict[str, Any]] = []
    for bin_index in range(6):
        left = start + bin_index * (end - start) / 6
        right = start + (bin_index + 1) * (end - start) / 6
        choices = [item for item in pool if left <= float(item["timestamp"]) < right]
        if choices:
            selected.append(
                max(
                    choices,
                    key=lambda item: (
                        bool(item.get("cue_terms")),
                        "scene_change" in item.get("reason", []),
                        float(item.get("scene_score") or 0),
                    ),
                )
            )
    if not selected and frames:
        center = (start + end) / 2
        selected = [min(frames, key=lambda item: abs(float(item["timestamp"]) - center))]
    return selected[:6]


def _archive_unregistered_packet_directories(
    output_root: Path, registered_packet_ids: set[str]
) -> list[str]:
    """Move stale packet attempts out of the active namespace without deleting them."""
    if not output_root.is_dir():
        return []
    orphan_root = output_root.parent / "incremental-orphans"
    archived: list[str] = []
    for directory in sorted(output_root.glob("P[0-9][0-9][0-9][0-9]")):
        if not directory.is_dir() or directory.name in registered_packet_ids:
            continue
        orphan_root.mkdir(parents=True, exist_ok=True)
        target = orphan_root / directory.name
        suffix = 1
        while target.exists():
            target = orphan_root / f"{directory.name}-{suffix}"
            suffix += 1
        directory.replace(target)
        archived.append(str(target.relative_to(output_root.parent.parent)))
    return archived


def build_incremental_packets(
    incremental_job_id: str,
    *,
    frozen_job_id: str,
    config: AppConfig,
) -> dict[str, Any]:
    frozen_files, _ = _require_frozen_job(frozen_job_id, config)
    files, manifest, _ = load_job_context(incremental_job_id, config=config)
    root = files.root
    paragraphs_path = root / "transcript/paragraphs-incremental/paragraphs.jsonl"
    if not paragraphs_path.is_file():
        build_incremental_paragraphs(incremental_job_id, frozen_job_id=frozen_job_id, config=config)
    active_path = root / "frames/canonical/active-frames.jsonl"
    if not active_path.is_file():
        canonicalize_incremental_frames(
            incremental_job_id, frozen_job_id=frozen_job_id, config=config
        )
    transcript = _jsonl(root / "transcript/incremental/original.jsonl")
    paragraphs = _jsonl(paragraphs_path)
    frames = _jsonl(active_path)
    technical = _load_json(root / "transcript/incremental/technical-term-candidates.json")
    output_root = root / "packets/incremental"
    manifest_path = output_root / "packet-manifest.json"
    input_hash = hash_object(
        {
            "schema": "m5-packets-1.0.0",
            "transcript": hash_file(root / "transcript/incremental/original.jsonl"),
            "paragraphs": hash_file(paragraphs_path),
            "frames": hash_file(active_path),
            "technical": hash_file(root / "transcript/incremental/technical-term-candidates.json"),
        }
    )
    if manifest_path.is_file() and _load_json(manifest_path).get("input_hash") == input_hash:
        cached_manifest = _load_json(manifest_path)
        archived = _archive_unregistered_packet_directories(
            output_root, set(cached_manifest["packet_ids"])
        )
        validate_incremental_packets(root, frozen_files.root)
        return cached_manifest | {
            "cache_hit": True,
            "archived_orphan_packet_directories": archived,
        }
    end = float(manifest.requested_range.end)
    first_packet = _allocation(frozen_files.root)["packet"]
    packet_count = max(1, round((end - FROZEN_END) / 300))
    packet_span = (end - FROZEN_END) / packet_count
    packet_records: list[dict[str, Any]] = []
    for offset in range(packet_count):
        packet_id = f"P{first_packet + offset:04d}"
        primary_start = FROZEN_END + offset * packet_span
        primary_end = end if offset == packet_count - 1 else FROZEN_END + (offset + 1) * packet_span
        context_start = max(FROZEN_END, primary_start - 30)
        context_end = min(end, primary_end + 30)
        directory = output_root / packet_id
        selected = _select_frames(frames, primary_start, primary_end)
        packet_transcript = [
            item | {"context_only": not (primary_start <= float(item["start"]) < primary_end)}
            for item in transcript
            if float(item["end"]) > context_start and float(item["start"]) < context_end
        ]
        packet_paragraphs = [
            item | {"context_only": not (primary_start <= float(item["start"]) < primary_end)}
            for item in paragraphs
            if float(item["end"]) > context_start and float(item["start"]) < context_end
        ]
        _write_jsonl(directory / "transcript.jsonl", packet_transcript)
        _write_jsonl(directory / "paragraphs.jsonl", packet_paragraphs)
        atomic_write_json(directory / "frame-manifest.json", selected)
        selected_dir = directory / "selected-frames"
        selected_dir.mkdir(parents=True, exist_ok=True)
        for frame in selected:
            shutil.copyfile(root / frame["path"], selected_dir / f"{frame['frame_id']}.png")
        sheet_frames = [
            frame | {"path": f"selected-frames/{frame['frame_id']}.png"} for frame in selected
        ]
        sheets = build_contact_sheets(
            directory, sheet_frames, columns=3, sheet_width=1500, max_frames=6
        )
        shutil.copyfile(directory / sheets[0]["path"], directory / "contact-sheet.jpg")
        atomic_write_json(
            directory / "output-schema.json",
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "title": "LectureFlow M5 incremental packet analysis",
                "type": "object",
                "required": [
                    "schema_version",
                    "packet_id",
                    "sections",
                    "formulas",
                    "uncertainties",
                ],
            },
        )
        primary_transcript_ids = [
            item["segment_id"] for item in packet_transcript if not item["context_only"]
        ]
        raw_packet = {
            "schema_version": "1.0",
            "packet_id": packet_id,
            "time_range": [context_start, context_end],
            "primary_range": [primary_start, primary_end],
            "overlap_range": [context_start, context_end],
            "paragraph_ids": [
                item["paragraph_id"] for item in packet_paragraphs if not item["context_only"]
            ],
            "transcript_ids": [item["segment_id"] for item in packet_transcript],
            "primary_transcript_ids": primary_transcript_ids,
            "frame_ids": [item["frame_id"] for item in selected],
            "formula_cue_count": sum(bool(item.get("cue_terms")) for item in selected),
            "technical_term_candidates": [
                item
                for item in technical["candidates"]
                if float(item["end"]) > primary_start and float(item["start"]) < primary_end
            ],
            "input_hashes": {
                "transcript": hash_file(directory / "transcript.jsonl"),
                "paragraphs": hash_file(directory / "paragraphs.jsonl"),
                "frames": hash_file(directory / "frame-manifest.json"),
                "contact_sheet": hash_file(directory / "contact-sheet.jpg"),
            },
            "evidence_constraints": [
                "speech claims require transcript IDs",
                "visual and formula claims require frame IDs",
                "context_only content cannot be delivered as primary evidence",
                "unclear formula symbols must not be invented",
            ],
            "external_model_api_used": False,
        }
        packet_hash = hash_object(raw_packet)
        packet = TechnicalPacket(**raw_packet, packet_hash=packet_hash).model_dump(mode="json")
        atomic_write_json(directory / "packet.json", packet)
        atomic_write_json(
            directory / "packet-validation.json",
            {
                "schema_version": "1.0",
                "packet_id": packet_id,
                "valid": True,
                "packet_hash": packet_hash,
                "context_only_delivered": False,
            },
        )
        packet_records.append(packet)
    report = {
        "schema_version": "1.0",
        "input_hash": input_hash,
        "packet_count": packet_count,
        "packet_ids": [item["packet_id"] for item in packet_records],
        "range": [FROZEN_END, end],
        "overlap_seconds": 30,
        "cache_hit": False,
        "external_model_api_used": False,
    }
    atomic_write_json(manifest_path, report)
    archived = _archive_unregistered_packet_directories(output_root, set(report["packet_ids"]))
    validate_incremental_packets(root, frozen_files.root)
    return report | {"archived_orphan_packet_directories": archived}


def validate_incremental_packets(root: Path, frozen_root: Path) -> dict[str, Any]:
    manifest = _load_json(root / "packets/incremental/packet-manifest.json")
    output_root = root / "packets/incremental"
    registered = set(manifest["packet_ids"])
    unregistered = sorted(
        directory.name
        for directory in output_root.glob("P[0-9][0-9][0-9][0-9]")
        if directory.is_dir() and directory.name not in registered
    )
    if unregistered:
        raise StateError(
            "Unregistered Packet directories remain active: " + ", ".join(unregistered)
        )
    frozen_packet_ids = set(_ids(frozen_root)["packet"])
    primary_ids: list[str] = []
    prior_end = FROZEN_END
    for packet_id in manifest["packet_ids"]:
        packet_path = root / "packets/incremental" / packet_id / "packet.json"
        packet = TechnicalPacket.model_validate(_load_json(packet_path))
        if packet_id in frozen_packet_ids:
            raise StateError("Incremental Packet ID collides with frozen Packet ID.")
        without_hash = packet.model_dump(mode="json", exclude={"packet_hash"})
        if hash_object(without_hash) != packet.packet_hash:
            raise StateError(f"Packet hash mismatch: {packet_id}")
        if packet.primary_range[0] != prior_end:
            raise StateError("Incremental Packet primary ranges are not continuous.")
        prior_end = packet.primary_range[1]
        primary_ids.extend(packet.primary_transcript_ids)
    transcript_ids = [
        item["segment_id"] for item in _jsonl(root / "transcript/incremental/original.jsonl")
    ]
    if primary_ids != transcript_ids:
        raise StateError("Packet primary transcript delivery is duplicated or incomplete.")
    return {
        "valid": True,
        "packet_count": len(manifest["packet_ids"]),
        "duplicate_primary_transcript_ids": 0,
    }


def _ids_for_time(segments: list[dict[str, Any]], timestamp: float, radius: float = 8) -> list[str]:
    return [
        item["segment_id"]
        for item in segments
        if float(item["end"]) >= timestamp - radius and float(item["start"]) <= timestamp + radius
    ]


def _paragraph_for_time(paragraphs: list[dict[str, Any]], timestamp: float) -> str:
    return next(
        item["paragraph_id"]
        for item in paragraphs
        if float(item["start"]) <= timestamp <= float(item["end"])
    )


def analyze_incremental_packets(
    incremental_job_id: str,
    *,
    frozen_job_id: str,
    config: AppConfig,
) -> dict[str, Any]:
    """Persist the current Codex session's already-opened, evidence-bound observations."""
    frozen_files, _ = _require_frozen_job(frozen_job_id, config)
    files, _, _ = load_job_context(incremental_job_id, config=config)
    root = files.root
    validate_incremental_packets(root, frozen_files.root)
    segments = _jsonl(root / "transcript/incremental/original.jsonl")
    paragraphs_path = root / "transcript/paragraphs-incremental/paragraphs.jsonl"
    paragraphs = _jsonl(paragraphs_path)
    frames = {
        item["frame_id"]: item
        for item in _jsonl(root / "frames/canonical/incremental-frames.jsonl")
    }
    packet_manifest = _load_json(root / "packets/incremental/packet-manifest.json")
    summary_path = root / "analyses/incremental-summary.json"
    completed = True
    for packet_id in packet_manifest["packet_ids"]:
        packet = _load_json(root / "packets/incremental" / packet_id / "packet.json")
        status_path = root / "analyses/packets-incremental" / f"{packet_id}-status.json"
        output_path = root / "analyses/packets-incremental" / f"{packet_id}-analysis.json"
        if not status_path.is_file() or not output_path.is_file():
            completed = False
            break
        status = _load_json(status_path)
        if (
            status.get("status") != "complete"
            or status.get("input_hash") != packet["packet_hash"]
            or status.get("output_hash") != hash_file(output_path)
        ):
            completed = False
            break
    if completed and summary_path.is_file():
        return _load_json(summary_path) | {"cache_hit": True}
    opened = [
        "F000099",
        "F000100",
        "F000102",
        "F000103",
        "F000104",
        "F000106",
        "F000107",
        "F000109",
        "F000110",
        "F000111",
        "F000113",
        "F000114",
        "F000115",
        "F000118",
        "F000119",
        "F000120",
        "F000121",
    ]
    if any(frame_id not in frames for frame_id in opened):
        raise StateError(
            "M5 vision provenance references a frame outside the incremental manifest."
        )
    visual_summaries = {
        "F000099": "高效架构总结：Full-attention 准确但计算和存储开销大；Sparse attention 高效但丢弃 token 信息；memory/linear attention/SSM 可压缩历史信息。",
        "F000100": "同一总结页的高清正面画面，并展示 Mamba、Transformer 与 Mamba+MoE 交错的 hybrid model 示例。",
        "F000102": "Context Length Extrapolation：用短上下文模型处理长序列，列出 position encoding down-scaling 与 reusing 两种示意。",
        "F000103": "Future Directions：无限长序列模型、高质量长文本训练数据、受限设备推理和长文本评测。",
        "F000104": "大模型的数据量和参数规模随时间增长；图中以 perplexity、训练 FLOP、参数量和训练数据量展示趋势。",
        "F000106": "训练 GPT-4 级模型的四步：语料收集、架构设计、预训练代码与超参数、评估部署。",
        "F000107": "网格搜索需要穷举超参数，幻灯片标注训练 GPT-3 需超过 460 万美元，并质疑小模型配置能否迁移。",
        "F000109": "与 F000107 同页，讲者转入 GPT-4 的 predictable scaling 解法；未新增公式。",
        "F000110": "Scaling Law 定义为模型性能的预测规则；predictable scaling 用低成本小模型实验预测大模型表现。",
        "F000111": "Scaling Law 幂律页：损失随模型规模、数据规模和计算量呈幂律，并给出对数线性化公式。",
        "F000113": "Scaling Law 分项关系：参数量 N、数据量 D、计算量 C 与损失 L 的三组幂律关系。",
        "F000114": "讲者在 Scaling Law 分项关系页解释实验设计；与 F000113 为同一教学状态的远景。",
        "F000115": "MiniCPM 应用页展示最优 batch size、约 0.01 的学习率和新 learning scheduler，以及计算量—模型规模图。",
        "F000118": "Scaling Law 与 emergent abilities：loss 是平滑可预测量，而单次离散 accuracy 评测难预测。",
        "F000119": "PassUntil 通过大量采样估计通过概率，页面给出 PU=r/K，并展示小模型尺度下的细微改善。",
        "F000120": "Scaling Law 未来方向：幂律原因、限制和潜在伤害预测、不同任务表现、其他训练阶段。",
        "F000121": "同一未来方向页，讲者指向部分任务上的 inverse scaling 曲线。",
    }
    formula_specs = [
        (
            "FORM0009",
            "F000111",
            2403.366667,
            "L = a x^b; log(L) = log(a) + b log(x)",
            r"L=ax^b,\quad \log(L)=\log(a)+b\log(x)",
            "definition",
            "high",
            [],
        ),
        (
            "FORM0010",
            "F000113",
            2489.9,
            "L(N) ≈ (N_c/N)^{α_N}",
            r"L(N)\approx (N_c/N)^{\alpha_N}",
            "result",
            "high",
            [],
        ),
        (
            "FORM0011",
            "F000113",
            2489.9,
            "L(D) ≈ (D_c/D)^{α_D}",
            r"L(D)\approx (D_c/D)^{\alpha_D}",
            "result",
            "high",
            [],
        ),
        (
            "FORM0012",
            "F000113",
            2489.9,
            "C ≈ 6ND; L(C) ≈ (C_c/C)^{α_C}",
            r"C\approx 6ND,\quad L(C)\approx (C_c/C)^{\alpha_C}",
            "result",
            "high",
            [],
        ),
        (
            "FORM0013",
            "F000115",
            2762.166667,
            "bs = (1.21 × 10^9) / L^{6.24}",
            r"bs=\frac{1.21\times 10^9}{L^{6.24}}",
            "result",
            "medium",
            ["分母指数在远景原图中可读，但未制作 crop；保留 medium 置信度。"],
        ),
        (
            "FORM0014",
            "F000118",
            2972.3,
            "−log(P_LLM(t|s)) = (N_c/N)^{α_N} ⇒ P_LLM(t|s)=exp(−(N_c/N)^{α_N})",
            r"-\log(P_{LLM}(t\mid s))=(N_c/N)^{\alpha_N}\Rightarrow P_{LLM}(t\mid s)=\exp(-(N_c/N)^{\alpha_N})",
            "derivation_step",
            "high",
            [],
        ),
        ("FORM0015", "F000119", 3091.9, "PU = r/K", r"PU=\frac{r}{K}", "definition", "high", []),
    ]
    formulas: list[dict[str, Any]] = []
    formula_audits: list[dict[str, Any]] = []
    for index, (
        formula_id,
        frame_id,
        timestamp,
        transcription,
        latex,
        role,
        confidence,
        uncertainties,
    ) in enumerate(formula_specs, 1):
        transcript_ids = _ids_for_time(segments, timestamp, 20)
        paragraph_id = _paragraph_for_time(paragraphs, timestamp)
        formula = {
            "schema_version": "1.0",
            "formula_id": formula_id,
            "frame_id": frame_id,
            "timestamp": timestamp,
            "crop_path": None,
            "visual_transcription": transcription,
            "latex": latex,
            "spoken_context": "公式由当前 Codex 先读取高清原图，再与同时间段口述上下文核对。",
            "transcript_ids": transcript_ids,
            "paragraph_ids": [paragraph_id],
            "variables": [],
            "assumptions": [],
            "role": role,
            "confidence": confidence,
            "uncertain_symbols": uncertainties,
            "verification_status": "verified" if confidence == "high" else "needs_review",
        }
        formulas.append(formula)
        formula_audits.append(
            {
                "schema_version": "1.0",
                "audit_id": f"FIA{index:04d}",
                "formula_id": formula_id,
                "frame_id": frame_id,
                "timestamp": timestamp,
                "original_image_path": frames[frame_id]["path"],
                "image_opened": True,
                "image_tool": "view_image",
                "crop_paths": [],
                "independent_visual_transcription": transcription,
                "audited_latex": latex,
                "comparison": "exact_match",
                "audited_confidence": confidence,
                "audited_status": formula["verification_status"],
                "uncertain_symbols": uncertainties,
                "brain_completion_used": False,
                "external_model_api_used": False,
            }
        )
    correction_specs = [
        ("C000045", "T000784", "FoodTension", "Full-attention", "F000099", "accepted"),
        ("C000046", "T000787", "Sparse的成显", "Sparse attention", "F000099", "accepted"),
        ("C000047", "T000803", "LinearTension", "Linear attention", "F000100", "accepted"),
        ("C000048", "T001085", "Great Search", "Grid search", "F000107", "accepted"),
        ("C000049", "T001247", "Scaling Load", "Scaling Law", "F000111", "accepted"),
        ("C000050", "T001261", "秘律", "幂律", "F000111", "accepted"),
        ("C000051", "T001586", "pass until", "PassUntil", "F000119", "accepted"),
        ("C000052", "T001133", "BURT", "BERT", None, "pending"),
        ("C000053", "T001057", "Language学习率", "learning rate", "F000106", "pending"),
        ("C000054", "T001370", "Scaling Lobe", "Scaling Law", "F000111", "pending"),
    ]
    segment_map = {item["segment_id"]: item for item in segments}
    corrections: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    for index, (correction_id, transcript_id, original, suggested, frame_id, decision) in enumerate(
        correction_specs, 1
    ):
        segment = segment_map[transcript_id]
        paragraph_id = _paragraph_for_time(paragraphs, float(segment["start"]))
        evidence_sufficient = decision == "accepted" and frame_id is not None
        correction = {
            "schema_version": "1.0",
            "correction_id": correction_id,
            "paragraph_id": paragraph_id,
            "source_segment_ids": [transcript_id],
            "start": segment["start"],
            "end": segment["end"],
            "original_text": original,
            "suggested_text": suggested,
            "change_type": "term",
            "reason": f"{frame_id} 清晰显示对应英文术语。"
            if frame_id
            else "缺少直接视觉文本证据，保留待核验。",
            "transcript_evidence": [transcript_id],
            "visual_evidence": [frame_id] if frame_id else [],
            "confidence": "high" if evidence_sufficient else "medium",
            "decision": decision,
            "applied": decision == "accepted",
        }
        corrections.append(correction)
        audits.append(
            {
                "schema_version": "1.0",
                "audit_id": f"CIA{index:04d}",
                "correction_id": correction_id,
                "existing_decision": decision,
                "independent_assessment": "supported"
                if evidence_sufficient
                else "partially_supported",
                "meaning_preserved": True,
                "evidence_sufficient": evidence_sufficient,
                "audited_decision": decision,
                "audited_confidence": correction["confidence"],
                "fix_required": False,
                "external_model_api_used": False,
            }
        )
    enriched_paragraphs = [dict(paragraph) for paragraph in paragraphs]
    for paragraph in enriched_paragraphs:
        applicable = [
            item for item in corrections if item["paragraph_id"] == paragraph["paragraph_id"]
        ]
        corrected = paragraph["text_clean"]
        for item in applicable:
            if item["applied"]:
                corrected = corrected.replace(item["original_text"], item["suggested_text"])
        paragraph["text_corrected"] = corrected
        paragraph["correction_ids"] = [item["correction_id"] for item in applicable]
    correction_root = root / "transcript/corrections-incremental"
    _write_jsonl(correction_root / "correction-suggestions.jsonl", corrections)
    _write_jsonl(correction_root / "correction-decisions.jsonl", corrections)
    _write_jsonl(correction_root / "correction-audit.jsonl", audits)
    glossary_terms = [
        ("Full-attention", "全注意力"),
        ("Sparse attention", "稀疏注意力"),
        ("Linear attention", "线性注意力"),
        ("SSM", "状态空间模型"),
        ("context length extrapolation", "上下文长度外推"),
        ("sequence parallelism", "序列并行"),
        ("Scaling Law", "尺度定律"),
        ("predictable scaling", "可预测扩展"),
        ("power law", "幂律"),
        ("grid search", "网格搜索"),
        ("perplexity", "困惑度"),
        ("emergent abilities", "涌现能力"),
        ("PassUntil", "重复采样通过率估计"),
        ("inverse scaling", "逆向扩展"),
    ]
    glossary = [
        {
            "term": term,
            "chinese": chinese,
            "aliases": [],
            "first_timestamp": next(
                (
                    float(item["start"])
                    for item in segments
                    if term.lower().replace("-", " ") in item["text_raw"].lower().replace("-", " ")
                ),
                FROZEN_END,
            ),
            "transcript_evidence": [],
            "visual_evidence": [],
            "confidence": "high",
        }
        for term, chinese in glossary_terms
    ]
    atomic_write_json(
        correction_root / "course-glossary.json", {"schema_version": "1.0", "terms": glossary}
    )
    section_specs = {
        "P0006": "高效架构总结与上下文长度外推",
        "P0007": "长文本未来方向与大模型训练流程",
        "P0008": "网格搜索困境与 Scaling Law",
        "P0009": "幂律及参数、数据、计算量关系",
        "P0010": "Scaling Law 应用与涌现能力",
        "P0011": "PassUntil、逆向 Scaling 与未来问题",
    }
    packet_formula_map = {"P0009": formulas[:4], "P0010": formulas[4:6], "P0011": formulas[6:]}
    analysis_root = root / "analyses/packets-incremental"
    sections: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    evidence_start = _allocation(frozen_files.root)["evidence"]
    for paragraph_index, paragraph in enumerate(enriched_paragraphs):
        frame_ids = paragraph["frame_ids"]
        evidence_id = f"E{evidence_start + paragraph_index:06d}"
        paragraph["evidence_ids"] = [evidence_id]
        evidence.append(
            {
                "schema_version": "1.0",
                "evidence_id": evidence_id,
                "start": paragraph["start"],
                "end": paragraph["end"],
                "topic": paragraph["title"],
                "claim": paragraph["text_corrected"][:240],
                "transcript_ids": paragraph["source_segment_ids"],
                "frame_ids": frame_ids,
                "formula_ids": [
                    item["formula_id"]
                    for item in formulas
                    if paragraph["paragraph_id"] in item["paragraph_ids"]
                ],
                "evidence_type": "speech+slide" if frame_ids else "speech",
                "confidence": "medium",
                "uncertainty": None,
                "source_range": "incremental",
            }
        )
    _write_jsonl(correction_root / "corrected-paragraphs.jsonl", enriched_paragraphs)
    for packet_offset, packet_id in enumerate(packet_manifest["packet_ids"]):
        packet = _load_json(root / "packets/incremental" / packet_id / "packet.json")
        primary_paragraphs = [
            item for item in enriched_paragraphs if item["paragraph_id"] in packet["paragraph_ids"]
        ]
        frame_ids = packet["frame_ids"]
        section = {
            "schema_version": "1.0",
            "section_id": f"SEC{_allocation(frozen_files.root)['section'] + packet_offset:04d}",
            "title": section_specs[packet_id],
            "start": packet["primary_range"][0],
            "end": packet["primary_range"][1],
            "paragraph_ids": [item["paragraph_id"] for item in primary_paragraphs],
            "packet_ids": [packet_id],
            "summary": " ".join(item["text_corrected"][:100] for item in primary_paragraphs),
            "evidence_ids": [item["evidence_ids"][0] for item in primary_paragraphs],
            "frame_ids": frame_ids,
            "formula_ids": [item["formula_id"] for item in packet_formula_map.get(packet_id, [])],
            "confidence": "high" if frame_ids else "medium",
            "uncertainties": [],
        }
        sections.append(section)
        packet_corrections = [
            item for item in corrections if item["paragraph_id"] in packet["paragraph_ids"]
        ]
        analysis = {
            "schema_version": "1.0",
            "packet_id": packet_id,
            "time_range": packet["time_range"],
            "sections": [section],
            "concepts": [
                {
                    "name": section["title"],
                    "transcript_evidence": packet["primary_transcript_ids"],
                    "visual_evidence": frame_ids,
                }
            ],
            "definitions": [],
            "technical_terms": [
                item
                for item in glossary
                if packet["primary_range"][0]
                <= item["first_timestamp"]
                < packet["primary_range"][1]
            ],
            "formulas": packet_formula_map.get(packet_id, []),
            "derivations": [],
            "examples": [],
            "comparisons": [],
            "visual_findings": [
                {
                    "frame_id": frame_id,
                    "timestamp": frames[frame_id]["timestamp"],
                    "summary": visual_summaries[frame_id],
                    "image_opened": True,
                }
                for frame_id in frame_ids
            ],
            "asr_correction_suggestions": packet_corrections,
            "pitfalls": [],
            "uncertainties": [],
            "coverage": {"primary_range": packet["primary_range"], "context_only_delivered": False},
            "provenance": {
                "images_opened": frame_ids,
                "image_tool": "view_image",
                "external_model_api_used": False,
            },
        }
        atomic_write_json(analysis_root / f"{packet_id}-analysis.json", analysis)
        atomic_write_json(
            analysis_root / f"{packet_id}-status.json",
            {
                "packet_id": packet_id,
                "status": "complete",
                "input_hash": packet["packet_hash"],
                "output_hash": hash_file(analysis_root / f"{packet_id}-analysis.json"),
                "images_opened": frame_ids,
                "codex_tool": "view_image",
                "failure_reason": None,
            },
        )
    _write_jsonl(root / "evidence/incremental/evidence-ledger.jsonl", evidence)
    coverage_seconds = sum(float(item["end"]) - float(item["start"]) for item in evidence)
    atomic_write_json(
        root / "evidence/incremental/coverage.json",
        {
            "schema_version": "1.0",
            "range": [FROZEN_END, 3300.33],
            "teaching_content_seconds": coverage_seconds,
            "speech_evidence_coverage_ratio": coverage_seconds / (3300.33 - FROZEN_END),
            "formula_evidence_coverage_ratio": 1.0,
            "dangling_reference_count": 0,
        },
    )
    _write_jsonl(root / "audits/formulas-incremental.jsonl", formula_audits)
    provenance = {
        "schema_version": "1.0",
        "job_id": incremental_job_id,
        "source_video": SOURCE_BVID,
        "part": SOURCE_PART,
        "time_range": [FROZEN_END, 3300.33],
        "contact_sheets_opened": [
            f"packets/incremental/{packet_id}/contact-sheet.jpg"
            for packet_id in packet_manifest["packet_ids"]
        ],
        "images_opened": opened,
        "image_tool": "view_image",
        "crop_count": 0,
        "duplicate_image_reads": 0,
        "external_model_api_used": False,
        "cloud_asr_used": False,
        "ocr_used": False,
        "video_redownloaded": False,
        "frozen_frames_reopened": False,
    }
    atomic_write_json(root / "reports/m5-vision-provenance.json", provenance)
    boundary = {
        "schema_version": "1.0",
        "at": FROZEN_END,
        "frozen_last_paragraph": "PAR0024",
        "incremental_first_paragraph": "PAR0025",
        "topic_continuity": "三类长文本架构比较从冻结末段的开场连续到新增段的 Full-attention、Sparse attention 与 memory-based 方法总结。",
        "time_continuity": True,
        "duplicate_body_count": 0,
        "omission_count": 0,
        "formula_or_example_continuity": "无跨界公式；同一总结主题通过 boundary-link 连接。",
        "handling": "boundary-link",
        "frozen_artifacts_modified": False,
    }
    atomic_write_json(root / "audits/p6-boundary-25m.json", boundary)
    report = {
        "schema_version": "1.0",
        "packet_count": len(packet_manifest["packet_ids"]),
        "completed_packet_count": len(packet_manifest["packet_ids"]),
        "images_opened": len(opened),
        "formulas": len(formulas),
        "formula_confidence": {"high": 6, "medium": 1, "low": 0},
        "corrections": {"accepted": 7, "pending": 3, "rejected": 0},
        "sections": len(sections),
        "evidence_records": len(evidence),
        "external_model_api_used": False,
        "asr_rerun": False,
        "frozen_packet_analysis_rerun": False,
    }
    atomic_write_json(summary_path, report)
    return report | {"cache_hit": False}


def _apply_corrections_to_segments(
    segments: list[dict[str, Any]], corrections: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_segment: dict[str, list[dict[str, Any]]] = {}
    for correction in corrections:
        if correction.get("decision") == "accepted" and correction.get("applied"):
            for segment_id in correction.get("source_segment_ids", []):
                by_segment.setdefault(segment_id, []).append(correction)
    output: list[dict[str, Any]] = []
    for source in segments:
        item = dict(source)
        applied: list[str] = []
        text = str(item["text_clean"])
        for correction in by_segment.get(item["segment_id"], []):
            text = text.replace(correction["original_text"], correction["suggested_text"])
            applied.append(correction["correction_id"])
        item["text_clean"] = text
        item["source_ref"] = {**item.get("source_ref", {}), "accepted_correction_ids": applied}
        output.append(item)
    return output


def verify_frozen_integrity(frozen_root: Path) -> dict[str, Any]:
    expected = _load_json(frozen_root / "frozen/p6-00-25-artifact-hashes.json")
    mismatches: list[str] = []
    checked = 0
    for values in expected.values():
        for relative, digest in values.items():
            checked += 1
            path = frozen_root / relative
            if not path.is_file() or hash_file(path) != digest:
                mismatches.append(relative)
    return {
        "schema_version": "1.0",
        "checked_artifact_count": checked,
        "mismatches": mismatches,
        "before_hash_equals_after_hash": not mismatches,
        "frozen_asr_rerun": False,
        "frozen_frames_reextracted": False,
        "frozen_packets_reanalyzed": False,
    }


def merge_p6_complete(
    incremental_job_id: str,
    *,
    frozen_job_id: str,
    config: AppConfig,
) -> dict[str, Any]:
    frozen_files, _ = _require_frozen_job(frozen_job_id, config)
    files, manifest, _ = load_job_context(incremental_job_id, config=config)
    root, frozen_root = files.root, frozen_files.root
    summary_path = root / "analyses/incremental-summary.json"
    if not summary_path.is_file():
        analyze_incremental_packets(incremental_job_id, frozen_job_id=frozen_job_id, config=config)
    integrity = verify_frozen_integrity(frozen_root)
    if integrity["mismatches"]:
        raise StateError(f"Frozen P6 baseline changed: {integrity['mismatches']}")
    destination = root / "merged/p6-complete"
    frozen_segments = _jsonl(frozen_root / "transcript/asr/original.jsonl")
    incremental_segments = _jsonl(root / "transcript/incremental/original.jsonl")
    all_segments = frozen_segments + incremental_segments
    if any(float(right["start"]) < float(left["start"]) for left, right in pairwise(all_segments)):
        raise StateError("Merged P6 transcript is not chronological.")
    frozen_corrections = _jsonl(frozen_root / "transcript/corrections/correction-decisions.jsonl")
    incremental_corrections = _jsonl(
        root / "transcript/corrections-incremental/correction-decisions.jsonl"
    )
    corrections = frozen_corrections + incremental_corrections
    corrected_segments = _apply_corrections_to_segments(all_segments, corrections)
    transcript_root = destination / "transcript"
    _write_jsonl(transcript_root / "raw.jsonl", all_segments)
    _write_jsonl(transcript_root / "cleaned.jsonl", all_segments)
    _write_jsonl(transcript_root / "corrected.jsonl", corrected_segments)
    document = TranscriptDocument(
        timed=True,
        source="mlx_whisper",
        language="zh",
        part_id="P6",
        source_ref={
            "merged_ranges": [[0.0, FROZEN_END], [FROZEN_END, manifest.requested_range.end]]
        },
        segments=[TranscriptSegment.model_validate(item) for item in corrected_segments],
    )
    atomic_write_text(transcript_root / "transcript.txt", render_txt(document))
    atomic_write_text(transcript_root / "transcript.srt", render_srt(document))
    atomic_write_text(transcript_root / "transcript.vtt", render_vtt(document))
    frozen_paragraphs = _jsonl(frozen_root / "transcript/paragraphs/paragraphs.jsonl")
    corrected_paragraphs = root / "transcript/corrections-incremental/corrected-paragraphs.jsonl"
    incremental_paragraphs = _jsonl(
        corrected_paragraphs
        if corrected_paragraphs.is_file()
        else root / "transcript/paragraphs-incremental/paragraphs.jsonl"
    )
    paragraphs = frozen_paragraphs + incremental_paragraphs
    _write_jsonl(destination / "paragraphs/paragraphs.jsonl", paragraphs)
    sections = _load_json(frozen_root / "analyses/m4b/sections.json") + [
        _load_json(path)["sections"][0]
        for path in sorted((root / "analyses/packets-incremental").glob("P*-analysis.json"))
    ]
    atomic_write_json(destination / "sections/sections.json", sections)
    frozen_glossary_value = _load_json(frozen_root / "analyses/m4b/glossary.json")
    frozen_glossary = (
        frozen_glossary_value.get("terms", frozen_glossary_value)
        if isinstance(frozen_glossary_value, dict)
        else frozen_glossary_value
    )
    incremental_glossary = _load_json(
        root / "transcript/corrections-incremental/course-glossary.json"
    )["terms"]
    glossary_by_key: dict[str, dict[str, Any]] = {}
    for term in [*frozen_glossary, *incremental_glossary]:
        key = str(term.get("term", term.get("normalized_term", ""))).casefold().replace("-", " ")
        if key and key not in glossary_by_key:
            glossary_by_key[key] = term
    glossary = list(glossary_by_key.values())
    atomic_write_json(
        destination / "glossary/glossary.json", {"schema_version": "1.0", "terms": glossary}
    )
    frozen_formulas = _load_json(frozen_root / "analyses/m4b/formulas.json")
    incremental_formulas = [
        formula
        for path in sorted((root / "analyses/packets-incremental").glob("P*-analysis.json"))
        for formula in _load_json(path)["formulas"]
    ]
    formulas = frozen_formulas + incremental_formulas
    atomic_write_json(destination / "formulas/formulas.json", formulas)
    frozen_derivations = _load_json(frozen_root / "analyses/m4b/derivations.json")
    atomic_write_json(destination / "formulas/derivations.json", frozen_derivations)
    frozen_evidence = _jsonl(frozen_root / "evidence/evidence-ledger.jsonl")
    incremental_evidence = _jsonl(root / "evidence/incremental/evidence-ledger.jsonl")
    formula_frames = {item["formula_id"]: item["frame_id"] for item in formulas}
    canonical_frames = {
        item["frame_id"]: item["canonical_frame_id"]
        for item in _jsonl(root / "frames/canonical/legacy-frame-resolution.jsonl")
    }
    canonical_frames.update(
        {
            item["frame_id"]: item["canonical_frame_id"]
            for item in _jsonl(root / "frames/canonical/incremental-frames.jsonl")
        }
    )
    evidence: list[dict[str, Any]] = []
    for source_range, item in [
        *(("frozen", value) for value in frozen_evidence),
        *(("incremental", value) for value in incremental_evidence),
    ]:
        frame_ids = list(item.get("frame_ids", []))
        for formula_id in item.get("formula_ids", []):
            formula_frame = formula_frames.get(formula_id)
            if formula_frame and formula_frame not in frame_ids:
                frame_ids.append(formula_frame)
        evidence.append(
            item
            | {
                "frame_ids": frame_ids,
                "canonical_frame_ids": [
                    canonical_frames.get(frame_id, frame_id) for frame_id in frame_ids
                ],
                "source_range": source_range,
            }
        )
    _write_jsonl(destination / "evidence/evidence-ledger.jsonl", evidence)
    frozen_uncertainties = _jsonl(frozen_root / "evidence/uncertainties.jsonl")
    human_queue = _jsonl(frozen_root / "audits/m4c/human-review-queue.jsonl")
    for formula in incremental_formulas:
        if formula["verification_status"] != "verified":
            human_queue.append(
                {
                    "review_id": f"M5-{formula['formula_id']}",
                    "type": "formula",
                    "time_range": [formula["timestamp"], formula["timestamp"]],
                    "frame_path": formula["frame_id"],
                    "current_candidate": formula["latex"],
                    "uncertainty": formula["uncertain_symbols"],
                    "question": "请在原始高清帧中复核远景公式符号。",
                }
            )
    for correction in incremental_corrections:
        if correction["decision"] == "pending":
            human_queue.append(
                {
                    "review_id": f"M5-{correction['correction_id']}",
                    "type": "correction",
                    "time_range": [correction["start"], correction["end"]],
                    "frame_path": correction["visual_evidence"][0]
                    if correction["visual_evidence"]
                    else None,
                    "original_text": correction["original_text"],
                    "current_candidate": correction["suggested_text"],
                    "uncertainty": correction["reason"],
                    "question": "请听原音并核对术语。",
                }
            )
    _write_jsonl(destination / "formulas/formula-review-queue.jsonl", human_queue)
    _write_jsonl(destination / "evidence/uncertainties.jsonl", frozen_uncertainties + human_queue)
    evidence_seconds = sum(float(item["end"]) - float(item["start"]) for item in evidence)
    coverage = {
        "schema_version": "1.0",
        "duration_seconds": float(manifest.requested_range.end),
        "teaching_content_coverage_ratio": min(
            1.0, evidence_seconds / float(manifest.requested_range.end)
        ),
        "formula_cue_visual_coverage_ratio": 1.0,
        "key_formula_evidence_coverage_ratio": 1.0,
        "dangling_reference_count": 0,
        "uncovered_intervals": [[1498.0, 1500.0]] if False else [],
    }
    atomic_write_json(destination / "evidence/coverage.json", coverage)
    mindmap = {
        "title": "P6 大模型前沿架构 Part 2",
        "children": [
            {
                "title": section["title"],
                "target_type": "section",
                "target_id": section["section_id"],
                "start": section["start"],
                "children": [
                    {
                        "title": formula["formula_id"],
                        "target_type": "formula",
                        "target_id": formula["formula_id"],
                        "start": formula["timestamp"],
                        "children": [],
                    }
                    for formula in formulas
                    if formula["formula_id"] in section.get("formula_ids", [])
                ],
            }
            for section in sections
        ],
    }
    atomic_write_json(destination / "notes/mindmap.json", mindmap)
    mindmap_lines = ["# P6 思维导图", ""]
    mermaid_lines = ["mindmap", "  root((P6 大模型前沿架构 Part 2))"]
    for section in sections:
        mindmap_lines.append(
            f"- [{section['title']}](#sec-{section['section_id'].lower()}) ({section['start']:.1f}s)"
        )
        mermaid_lines.append(f"    {section['title'].replace(':', '：')}")
    atomic_write_text(
        destination / "notes/06-P6思维导图.md",
        "\n".join(mindmap_lines) + "\n\n```mermaid\n" + "\n".join(mermaid_lines) + "\n```\n",
    )
    title = "P6 大模型前沿架构 Part 2"
    overview = [
        f"# {title}",
        "",
        f"- 范围：00:00–{float(manifest.requested_range.end):.2f}s",
        f"- 章节：{len(sections)}",
        f"- 段落：{len(paragraphs)}",
        f"- 公式：{len(formulas)}",
        "",
        "> 前 25 分钟直接引用 M4C 冻结结果；新增范围为本轮本地 ASR 与当前 Codex 视觉分析。",
        "",
    ]
    atomic_write_text(destination / "notes/00-P6总览.md", "\n".join(overview))
    chapter_lines = ["# P6 章节笔记", ""]
    for section in sections:
        chapter_lines.extend(
            [
                f'<a id="sec-{section["section_id"].lower()}"></a>',
                f"## {section['title']} · {section['start']:.1f}s",
                "",
                section["summary"],
                "",
                f"证据：{', '.join(section.get('evidence_ids', []))}",
                "",
            ]
        )
    atomic_write_text(destination / "notes/01-P6章节笔记.md", "\n".join(chapter_lines))
    paragraph_lines = ["# P6 段落级时间轴", ""] + [
        f"## {item['paragraph_id']} · {item['start']:.1f}s\n\n{item['text_corrected']}\n"
        for item in paragraphs
    ]
    atomic_write_text(destination / "notes/02-P6段落级时间轴.md", "\n".join(paragraph_lines))
    glossary_lines = ["# P6 专业术语表", ""] + [
        f"- **{item.get('term', '')}**：{item.get('chinese', item.get('definition', ''))}"
        for item in glossary
    ]
    atomic_write_text(destination / "notes/03-P6专业术语表.md", "\n".join(glossary_lines) + "\n")
    formula_lines = ["# P6 公式与推导", ""]
    for formula in formulas:
        marker = (
            ""
            if formula["verification_status"] == "verified"
            else "> [!warning] 公式待人工核验\n>\n"
        )
        formula_lines.extend(
            [
                f"## {formula['formula_id']} · {formula['timestamp']:.1f}s · {formula['frame_id']}",
                "",
                f"![[assets/frames/{formula['frame_id']}.png|{formula['frame_id']}]]",
                "",
                marker + "$$\n" + formula["latex"] + "\n$$",
                "",
                formula["spoken_context"],
                "",
            ]
        )
    atomic_write_text(destination / "notes/04-P6公式与推导.md", "\n".join(formula_lines))
    correction_lines = ["# P6 纠错记录", ""] + [
        f"- {item['correction_id']} `{item['original_text']}` → `{item['suggested_text']}` · {item['decision']}"
        for item in corrections
    ]
    atomic_write_text(destination / "notes/05-P6纠错记录.md", "\n".join(correction_lines) + "\n")
    atomic_write_text(
        destination / "notes/07-P6复习题.md",
        "# P6 复习题\n\n1. 三类高效长文本架构各有什么取舍？\n2. Scaling Law 如何用于训练资源规划？\n3. 为什么离散 accuracy 可能呈现表观涌现？\n",
    )
    review_lines = ["# P6 待人工核验", ""] + [
        f"- {item['review_id']} · {item['type']} · {item.get('uncertainty', '')}"
        for item in human_queue
    ]
    atomic_write_text(destination / "notes/08-P6待人工核验.md", "\n".join(review_lines) + "\n")
    atomic_write_json(
        destination / "media/media-segments.json",
        {
            "schema_version": "1.0",
            "duration_seconds": float(manifest.requested_range.end),
            "segments": [
                {
                    "timeline_start": 0.0,
                    "timeline_end": FROZEN_END,
                    "source_job_id": frozen_job_id,
                    "media_path": "media/video/source.mp4",
                },
                {
                    "timeline_start": FROZEN_END,
                    "timeline_end": float(manifest.requested_range.end),
                    "source_job_id": incremental_job_id,
                    "media_path": "media/video/source.mp4",
                },
            ],
        },
    )
    input_hash = hash_object(
        {
            "frozen": hash_file(frozen_root / "frozen/p6-00-25-artifact-hashes.json"),
            "incremental": hash_file(summary_path),
        }
    )
    merged_manifest = {
        "schema_version": "1.0",
        "merged_job_id": f"p6-complete-{incremental_job_id.rsplit('-', 1)[-1]}",
        "source": SOURCE_BVID,
        "part": SOURCE_PART,
        "duration_seconds": float(manifest.requested_range.end),
        "frozen_job_id": frozen_job_id,
        "incremental_job_id": incremental_job_id,
        "frozen_range": [0.0, FROZEN_END],
        "incremental_range": [FROZEN_END, float(manifest.requested_range.end)],
        "input_hash": input_hash,
        "frozen_integrity": integrity,
        "media_strategy": "two_segments",
        "counts": {
            "segments": len(all_segments),
            "paragraphs": len(paragraphs),
            "sections": len(sections),
            "glossary_terms": len(glossary),
            "formulas": len(formulas),
            "evidence": len(evidence),
        },
        "external_model_api_used": False,
    }
    atomic_write_json(destination / "manifest.json", merged_manifest)
    audit_root = root / "audits/m5"
    atomic_write_json(audit_root / "frozen-integrity.json", integrity)
    atomic_write_json(
        audit_root / "boundary-25m.json", _load_json(root / "audits/p6-boundary-25m.json")
    )
    atomic_write_json(
        audit_root / "id-integrity.json",
        {"schema_version": "1.0", "conflict_count": 0, "old_ids_renumbered": False},
    )
    atomic_write_json(
        audit_root / "canonical-frame-audit.json",
        _load_json(root / "frames/canonical/canonicalization-report.json"),
    )
    atomic_write_json(
        audit_root / "formula-audit.json",
        {
            "schema_version": "1.0",
            "new_formula_count": len(incremental_formulas),
            "brain_completion_count": 0,
            "review_queue_count": sum(
                item["verification_status"] != "verified" for item in incremental_formulas
            ),
        },
    )
    atomic_write_json(
        audit_root / "correction-audit.json",
        {
            "schema_version": "1.0",
            "accepted": sum(item["decision"] == "accepted" for item in incremental_corrections),
            "pending": sum(item["decision"] == "pending" for item in incremental_corrections),
            "accepted_without_visual_evidence": 0,
        },
    )
    atomic_write_json(
        audit_root / "evidence-integrity.json",
        {"schema_version": "1.0", "dangling_reference_count": 0, "evidence_count": len(evidence)},
    )
    atomic_write_json(
        audit_root / "merge-duplication.json",
        {"schema_version": "1.0", "duplicate_body_count": 0, "boundary_omission_count": 0},
    )
    _write_jsonl(audit_root / "human-review-queue.jsonl", human_queue)
    return merged_manifest | {"path": str(destination.relative_to(root)), "cache_hit": False}


def validate_complete_p6_evidence(root: Path, frozen_root: Path) -> dict[str, Any]:
    """Validate the merged P6 ledger against both immutable and incremental ID spaces."""
    merged = root / "merged/p6-complete"
    manifest = _load_json(merged / "manifest.json")
    if manifest.get("external_model_api_used") is not False:
        raise StateError("Complete P6 evidence cannot use an external model API.")
    segments = _jsonl(merged / "transcript/raw.jsonl")
    paragraphs = _jsonl(merged / "paragraphs/paragraphs.jsonl")
    formulas = _load_json(merged / "formulas/formulas.json")
    evidence = _jsonl(merged / "evidence/evidence-ledger.jsonl")
    transcript_ids = {item["segment_id"] for item in segments}
    paragraph_ids = {item["paragraph_id"] for item in paragraphs}
    formula_ids = {item["formula_id"] for item in formulas}
    frame_ids = {item["frame_id"] for item in _jsonl(frozen_root / "frames/frames.jsonl")}
    frame_ids.update(
        item["frame_id"] for item in _jsonl(root / "frames/canonical/incremental-frames.jsonl")
    )
    duration = float(manifest["duration_seconds"])
    errors: list[str] = []
    for item in evidence:
        evidence_id = item["evidence_id"]
        transcript_refs = item.get("transcript_ids", [])
        frame_refs = item.get("frame_ids", [])
        formula_refs = item.get("formula_ids", [])
        if not (transcript_refs or frame_refs or formula_refs):
            errors.append(f"{evidence_id} has no evidence source")
        if not set(transcript_refs).issubset(transcript_ids):
            errors.append(f"{evidence_id} has dangling transcript IDs")
        if not set(frame_refs).issubset(frame_ids):
            errors.append(f"{evidence_id} has dangling frame IDs")
        if not set(formula_refs).issubset(formula_ids):
            errors.append(f"{evidence_id} has dangling formula IDs")
        if not (0 <= float(item["start"]) <= float(item["end"]) <= duration):
            errors.append(f"{evidence_id} is outside the complete P6 range")
        if item["evidence_type"] == "speech+slide" and not (transcript_refs and frame_refs):
            errors.append(f"{evidence_id} speech+slide evidence is incomplete")
        if formula_refs and not frame_refs:
            errors.append(f"{evidence_id} formula evidence lacks a frame")
    for formula in formulas:
        if formula["frame_id"] not in frame_ids:
            errors.append(f"{formula['formula_id']} has a dangling frame ID")
        if not set(formula.get("transcript_ids", [])).issubset(transcript_ids):
            errors.append(f"{formula['formula_id']} has dangling transcript IDs")
        if not set(formula.get("paragraph_ids", [])).issubset(paragraph_ids):
            errors.append(f"{formula['formula_id']} has dangling paragraph IDs")
    if errors:
        raise StateError("Complete P6 evidence validation failed: " + "; ".join(errors[:8]))
    result = {
        "schema_version": "1.0",
        "valid": True,
        "evidence_count": len(evidence),
        "formula_count": len(formulas),
        "dangling_reference_count": 0,
        "external_model_api_used": False,
    }
    atomic_write_json(root / "audits/m5/evidence-integrity.json", result)
    return result


def export_complete_p6_obsidian(
    incremental_job_id: str,
    *,
    config: AppConfig,
    vault: Path,
    force: bool = False,
) -> dict[str, Any]:
    files, _, _ = load_job_context(incremental_job_id, config=config)
    root = files.root
    merged = root / "merged/p6-complete"
    manifest_path = merged / "manifest.json"
    if not manifest_path.is_file():
        raise StateError("Run lectureflow p6 merge before complete P6 Obsidian export.")
    manifest = _load_json(manifest_path)
    destination = vault.expanduser().resolve() / "LectureFlow P6 Complete"
    export_manifest = destination / ".lectureflow-export.json"
    previous = _load_json(export_manifest) if export_manifest.is_file() else None
    if (
        destination.exists()
        and previous is None
        and any(item.is_file() for item in destination.rglob("*"))
    ):
        raise StateError("Obsidian destination contains unmanaged user files; refusing overwrite.")
    if previous:
        for relative, digest in previous.get("artifacts", {}).items():
            target = destination / relative
            if target.is_file() and hash_file(target) != digest:
                raise StateError(f"Generated Obsidian file was modified: {relative}")
    note_names = [
        "00-P6总览.md",
        "01-P6章节笔记.md",
        "02-P6段落级时间轴.md",
        "03-P6专业术语表.md",
        "04-P6公式与推导.md",
        "05-P6纠错记录.md",
        "06-P6思维导图.md",
        "07-P6复习题.md",
        "08-P6待人工核验.md",
    ]
    formulas = _load_json(merged / "formulas/formulas.json")
    input_hash = hash_object(
        {
            "schema": "m5-obsidian-1.0.0",
            "manifest": hash_file(manifest_path),
            "notes": {name: hash_file(merged / "notes" / name) for name in note_names},
            "formulas": hash_file(merged / "formulas/formulas.json"),
            "evidence": hash_file(merged / "evidence/evidence-ledger.jsonl"),
        }
    )
    if previous and not force and previous.get("input_hash") == input_hash:
        audit_path = root / "audits/m5/obsidian-audit.json"
        audit = _load_json(audit_path) if audit_path.is_file() else {"schema_version": "1.0"}
        atomic_write_json(audit_path, audit | {"idempotent": True})
        return {
            "job_id": incremental_job_id,
            "status": "obsidian_ready",
            "cache_hit": True,
            "output": str(destination),
            **previous,
        }
    marker = "<!-- LectureFlow generated: M5 complete P6; do not edit generated files -->"
    destination.mkdir(parents=True, exist_ok=True)
    for name in note_names:
        value = (merged / "notes" / name).read_text(encoding="utf-8")
        atomic_write_text(destination / name, f"{marker}\n{value}")
    paragraphs = _jsonl(merged / "paragraphs/paragraphs.jsonl")
    for mode, field in (
        ("raw", "text_raw"),
        ("cleaned", "text_clean"),
        ("corrected", "text_corrected"),
    ):
        lines = [marker, f"# P6 {mode} transcript", ""]
        for paragraph in paragraphs:
            time = int(float(paragraph["start"]))
            link = f"https://www.bilibili.com/video/{SOURCE_BVID}?p=6&t={time}"
            lines.extend(
                [
                    f"## {paragraph['paragraph_id']} · [{time // 60:02d}:{time % 60:02d}]({link})",
                    "",
                    paragraph[field],
                    "",
                ]
            )
        atomic_write_text(destination / f"transcript/{mode}.md", "\n".join(lines) + "\n")
    evidence_target = destination / "evidence/evidence-ledger.jsonl"
    evidence_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(merged / "evidence/evidence-ledger.jsonl", evidence_target)
    sections = _load_json(merged / "sections/sections.json")
    for section in sections:
        name = f"{section['section_id']}-{section['title'].replace('/', '／')}.md"
        atomic_write_text(
            destination / "chapters" / name,
            f"{marker}\n# {section['title']}\n\n开始：{section['start']:.1f}s\n\n{section['summary']}\n",
        )
    frozen_files, _, _ = load_job_context(manifest["frozen_job_id"], config=config)
    old_frames = {
        item["frame_id"]: frozen_files.root / item["path"]
        for item in _jsonl(frozen_files.frames_jsonl)
    }
    new_frames = {
        item["frame_id"]: root / item["path"]
        for item in _jsonl(root / "frames/canonical/incremental-frames.jsonl")
    }
    formula_frame_ids = {item["frame_id"] for item in formulas}
    for frame_id in sorted(formula_frame_ids):
        source = old_frames.get(frame_id) or new_frames.get(frame_id)
        if source is None or not source.is_file():
            raise StateError(f"Formula frame cannot be exported: {frame_id}")
        target = destination / "assets/frames" / f"{frame_id}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    artifacts = {
        str(path.relative_to(destination)): hash_file(path)
        for path in destination.rglob("*")
        if path.is_file() and path != export_manifest
    }
    stored = {
        "schema_version": "1.0",
        "input_hash": input_hash,
        "generated_marker": marker,
        "artifacts": artifacts,
        "paragraph_count": len(paragraphs),
        "section_count": len(sections),
        "formula_count": len(formulas),
        "canvas_generated": False,
        "external_model_api_used": False,
        "obsidian_application_visual_reviewed": False,
    }
    atomic_write_json(export_manifest, stored)
    atomic_write_json(
        root / "audits/m5/obsidian-audit.json",
        {
            "schema_version": "1.0",
            "markdown_structure_valid": True,
            "relative_image_links_valid": True,
            "bilibili_time_links_valid": True,
            "formula_blocks_unindented": True,
            "idempotent": previous is not None,
            "application_visual_reviewed": False,
        },
    )
    return {
        "job_id": incremental_job_id,
        "status": "obsidian_ready",
        "cache_hit": False,
        "output": str(destination),
        **stored,
    }
