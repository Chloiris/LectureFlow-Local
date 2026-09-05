from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from lectureflow.atomic import atomic_write_json, atomic_write_text
from lectureflow.config import AppConfig
from lectureflow.errors import StateError
from lectureflow.hashing import hash_file, hash_object
from lectureflow.pipeline import load_job_context
from lectureflow.schemas.multimodal import PacketAnalysis
from lectureflow.schemas.transcript import TranscriptSegment
from lectureflow.state import (
    JobLock,
    finish_stage_failure,
    finish_stage_success,
    record_transition,
    save_state,
    start_stage,
)

PACKET_SCHEMA = "packet-1.0.0"


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"Cannot read packet input {path.name}: {error}") from error


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"Cannot read packet JSONL {path.name}: {error}") from error


def _copy_verified(source: Path, destination: Path, digest: str) -> None:
    if not source.is_file() or hash_file(source) != digest:
        raise StateError(f"Packet source hash mismatch: {source.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    if hash_file(destination) != digest:
        raise StateError(f"Packet copy hash mismatch: {destination.name}")


def _packet_paths(root: Path) -> tuple[Path, Path]:
    packet_root = root / "packets/P0001"
    return packet_root, packet_root / "packet.json"


def _packet_cache_valid(root: Path) -> bool:
    try:
        validate_packet(root, "P0001")
        return True
    except StateError:
        return False


def build_packet(job_id: str, *, config: AppConfig) -> dict[str, Any]:
    files, _, state = load_job_context(job_id, config=config)
    packet_root, packet_path = _packet_paths(files.root)
    transcript_source = files.root / "transcript/asr/cleaned.jsonl"
    frames = _load_jsonl(files.frames_jsonl)
    observations = _load_jsonl(files.root / "analyses/vision-smoke/visual-observations.jsonl")
    transcript = _load_jsonl(transcript_source)
    transcript_ids = [str(item["segment_id"]) for item in transcript]
    frame_ids = [str(item["frame_id"]) for item in frames]
    source_hash = hash_object(
        {
            "schema": PACKET_SCHEMA,
            "transcript_sha256": hash_file(transcript_source),
            "frames_sha256": hash_file(files.frames_jsonl),
            "vision_sha256": hash_file(
                files.root / "analyses/vision-smoke/visual-observations.jsonl"
            ),
            "media_manifest_sha256": hash_file(files.media_manifest),
            "audio_manifest_sha256": hash_file(files.root / "media/audio/asr-input-000-300.json"),
        }
    )
    if packet_path.is_file() and _packet_cache_valid(files.root):
        stored = _load_json(packet_path)
        if stored.get("source_hash") == source_hash:
            record_transition(files.events, state, "stage_cache_hit", stage="packets_ready")
            return {"job_id": job_id, "packet_id": "P0001", "cache_hit": True, **stored}
    with JobLock(files.lock):
        attempt = start_stage(
            state,
            "packets_ready",
            input_hash=source_hash,
            config_hash=hash_object({"packet_id": "P0001", "range": [0, 300]}),
            tool="lectureflow-packet-builder",
            tool_version="1.0",
        )
        save_state(files.state, state)
        try:
            packet_root.mkdir(parents=True, exist_ok=True)
            atomic_write_text(packet_root / "transcript.jsonl", transcript_source.read_text())
            sheet_manifest = _load_json(files.root / "frames/contact-sheet-manifest.json")
            sheet = sheet_manifest["sheets"][0]
            _copy_verified(
                files.root / sheet["path"], packet_root / "contact-sheet.jpg", sheet["sha256"]
            )
            copied_frames: list[dict[str, Any]] = []
            for frame in frames:
                target = packet_root / "selected-frames" / f"{frame['frame_id']}.png"
                _copy_verified(files.root / frame["path"], target, frame["sha256"])
                copied_frames.append(
                    {
                        **frame,
                        "packet_path": str(target.relative_to(packet_root)),
                    }
                )
            frame_manifest = {
                "schema_version": "1.0",
                "frames": copied_frames,
                "m3a_visual_observations": observations,
            }
            atomic_write_json(packet_root / "frame-manifest.json", frame_manifest)
            atomic_write_json(
                packet_root / "analysis-output.schema.json", PacketAnalysis.model_json_schema()
            )
            artifact_paths = [
                "transcript.jsonl",
                "frame-manifest.json",
                "contact-sheet.jpg",
                "analysis-output.schema.json",
                *[f"selected-frames/{frame_id}.png" for frame_id in frame_ids],
            ]
            artifact_hashes = {path: hash_file(packet_root / path) for path in artifact_paths}
            packet_hash = hash_object(
                {
                    "source_hash": source_hash,
                    "artifacts": artifact_hashes,
                    "transcript_ids": transcript_ids,
                    "frame_ids": frame_ids,
                }
            )
            packet = {
                "schema_version": "1.0",
                "packet_id": "P0001",
                "job_id": job_id,
                "time_range": [0.0, 300.0],
                "source_hash": source_hash,
                "packet_hash": packet_hash,
                "transcript_ids": transcript_ids,
                "frame_ids": frame_ids,
                "media_manifest": "../../media/media-manifest.json",
                "media_manifest_sha256": hash_file(files.media_manifest),
                "audio_manifest": "../../media/audio/asr-input-000-300.json",
                "audio_manifest_sha256": hash_file(
                    files.root / "media/audio/asr-input-000-300.json"
                ),
                "m3a_observations": "../../analyses/vision-smoke/visual-observations.jsonl",
                "known_visual_vocabulary": [
                    "Introduction to Large Language Models",
                    "Course Info",
                    "Teaching Team",
                    "14 lectures",
                    "three assignments",
                    "final project",
                    "1-2 members",
                    "RTX 4090/3090 GPUs",
                    "July 19",
                ],
                "analysis_schema": "analysis-output.schema.json",
                "evidence_constraints": [
                    "speech claims cite transcript IDs",
                    "visual claims cite frame IDs",
                    "speech+slide claims cite both",
                    "ASR corrections are suggestions and never overwrite text_raw",
                    "no formula or code may be invented",
                ],
                "artifacts": artifact_hashes,
            }
            atomic_write_json(packet_path, packet)
            validation = validate_packet(files.root, "P0001")
            atomic_write_json(packet_root / "packet-validation.json", validation)
            finish_stage_success(
                state, "packets_ready", attempt, output_hash=hash_file(packet_path)
            )
            save_state(files.state, state)
            record_transition(files.events, state, "stage_succeeded", stage="packets_ready")
            return {"job_id": job_id, "cache_hit": False, **packet}
        except Exception as error:
            finish_stage_failure(
                state, "packets_ready", attempt, error=str(error), recoverable=True
            )
            save_state(files.state, state)
            record_transition(files.events, state, "stage_failed", stage="packets_ready")
            if isinstance(error, StateError):
                raise
            raise StateError(f"Packet build failed: {error}") from error


def validate_packet(root: Path, packet_id: str) -> dict[str, Any]:
    if packet_id != "P0001":
        raise StateError("M3B contains only packet P0001.")
    packet_root = root / f"packets/{packet_id}"
    packet = _load_json(packet_root / "packet.json")
    if packet.get("time_range") != [0.0, 300.0]:
        raise StateError("Packet time range must be 0–300 seconds.")
    transcript = _load_jsonl(packet_root / "transcript.jsonl")
    transcript_ids: set[str] = set()
    for raw in transcript:
        try:
            segment = TranscriptSegment.model_validate(raw)
        except ValidationError as error:
            raise StateError(f"Invalid packet transcript segment: {error}") from error
        transcript_ids.add(segment.segment_id)
    frames = _load_json(packet_root / "frame-manifest.json")["frames"]
    frame_ids = {str(frame["frame_id"]) for frame in frames}
    if set(packet["transcript_ids"]) != transcript_ids:
        raise StateError("Packet references a nonexistent transcript ID.")
    if set(packet["frame_ids"]) != frame_ids:
        raise StateError("Packet references a nonexistent frame ID.")
    for relative, digest in packet["artifacts"].items():
        target = packet_root / relative
        if not target.is_file() or hash_file(target) != digest:
            raise StateError(f"Packet artifact hash mismatch: {relative}")
    expected_hash = hash_object(
        {
            "source_hash": packet["source_hash"],
            "artifacts": packet["artifacts"],
            "transcript_ids": packet["transcript_ids"],
            "frame_ids": packet["frame_ids"],
        }
    )
    if packet["packet_hash"] != expected_hash:
        raise StateError("Packet hash mismatch.")
    return {
        "schema_version": "1.0",
        "packet_id": packet_id,
        "valid": True,
        "packet_hash": expected_hash,
        "transcript_count": len(transcript_ids),
        "frame_count": len(frame_ids),
    }


def validate_analysis(root: Path) -> dict[str, Any]:
    try:
        analysis = PacketAnalysis.model_validate(_load_json(root / "analyses/P0001-analysis.json"))
    except ValidationError as error:
        raise StateError(f"Invalid P0001 analysis: {error}") from error
    packet = _load_json(root / "packets/P0001/packet.json")
    transcript_ids = set(packet["transcript_ids"])
    frame_ids = set(packet["frame_ids"])
    findings = [
        *analysis.concepts,
        *analysis.definitions,
        *analysis.course_information,
        *analysis.formulas,
        *analysis.code_blocks,
        *analysis.visual_findings,
        *analysis.examples,
        *analysis.pitfalls,
        *analysis.uncertainties,
    ]
    for item in [*analysis.sections, *findings]:
        if not set(item.transcript_evidence) <= transcript_ids:
            raise StateError("Analysis references a nonexistent transcript ID.")
        if not set(item.visual_evidence) <= frame_ids:
            raise StateError("Analysis references a nonexistent frame ID.")
    provenance_path = root / "reports/m3b-provenance.json"
    if not provenance_path.is_file():
        raise StateError("Analysis image claims require M3B provenance.")
    provenance = _load_json(provenance_path)
    if provenance.get("external_model_api_used"):
        raise StateError("External model API use invalidates M3B.")
    if set(provenance.get("images_opened", [])) != set(analysis.images_opened):
        raise StateError("Analysis opened images differ from provenance.")
    return {
        "schema_version": "1.0",
        "packet_id": "P0001",
        "valid": True,
        "section_count": len(analysis.sections),
        "finding_count": len(findings),
    }
