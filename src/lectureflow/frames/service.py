from __future__ import annotations

import json
import math
import re
import shutil
from collections import Counter
from contextlib import suppress
from itertools import pairwise
from pathlib import Path
from typing import Any

from lectureflow.atomic import atomic_write_json, atomic_write_text
from lectureflow.config import AppConfig, FrameConfig
from lectureflow.constants import PIPELINE_VERSION
from lectureflow.errors import FrameError, StateError
from lectureflow.frames.contact_sheet import build_contact_sheets
from lectureflow.frames.image import compare_images, hamming_distance, inspect_image
from lectureflow.hashing import hash_file, hash_object
from lectureflow.media.service import acquire_media, resolve_media_path
from lectureflow.paths import JobFiles
from lectureflow.pipeline import load_job_context
from lectureflow.process import run_command
from lectureflow.schemas.frames import FrameCandidate, FrameRecord, FrameRunResult, MediaManifest
from lectureflow.schemas.state import StageStatus
from lectureflow.state import (
    JobLock,
    finish_stage_failure,
    finish_stage_success,
    invalidate_from,
    load_state,
    mark_interrupted_running_stages,
    record_transition,
    restore_stage_cache_hit,
    save_state,
    start_stage,
    utc_now,
)

FRAME_STAGE_SCHEMA = "frames-1.3.0"
_PTS = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")
_SCORE = re.compile(r"lavfi\.scene_score=([0-9]+(?:\.[0-9]+)?)")
_BLACK = re.compile(r"black_start:([0-9]+(?:\.[0-9]+)?)\s+black_end:([0-9]+(?:\.[0-9]+)?)")
_FORMULA_CUE_FRAGMENTS = (
    "公式",
    "式子",
    "矩阵",
    "向量",
    "求和",
    "梯度",
    "概率",
    "期望",
    "定义",
    "推导",
)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FrameError(f"Cannot read frame input {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise FrameError(f"Expected a JSON object in {path}.")
    return value


def _write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    lines = [
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for value in values
    ]
    atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))


def _transcript_files(root: Path) -> list[Path]:
    result: list[Path] = []
    direct = root / "transcript/original.json"
    if direct.is_file():
        result.append(direct)
    result.extend(sorted((root / "transcript/parts").glob("*/original.json")))
    asr = root / "transcript/asr/original.jsonl"
    if asr.is_file():
        result.append(asr)
    return result


def _transcript_segments(path: Path) -> list[dict[str, Any]]:
    if path.suffix == ".jsonl":
        try:
            return [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, json.JSONDecodeError) as exc:
            raise FrameError(f"Cannot read transcript JSONL {path}: {exc}") from exc
    return list(_read_json(path).get("segments", []))


def _transcript_hash(root: Path) -> str | None:
    files = _transcript_files(root)
    return (
        hash_object([(str(path.relative_to(root)), hash_file(path)) for path in files])
        if files
        else None
    )


def _cue_matches(
    root: Path, config: FrameConfig, *, start: float, end: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    matches: list[dict[str, Any]] = []
    moments: list[dict[str, Any]] = []
    lowered = [(term, term.casefold()) for term in config.subtitle_cues]
    for path in _transcript_files(root):
        for segment in _transcript_segments(path):
            if not isinstance(segment, dict):
                continue
            text = str(segment.get("text_clean") or segment.get("text_raw") or "")
            terms = [term for term, folded in lowered if folded in text.casefold()]
            if not terms:
                continue
            timestamp = float(segment.get("start", 0.0))
            match = {
                "segment_id": segment.get("segment_id"),
                "timestamp": timestamp,
                "terms": terms,
                "text": text,
                "source": str(path.relative_to(root)),
            }
            matches.append(match)
            for offset in config.cue_offsets_seconds:
                proposed = min(end - 0.01, max(start, timestamp + offset))
                if start <= proposed < end:
                    moments.append(
                        {
                            "timestamp": proposed,
                            "reason": ["subtitle_cue"],
                            "cue_terms": terms,
                            "nearby_segment_ids": [str(segment.get("segment_id"))],
                            "cue_offsets": [offset],
                            "cue_offset_types": [
                                "before" if offset < 0 else "after" if offset > 0 else "at"
                            ],
                        }
                    )
    return matches, moments


def _progressive_protection(
    candidate: dict[str, Any],
    canonical: dict[str, Any],
    *,
    config: FrameConfig,
    metrics: dict[str, float],
) -> list[str]:
    reasons: list[str] = []
    protected_terms = tuple(term.casefold() for term in config.protected_cue_terms)
    if any(
        protected in term.casefold()
        for term in candidate.get("cue_terms", [])
        for protected in protected_terms
    ):
        reasons.append("protected_subtitle_cue")
    if abs(float(candidate["timestamp"]) - float(canonical["timestamp"])) >= (
        config.progressive_slide_protection_seconds
    ):
        reasons.append("time_interval")
    if metrics["local_change_ratio"] >= config.local_change_ratio_threshold:
        reasons.append("local_pixel_change")
    if metrics["edge_change_ratio"] >= config.edge_change_ratio_threshold:
        reasons.append("edge_change")
    if metrics["high_contrast_change_ratio"] >= (config.high_contrast_change_ratio_threshold):
        reasons.append("high_contrast_text_change")
    if (candidate.get("scene_score") or 0.0) >= 0.45:
        reasons.append("high_scene_score")
    return reasons


def _periodic(start: float, end: float, interval: float) -> list[dict[str, Any]]:
    moments: list[dict[str, Any]] = []
    current = start
    while current < end:
        moments.append({"timestamp": current, "reason": ["periodic"]})
        current += interval
    tail = max(start, end - 0.05)
    if not moments or tail - moments[-1]["timestamp"] > max(1.0, interval / 3):
        moments.append({"timestamp": tail, "reason": ["periodic"]})
    return moments


def _scene_frames(
    media_path: Path,
    output_dir: Path,
    *,
    physical_start: float,
    duration: float,
    logical_start: float,
    threshold: float,
) -> list[dict[str, Any]]:
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise FrameError("FFmpeg is required. Run 'lectureflow doctor'.")
    output_dir.mkdir(parents=True, exist_ok=True)
    pattern = output_dir / "scene-%06d.png"
    filter_value = f"select='gt(scene,{threshold:g})',metadata=mode=print:file=-"
    result = run_command(
        (
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{physical_start:.6f}",
            "-i",
            media_path,
            "-t",
            f"{duration:.6f}",
            "-vf",
            filter_value,
            "-vsync",
            "vfr",
            "-y",
            pattern,
        ),
        timeout=max(180, duration * 3),
        max_output_chars=4_000_000,
    )
    timestamps = [float(value) for value in _PTS.findall(result.stdout)]
    scores = [float(value) for value in _SCORE.findall(result.stdout)]
    paths = sorted(output_dir.glob("scene-*.png"))
    if len(timestamps) != len(scores):
        raise FrameError("FFmpeg emitted incomplete scene timestamp/score metadata pairs.")
    # FFmpeg can report a selected frame exactly at the -t boundary while the output muxer
    # correctly omits it. Match only metadata inside the half-open requested interval.
    metadata = [
        (timestamp, score)
        for timestamp, score in zip(timestamps, scores, strict=True)
        if 0 <= timestamp < duration - 1e-6
    ]
    if len(metadata) != len(paths):
        raise FrameError(
            "FFmpeg scene metadata/score count does not match extracted PNG count; refusing "
            "to invent timestamps or scores from filenames."
        )
    moments: list[dict[str, Any]] = []
    for index, path in enumerate(paths):
        relative_pts, scene_score = metadata[index]
        moments.append(
            {
                "timestamp": logical_start + relative_pts,
                "reason": ["scene_change"],
                "scene_score": scene_score,
                "prepared_path": path,
            }
        )
    return moments


def _black_probes(
    media_path: Path,
    *,
    physical_start: float,
    duration: float,
    logical_start: float,
) -> list[dict[str, Any]]:
    """Sample the midpoint of black intervals so quality filtering is itself audited."""
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise FrameError("FFmpeg is required. Run 'lectureflow doctor'.")
    result = run_command(
        (
            executable,
            "-hide_banner",
            "-loglevel",
            "info",
            "-ss",
            f"{physical_start:.6f}",
            "-i",
            media_path,
            "-t",
            f"{duration:.6f}",
            "-vf",
            "blackdetect=d=0.5:pix_th=0.10",
            "-an",
            "-f",
            "null",
            "-",
        ),
        timeout=max(180, duration * 3),
        max_output_chars=4_000_000,
    )
    return [
        {
            "timestamp": logical_start + (float(left) + float(right)) / 2,
            "reason": ["scene_change"],
            "quality_probe": "black_interval",
        }
        for left, right in _BLACK.findall(result.stderr)
    ]


def _merge_moments(
    values: list[dict[str, Any]], *, tolerance: float = 0.25
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for value in sorted(values, key=lambda item: float(item["timestamp"])):
        if merged and abs(float(merged[-1]["timestamp"]) - float(value["timestamp"])) <= tolerance:
            target = merged[-1]
            for key in (
                "reason",
                "cue_terms",
                "nearby_segment_ids",
                "cue_offsets",
                "cue_offset_types",
            ):
                target[key] = sorted(set(target.get(key, [])) | set(value.get(key, [])))
            if value.get("scene_score") is not None:
                target["scene_score"] = max(target.get("scene_score") or 0, value["scene_score"])
            if value.get("prepared_path") and not target.get("prepared_path"):
                target["prepared_path"] = value["prepared_path"]
        else:
            merged.append(dict(value))
    return merged


def _priority(value: dict[str, Any]) -> tuple[int, float, float, float]:
    reasons = value.get("reason", [])
    formula_cue = any(
        fragment in term
        for term in value.get("cue_terms", [])
        for fragment in _FORMULA_CUE_FRAGMENTS
    )
    rank = (
        4
        if formula_cue
        else 3
        if "subtitle_cue" in reasons
        else 2
        if "scene_change" in reasons
        else 1
    )
    offsets = value.get("cue_offsets", [])
    cue_closeness = -min((abs(float(offset)) for offset in offsets), default=math.inf)
    return rank, float(value.get("scene_score") or 0), cue_closeness, -float(value["timestamp"])


def _stratified_final_selection(
    frames: list[dict[str, Any]], *, start: float, end: float, maximum: int
) -> list[dict[str, Any]]:
    if len(frames) <= maximum:
        return list(frames)
    bucket_count = max(1, math.ceil((end - start) / 300.0))
    base_budget = max(1, maximum // bucket_count)
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    for index in range(bucket_count):
        left = start + index * 300.0
        right = min(end, left + 300.0)
        bucket = [
            item
            for item in frames
            if left <= float(item["timestamp"]) < right
            or (index == bucket_count - 1 and float(item["timestamp"]) == end)
        ]
        for item in sorted(
            bucket, key=lambda value: (_priority(value), value["sharpness"]), reverse=True
        )[:base_budget]:
            selected.append(item)
            selected_ids.add(item["candidate_id"])
    return selected


def _extract_one(
    media: Path,
    target: Path,
    *,
    physical_timestamp: float,
) -> None:
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise FrameError("FFmpeg is required. Run 'lectureflow doctor'.")
    target.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        (
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, physical_timestamp):.6f}",
            "-i",
            media,
            "-frames:v",
            "1",
            "-y",
            target,
        ),
        timeout=180,
    )


def _physical_timestamp(media: MediaManifest, logical: float) -> float:
    return logical if media.storage == "external_local" else logical - media.requested_start


def _quality_flags(metrics: dict[str, Any], config: FrameConfig) -> list[str]:
    flags: list[str] = []
    if metrics["black_ratio"] >= config.black_frame_threshold:
        flags.append("black")
    if metrics["mean_luma"] <= config.dark_luma_threshold:
        flags.append("extreme_dark")
    if metrics["luma_stddev"] <= config.low_information_stddev:
        flags.append("low_information")
    return flags


def _artifact_integrity(root: Path, payload: dict[str, Any]) -> tuple[bool, list[str]]:
    damaged: list[str] = []
    for item in payload.get("artifacts", []):
        path = root / str(item.get("path"))
        if not path.is_file() or hash_file(path) != item.get("sha256"):
            damaged.append(str(item.get("frame_id") or item.get("path")))
    return not damaged, damaged


def _repair_frames(
    files: JobFiles,
    media: MediaManifest,
    media_path: Path,
    damaged: list[str],
    config: FrameConfig,
) -> list[str]:
    records = [
        json.loads(line)
        for line in files.frames_jsonl.read_text(encoding="utf-8").splitlines()
        if line
    ]
    repaired: list[str] = []
    for record in records:
        if record["frame_id"] not in damaged and record["path"] not in damaged:
            continue
        target = files.root / record["path"]
        _extract_one(
            media_path, target, physical_timestamp=_physical_timestamp(media, record["timestamp"])
        )
        metrics = inspect_image(target)
        record.update(
            {
                "sha256": metrics["sha256"],
                "perceptual_hash": metrics["perceptual_hash"],
                "width": metrics["width"],
                "height": metrics["height"],
                "quality_flags": _quality_flags(metrics, config),
            }
        )
        FrameRecord.model_validate(record)
        repaired.append(record["frame_id"])
    if not repaired:
        raise FrameError("Frame artifacts are damaged outside the repairable final-frame set.")
    _write_jsonl(files.frames_jsonl, records)
    sheets = build_contact_sheets(
        files.root,
        records,
        columns=config.contact_sheet_columns,
        sheet_width=config.contact_sheet_width,
        max_frames=config.contact_sheet_max_frames,
    )
    manifest = _read_json(files.frames_manifest)
    preserved = [
        item
        for item in manifest.get("artifacts", [])
        if not item.get("frame_id")
        and not str(item.get("path", "")).startswith("frames/contact-sheets/")
        and item.get("path") != "frames/contact-sheet-manifest.json"
    ]
    for item in preserved:
        item["sha256"] = hash_file(files.root / item["path"])
    artifacts = [
        {"frame_id": item["frame_id"], "path": item["path"], "sha256": item["sha256"]}
        for item in records
    ]
    artifacts.extend({"path": item["path"], "sha256": item["sha256"]} for item in sheets)
    contact_manifest = files.root / "frames/contact-sheet-manifest.json"
    artifacts.append(
        {
            "path": str(contact_manifest.relative_to(files.root)),
            "sha256": hash_file(contact_manifest),
        }
    )
    artifacts.extend(preserved)
    manifest.update({"artifacts": artifacts, "generated_at": utc_now(), "repair": repaired})
    atomic_write_json(files.frames_manifest, manifest)
    return repaired


def extract_frames(
    job_id: str,
    *,
    config: AppConfig,
    force: bool = False,
) -> FrameRunResult:
    acquire_media(job_id, config=config)
    files, manifest, _ = load_job_context(job_id, config=config)
    media = MediaManifest.model_validate(_read_json(files.media_manifest))
    media_path = resolve_media_path(files, media)
    start = manifest.requested_range.start
    end = (
        manifest.requested_range.end
        or media.source_duration
        or (start + media.duration if media.storage == "job_local" else media.duration)
    )
    if end <= start:
        raise FrameError("Frame range is empty after media inspection.")
    config_payload = config.frames.model_dump(mode="json")
    config_hash = hash_object(config_payload)
    input_hash = hash_object(
        {
            "schema": FRAME_STAGE_SCHEMA,
            "media_manifest": hash_file(files.media_manifest),
            "transcript": _transcript_hash(files.root),
            "range": [start, end],
            "config": config_payload,
            "pipeline_version": PIPELINE_VERSION,
        }
    )
    with JobLock(files.lock):
        state = load_state(files.state)
        interrupted = mark_interrupted_running_stages(state)
        if interrupted:
            save_state(files.state, state)
            for name in interrupted:
                record_transition(files.events, state, "stage_interrupted", stage=name)
        artifact_payload: dict[str, Any] | None = None
        if files.frames_manifest.is_file():
            try:
                artifact_payload = _read_json(files.frames_manifest)
            except FrameError:
                artifact_payload = None
        cache_identity = bool(
            artifact_payload
            and artifact_payload.get("input_hash") == input_hash
            and artifact_payload.get("config_hash") == config_hash
        )
        if cache_identity and not force:
            valid, damaged = _artifact_integrity(files.root, artifact_payload or {})
            if valid:
                state_record = state.stages["frames_ready"]
                restored = restore_stage_cache_hit(
                    state,
                    "frames_ready",
                    input_hash=input_hash,
                    output_hash=hash_file(files.frames_manifest),
                )
                if restored:
                    save_state(files.state, state)
                if state_record.status == StageStatus.SUCCEEDED:
                    record_transition(files.events, state, "stage_cache_hit", stage="frames_ready")
                    return FrameRunResult.model_validate(
                        (artifact_payload or {})["result"] | {"cache_hit": True}
                    )
            elif all(item.startswith("F") for item in damaged):
                if state.stages["frames_ready"].status != StageStatus.PENDING:
                    invalidate_from(state, "frames_ready")
                attempt = start_stage(
                    state,
                    "frames_ready",
                    input_hash=input_hash,
                    config_hash=config_hash,
                    tool="ffmpeg+Pillow-repair",
                    tool_version=media.tool_versions.get("ffprobe"),
                )
                save_state(files.state, state)
                repaired = _repair_frames(files, media, media_path, damaged, config.frames)
                artifact_payload = _read_json(files.frames_manifest)
                result = FrameRunResult.model_validate(
                    artifact_payload["result"]
                    | {"cache_hit": False, "repaired_frame_ids": repaired}
                )
                finish_stage_success(
                    state, "frames_ready", attempt, output_hash=hash_file(files.frames_manifest)
                )
                save_state(files.state, state)
                record_transition(
                    files.events, state, "stage_repaired", stage="frames_ready", frame_ids=repaired
                )
                return result
        if state.stages["frames_ready"].status != StageStatus.PENDING:
            invalidate_from(state, "frames_ready")
        attempt = start_stage(
            state,
            "frames_ready",
            input_hash=input_hash,
            config_hash=config_hash,
            tool="ffmpeg+Pillow",
            tool_version=media.tool_versions.get("ffprobe"),
        )
        save_state(files.state, state)
        record_transition(files.events, state, "stage_started", stage="frames_ready")
        try:
            physical_start = start if media.storage == "external_local" else 0.0
            window_duration = end - start
            scene_dir = files.root / "frames/candidates/scene"
            scene = _scene_frames(
                media_path,
                scene_dir,
                physical_start=physical_start,
                duration=window_duration,
                logical_start=start,
                threshold=config.frames.scene_threshold,
            )
            black_probes = _black_probes(
                media_path,
                physical_start=physical_start,
                duration=window_duration,
                logical_start=start,
            )
            cue_matches, cues = _cue_matches(files.root, config.frames, start=start, end=end)
            periodic = _periodic(start, end, config.frames.periodic_interval_seconds)
            merged = _merge_moments(scene + black_probes + cues + periodic)
            budget_dropped = max(0, len(merged) - config.frames.max_candidate_frames)
            chosen = sorted(
                sorted(merged, key=_priority, reverse=True)[: config.frames.max_candidate_frames],
                key=lambda item: item["timestamp"],
            )
            candidates: list[dict[str, Any]] = []
            for index, item in enumerate(chosen, start=1):
                candidate_id = f"C{index:06d}"
                target = files.root / f"frames/candidates/{candidate_id}.png"
                prepared = item.get("prepared_path")
                if prepared and Path(prepared).is_file():
                    shutil.copyfile(Path(prepared), target)
                else:
                    _extract_one(
                        media_path,
                        target,
                        physical_timestamp=_physical_timestamp(media, float(item["timestamp"])),
                    )
                metrics = inspect_image(target)
                flags = _quality_flags(metrics, config.frames)
                value = FrameCandidate(
                    candidate_id=candidate_id,
                    timestamp=float(item["timestamp"]),
                    path=str(target.relative_to(files.root)),
                    reason=sorted(set(item.get("reason", []))),
                    scene_score=item.get("scene_score"),
                    cue_terms=sorted(set(item.get("cue_terms", []))),
                    nearby_segment_ids=sorted(set(item.get("nearby_segment_ids", []))),
                    cue_offsets=sorted(set(item.get("cue_offsets", []))),
                    cue_offset_types=sorted(set(item.get("cue_offset_types", []))),
                    quality_flags=flags,
                    **metrics,
                ).model_dump(mode="json")
                candidates.append(value)
            for temporary_scene in scene_dir.glob("scene-*.png"):
                temporary_scene.unlink()
            with suppress(OSError):
                scene_dir.rmdir()
            ranked = sorted(
                candidates,
                key=lambda item: (_priority(item), item["sharpness"]),
                reverse=True,
            )
            kept: list[dict[str, Any]] = []
            dedup_decisions: list[dict[str, Any]] = []
            for candidate in ranked:
                if (
                    "black" in candidate["quality_flags"]
                    or "extreme_dark" in candidate["quality_flags"]
                ):
                    dedup_decisions.append(
                        {
                            "candidate_id": candidate["candidate_id"],
                            "action": "quality_drop",
                            "flags": candidate["quality_flags"],
                        }
                    )
                    continue
                duplicate_options = [
                    (
                        other,
                        hamming_distance(candidate["perceptual_hash"], other["perceptual_hash"]),
                    )
                    for other in kept
                    if hamming_distance(candidate["perceptual_hash"], other["perceptual_hash"])
                    <= config.frames.duplicate_threshold
                ]
                duplicate = min(duplicate_options, key=lambda item: item[1], default=None)
                if duplicate:
                    canonical, distance = duplicate
                    change_metrics = compare_images(
                        files.root / candidate["path"], files.root / canonical["path"]
                    )
                    protections = _progressive_protection(
                        candidate,
                        canonical,
                        config=config.frames,
                        metrics=change_metrics,
                    )
                    if protections:
                        kept.append(candidate)
                        dedup_decisions.append(
                            {
                                "candidate_id": candidate["candidate_id"],
                                "action": "progressive_change_protected",
                                "canonical": canonical["candidate_id"],
                                "distance": distance,
                                **change_metrics,
                                "protection_reasons": protections,
                            }
                        )
                        continue
                    canonical["reason"] = sorted(
                        set(canonical["reason"]) | set(candidate["reason"])
                    )
                    canonical["cue_terms"] = sorted(
                        set(canonical["cue_terms"]) | set(candidate["cue_terms"])
                    )
                    canonical["nearby_segment_ids"] = sorted(
                        set(canonical["nearby_segment_ids"]) | set(candidate["nearby_segment_ids"])
                    )
                    dedup_decisions.append(
                        {
                            "candidate_id": candidate["candidate_id"],
                            "action": "duplicate_drop",
                            "canonical": canonical["candidate_id"],
                            "distance": distance,
                            **change_metrics,
                            "protection_reasons": [],
                            "reason": "dhash_and_local_change_below_thresholds",
                        }
                    )
                else:
                    kept.append(candidate)
            pre_final = len(kept)
            kept = sorted(kept, key=lambda item: item["timestamp"])
            if len(kept) > config.frames.max_final_frames:
                selection = _stratified_final_selection(
                    kept,
                    start=start,
                    end=end,
                    maximum=config.frames.max_final_frames,
                )
                selected_ids = {item["candidate_id"] for item in selection}
                for item in kept:
                    if item["candidate_id"] not in selected_ids:
                        dedup_decisions.append(
                            {"candidate_id": item["candidate_id"], "action": "final_budget_drop"}
                        )
                kept = sorted(selection, key=lambda item: item["timestamp"])
            records: list[dict[str, Any]] = []
            for index, item in enumerate(kept, start=1):
                frame_id = f"F{index:06d}"
                target = files.root / f"frames/originals/{frame_id}.png"
                shutil.copyfile(files.root / item["path"], target)
                value = FrameRecord(
                    frame_id=frame_id,
                    candidate_id=item["candidate_id"],
                    timestamp=item["timestamp"],
                    path=str(target.relative_to(files.root)),
                    reason=item["reason"],
                    scene_score=item.get("scene_score"),
                    cue_terms=item["cue_terms"],
                    nearby_segment_ids=item["nearby_segment_ids"],
                    width=item["width"],
                    height=item["height"],
                    sha256=hash_file(target),
                    perceptual_hash=item["perceptual_hash"],
                    quality_flags=item["quality_flags"],
                ).model_dump(mode="json")
                records.append(value)
            _write_jsonl(files.frame_candidates_jsonl, candidates)
            _write_jsonl(files.frames_jsonl, records)
            atomic_write_json(
                files.root / "frames/cue-matches.json",
                {"schema_version": "1.0.0", "matches": cue_matches},
            )
            protected_terms = tuple(term.casefold() for term in config.frames.protected_cue_terms)
            formula_matches = [
                item
                for item in cue_matches
                if any(
                    protected in term.casefold()
                    for term in item["terms"]
                    for protected in protected_terms
                )
            ]
            atomic_write_json(
                files.root / "frames/formula-cue-matches.json",
                {
                    "schema_version": "1.0",
                    "offsets_seconds": config.frames.cue_offsets_seconds,
                    "match_count": len(formula_matches),
                    "matches": formula_matches,
                },
            )
            atomic_write_json(
                files.root / "frames/frame-dedup-report.json",
                {
                    "schema_version": "1.0.0",
                    "threshold": config.frames.duplicate_threshold,
                    "decisions": dedup_decisions,
                },
            )
            progressive_decisions = [
                item
                for item in dedup_decisions
                if item["action"] in {"duplicate_drop", "progressive_change_protected"}
            ]
            atomic_write_json(
                files.root / "frames/progressive-slide-report.json",
                {
                    "schema_version": "1.0",
                    "thresholds": {
                        "dhash": config.frames.duplicate_threshold,
                        "time_seconds": config.frames.progressive_slide_protection_seconds,
                        "local_change_ratio": config.frames.local_change_ratio_threshold,
                        "edge_change_ratio": config.frames.edge_change_ratio_threshold,
                        "high_contrast_change_ratio": (
                            config.frames.high_contrast_change_ratio_threshold
                        ),
                    },
                    "protected_count": sum(
                        item["action"] == "progressive_change_protected"
                        for item in progressive_decisions
                    ),
                    "duplicate_count": sum(
                        item["action"] == "duplicate_drop" for item in progressive_decisions
                    ),
                    "decisions": progressive_decisions,
                },
            )
            quality_counts = Counter(flag for item in candidates for flag in item["quality_flags"])
            quality_summary = {
                name: quality_counts.get(name, 0)
                for name in ("black", "extreme_dark", "low_information")
            }
            atomic_write_json(
                files.root / "frames/frame-quality-report.json",
                {
                    "schema_version": "1.0.0",
                    "counts": quality_summary,
                    "candidates": [
                        {
                            "candidate_id": item["candidate_id"],
                            "flags": item["quality_flags"],
                            "black_ratio": item["black_ratio"],
                            "mean_luma": item["mean_luma"],
                            "luma_stddev": item["luma_stddev"],
                        }
                        for item in candidates
                    ],
                },
            )
            sheets = build_contact_sheets(
                files.root,
                records,
                columns=config.frames.contact_sheet_columns,
                sheet_width=config.frames.contact_sheet_width,
                max_frames=config.frames.contact_sheet_max_frames,
            )
            reason_counts = Counter(reason for item in records for reason in item["reason"])
            timestamps = [start] + [item["timestamp"] for item in records] + [end]
            gaps = [right - left for left, right in pairwise(timestamps)]
            minute_buckets: list[dict[str, Any]] = []
            minute_start = start
            minute_index = 0
            while minute_start < end:
                minute_end = min(end, minute_start + 60)
                frame_ids = [
                    item["frame_id"]
                    for item in records
                    if minute_start <= item["timestamp"] < minute_end
                    or (minute_end == end and item["timestamp"] == end)
                ]
                minute_buckets.append(
                    {
                        "minute_index": minute_index,
                        "start": minute_start,
                        "end": minute_end,
                        "has_evidence": bool(frame_ids),
                        "frame_ids": frame_ids,
                    }
                )
                minute_index += 1
                minute_start = minute_end
            matched_segment_ids = {
                str(item["segment_id"])
                for item in cue_matches
                if item.get("segment_id") is not None
            }
            selected_cue_segment_ids = {
                segment_id
                for item in records
                for segment_id in item["nearby_segment_ids"]
                if segment_id in matched_segment_ids
            }
            coverage = {
                "schema_version": "1.0.0",
                "range": {"start": start, "end": end, "duration": end - start},
                "counts": {
                    "scene_candidates": len(scene) + len(black_probes),
                    "cue_candidates": len(cues),
                    "periodic_candidates": len(periodic),
                    "merged_candidates": len(merged),
                    "extracted_candidates": len(candidates),
                    "post_quality_and_dedup": pre_final,
                    "final": len(records),
                },
                "reason_counts": dict(reason_counts),
                "max_gap_seconds": max(gaps) if gaps else end - start,
                "per_minute": len(records) / max((end - start) / 60, 1 / 60),
                "budgets": {
                    "max_candidates": config.frames.max_candidate_frames,
                    "candidate_dropped": budget_dropped,
                    "max_final": config.frames.max_final_frames,
                    "final_dropped": max(0, pre_final - len(records)),
                },
                "minute_buckets": minute_buckets,
                "quality_flags": quality_summary,
                "cue_coverage": {
                    "matched_segments": len(cue_matches),
                    "selected_frames_with_cue": sum(
                        "subtitle_cue" in item["reason"] for item in records
                    ),
                    "covered_segment_ids": sorted(selected_cue_segment_ids),
                    "segment_coverage_ratio": (
                        len(selected_cue_segment_ids) / len(matched_segment_ids)
                        if matched_segment_ids
                        else None
                    ),
                },
            }
            atomic_write_json(files.frame_coverage, coverage)
            atomic_write_json(files.root / "frames/frame-coverage-report.json", coverage)
            result = FrameRunResult(
                job_id=job_id,
                status="frames_ready",
                cache_hit=False,
                candidate_count=len(candidates),
                deduplicated_count=pre_final,
                final_count=len(records),
                frames_path=str(files.frames_jsonl.relative_to(files.root)),
                contact_sheet_paths=[item["path"] for item in sheets],
                coverage_path=str(files.frame_coverage.relative_to(files.root)),
                details={"range": [start, end], "max_final_frames": config.frames.max_final_frames},
            )
            artifacts = [
                {"frame_id": item["frame_id"], "path": item["path"], "sha256": item["sha256"]}
                for item in records
            ]
            artifacts.extend(
                {
                    "candidate_id": item["candidate_id"],
                    "path": item["path"],
                    "sha256": item["sha256"],
                }
                for item in candidates
            )
            artifacts.extend({"path": item["path"], "sha256": item["sha256"]} for item in sheets)
            artifacts.extend(
                {"path": str(path.relative_to(files.root)), "sha256": hash_file(path)}
                for path in [
                    files.frames_jsonl,
                    files.frame_candidates_jsonl,
                    files.frame_coverage,
                    files.root / "frames/frame-dedup-report.json",
                    files.root / "frames/frame-quality-report.json",
                    files.root / "frames/cue-matches.json",
                    files.root / "frames/formula-cue-matches.json",
                    files.root / "frames/progressive-slide-report.json",
                    files.root / "frames/frame-coverage-report.json",
                    files.root / "frames/contact-sheet-manifest.json",
                ]
            )
            atomic_write_json(
                files.frames_manifest,
                {
                    "schema_version": "1.0.0",
                    "input_hash": input_hash,
                    "config_hash": config_hash,
                    "generated_at": utc_now(),
                    "artifacts": artifacts,
                    "result": result.model_dump(mode="json"),
                },
            )
        except Exception as exc:
            finish_stage_failure(state, "frames_ready", attempt, error=str(exc), recoverable=True)
            save_state(files.state, state)
            record_transition(
                files.events,
                state,
                "stage_failed",
                stage="frames_ready",
                error=str(exc),
                recoverable=True,
            )
            if isinstance(exc, FrameError):
                raise
            raise FrameError(f"Frame extraction failed: {exc}") from exc
        finish_stage_success(
            state, "frames_ready", attempt, output_hash=hash_file(files.frames_manifest)
        )
        save_state(files.state, state)
        record_transition(
            files.events,
            state,
            "stage_succeeded",
            stage="frames_ready",
            output_hash=hash_file(files.frames_manifest),
        )
        return result


def inspect_frames(job_id: str, *, config: AppConfig) -> dict[str, Any]:
    files, _, state = load_job_context(job_id, config=config)
    if not files.frames_manifest.is_file():
        raise StateError("Frames are not ready. Run 'lectureflow frames extract <job-id>'.")
    manifest = _read_json(files.frames_manifest)
    valid, damaged = _artifact_integrity(files.root, manifest)
    return {
        "job_id": job_id,
        "stage": state.stages["frames_ready"].status.value,
        "integrity": valid,
        "damaged": damaged,
        "result": manifest.get("result"),
        "coverage": _read_json(files.frame_coverage),
    }


def rebuild_contact_sheets(job_id: str, *, config: AppConfig) -> dict[str, Any]:
    files, _, _ = load_job_context(job_id, config=config)
    if not files.frames_jsonl.is_file():
        raise StateError("Frames are not ready.")
    with JobLock(files.lock):
        state = load_state(files.state)
        if state.stages["frames_ready"].status != StageStatus.SUCCEEDED:
            raise StateError("frames_ready must be succeeded before rebuilding contact sheets.")
        artifact = _read_json(files.frames_manifest)
        invalidate_from(state, "frames_ready")
        contact_config = {
            "columns": config.frames.contact_sheet_columns,
            "width": config.frames.contact_sheet_width,
            "max_frames": config.frames.contact_sheet_max_frames,
        }
        attempt = start_stage(
            state,
            "frames_ready",
            input_hash=str(artifact["input_hash"]),
            config_hash=hash_object(contact_config),
            tool="Pillow-contact-sheet",
            tool_version=None,
        )
        save_state(files.state, state)
        try:
            records = [
                json.loads(line)
                for line in files.frames_jsonl.read_text(encoding="utf-8").splitlines()
                if line
            ]
            sheets = build_contact_sheets(
                files.root,
                records,
                columns=config.frames.contact_sheet_columns,
                sheet_width=config.frames.contact_sheet_width,
                max_frames=config.frames.contact_sheet_max_frames,
            )
            preserved = [
                item
                for item in artifact.get("artifacts", [])
                if not str(item.get("path", "")).startswith("frames/contact-sheets/")
                and item.get("path") != "frames/contact-sheet-manifest.json"
            ]
            preserved.extend({"path": item["path"], "sha256": item["sha256"]} for item in sheets)
            contact_manifest = files.root / "frames/contact-sheet-manifest.json"
            preserved.append(
                {
                    "path": str(contact_manifest.relative_to(files.root)),
                    "sha256": hash_file(contact_manifest),
                }
            )
            artifact["artifacts"] = preserved
            artifact["contact_sheet_config_hash"] = hash_object(contact_config)
            artifact["generated_at"] = utc_now()
            artifact["result"]["contact_sheet_paths"] = [item["path"] for item in sheets]
            atomic_write_json(files.frames_manifest, artifact)
        except Exception as exc:
            finish_stage_failure(state, "frames_ready", attempt, error=str(exc), recoverable=True)
            save_state(files.state, state)
            raise FrameError(f"Contact-sheet rebuild failed: {exc}") from exc
        output_hash = hash_file(files.frames_manifest)
        finish_stage_success(state, "frames_ready", attempt, output_hash=output_hash)
        save_state(files.state, state)
        record_transition(
            files.events,
            state,
            "stage_succeeded",
            stage="frames_ready",
            operation="contact_sheet_rebuild",
            output_hash=output_hash,
        )
        return {
            "job_id": job_id,
            "contact_sheet_paths": [item["path"] for item in sheets],
            "frame_count": len(records),
        }
