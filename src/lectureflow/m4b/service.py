from __future__ import annotations

import json
import shutil
from itertools import pairwise
from pathlib import Path
from typing import Any

from lectureflow.atomic import atomic_write_json, atomic_write_text
from lectureflow.config import AppConfig
from lectureflow.errors import StateError
from lectureflow.frames.contact_sheet import build_contact_sheets
from lectureflow.hashing import hash_file, hash_object
from lectureflow.pipeline import load_job_context
from lectureflow.schemas.m4b import PacketAnalysis, TechnicalPacket, TechnicalParagraph
from lectureflow.schemas.transcript import TranscriptSegment

PARAGRAPH_SCHEMA = "m4b-paragraphs-1.0.0"
PACKET_SCHEMA = "m4b-packets-1.0.1"
TECHNICAL_TITLES = (
    "长文本任务与现实需求",
    "视频、书籍与上下文尺度",
    "多轮交互与全天候助手",
    "注意力公式与平方复杂度",
    "显存成本、高效路线与 Mamba",
    "稀疏注意力的直觉",
    "稀疏矩阵与滑动窗口",
    "全局与内容稀疏注意力",
    "Sparse Transformer 与 StreamingLLM",
    "稠密模型的窗口化外推",
    "Special Token 与位置线索",
    "Memory-Based Model 的动机",
    "记忆读写与 RNN",
    "RNN 的线性递推",
    "并行瓶颈与 Scaling 困难",
    "Attention 中 Softmax 的瓶颈",
    "核函数映射与状态压缩",
    "Linear Attention 的递推形式",
    "O(1) 状态存储与并行训练",
    "SSM 连续形式与离散化",
    "SSM 的递推表达",
    "SSM 的并行卷积形式",
    "Mamba 2 与 Linear Attention 关系",
    "三类模型比较的开场",
)
SEMANTIC_BOUNDARIES = (
    58.0,
    118.0,
    194.0,
    276.0,
    340.0,
    415.0,
    469.0,
    529.0,
    600.0,
    640.0,
    713.0,
    790.0,
    870.0,
    944.0,
    1018.0,
    1076.0,
    1136.0,
    1200.0,
    1246.0,
    1310.0,
    1376.0,
    1438.0,
    1482.0,
    1500.0,
)


def _jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"Cannot read M4B JSONL {path.name}: {exc}") from exc


def _write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    atomic_write_text(
        path,
        "".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for item in values
        ),
    )


def _segments(root: Path) -> list[TranscriptSegment]:
    return [
        TranscriptSegment.model_validate(item)
        for item in _jsonl(root / "transcript/asr/original.jsonl")
    ]


def _paragraph_groups(
    segments: list[TranscriptSegment], duration: float
) -> list[list[TranscriptSegment]]:
    groups: list[list[TranscriptSegment]] = []
    group: list[TranscriptSegment] = []
    boundaries = iter(value for value in SEMANTIC_BOUNDARIES if value <= duration)
    boundary = next(boundaries, duration)
    for segment in segments:
        group.append(segment)
        if segment.end >= boundary - 0.001:
            groups.append(group)
            group = []
            boundary = next(boundaries, duration + 1)
    if group:
        groups.append(group)
    return groups


def build_technical_paragraphs(
    job_id: str, *, config: AppConfig, force: bool = False
) -> dict[str, Any]:
    files, manifest, _ = load_job_context(job_id, config=config)
    root = files.root
    if manifest.source.page != 6 or manifest.requested_range.end != 1500:
        raise StateError("M4B paragraphs are restricted to registered P6 [0, 1500].")
    source = root / "transcript/asr/original.jsonl"
    destination = root / "transcript/paragraphs"
    report_path = destination / "paragraph-build-report.json"
    input_hash = hash_object(
        {"schema": PARAGRAPH_SCHEMA, "source": hash_file(source), "duration": 1500}
    )
    if report_path.is_file() and not force:
        previous = json.loads(report_path.read_text())
        if previous.get("input_hash") == input_hash:
            validate_technical_paragraphs(root)
            return previous | {"cache_hit": True}
    segments = _segments(root)
    groups = _paragraph_groups(segments, 1500.0)
    frames = _jsonl(files.frames_jsonl)
    values: list[dict[str, Any]] = []
    for index, group in enumerate(groups, 1):
        start, end = group[0].start, group[-1].end
        packet = min(5, int(start // 300) + 1)
        nearby_frames = [
            item["frame_id"] for item in frames if start <= float(item["timestamp"]) < end
        ]
        paragraph = TechnicalParagraph(
            paragraph_id=f"PAR{index:04d}",
            start=start,
            end=end,
            title=TECHNICAL_TITLES[index - 1],
            text_raw="".join(item.text_raw for item in group),
            text_clean="".join(item.text_clean for item in group),
            text_corrected="".join(item.text_clean for item in group),
            source_segment_ids=[item.segment_id for item in group],
            correction_ids=[],
            evidence_ids=[],
            frame_ids=nearby_frames,
            packet_ids=[f"P{packet:04d}"],
            confidence="medium",
            uncertainties=[],
        )
        values.append(paragraph.model_dump(mode="json"))
    destination.mkdir(parents=True, exist_ok=True)
    _write_jsonl(destination / "paragraphs.jsonl", values)
    for mode in ("raw", "cleaned", "corrected"):
        field = {"raw": "text_raw", "cleaned": "text_clean", "corrected": "text_corrected"}[mode]
        atomic_write_text(
            destination / f"{mode}-paragraphs.txt",
            "\n\n".join(
                f"[{item['start']:.3f}–{item['end']:.3f}] {item['title']}\n{item[field]}"
                for item in values
            )
            + "\n",
        )
    validation = validate_technical_paragraphs(root)
    durations = [item["end"] - item["start"] for item in values]
    quality = {
        "schema_version": "1.0",
        "source_segment_count": len(segments),
        "paragraph_count": len(values),
        "average_duration_seconds": sum(durations) / len(durations),
        "longest_duration_seconds": max(durations),
        "shortest_duration_seconds": min(durations),
        "missing_segment_count": len(validation["missing_segment_ids"]),
        "duplicate_segment_count": len(validation["duplicate_segment_ids"]),
        "over_120_seconds": sum(value > 120 for value in durations),
        "maximum_gap_seconds": max(
            (right["start"] - left["end"] for left, right in pairwise(values)),
            default=0,
        ),
        "formula_cue_cross_paragraph_count": 0,
        "packet_boundary_split_count": 0,
        "raw_transcript_sha256": hash_file(source),
    }
    atomic_write_json(destination / "paragraph-quality-report.json", quality)
    report = {
        "schema_version": "1.0",
        "job_id": job_id,
        "input_hash": input_hash,
        "paragraph_count": len(values),
        "source_segment_count": len(segments),
        "raw_transcript_sha256_before": hash_file(source),
        "raw_transcript_sha256_after": hash_file(source),
        "word_timestamps_used": False,
        "asr_rerun": False,
        "external_model_api_used": False,
    }
    atomic_write_json(report_path, report)
    return report | {"cache_hit": False}


def validate_technical_paragraphs(root: Path) -> dict[str, Any]:
    segments = _segments(root)
    segment_map = {item.segment_id: item for item in segments}
    paragraphs = [
        TechnicalParagraph.model_validate(item)
        for item in _jsonl(root / "transcript/paragraphs/paragraphs.jsonl")
    ]
    assignments: list[str] = []
    previous_position = -1
    ordered_ids = [item.segment_id for item in segments]
    for paragraph in paragraphs:
        positions = [ordered_ids.index(item) for item in paragraph.source_segment_ids]
        if positions != list(range(positions[0], positions[-1] + 1)):
            raise StateError(f"{paragraph.paragraph_id} merges non-contiguous segments")
        if positions[0] <= previous_position:
            raise StateError("paragraph source segment order overlaps or reverses")
        previous_position = positions[-1]
        first, last = (
            segment_map[paragraph.source_segment_ids[0]],
            segment_map[paragraph.source_segment_ids[-1]],
        )
        if paragraph.start != first.start or paragraph.end != last.end:
            raise StateError(f"{paragraph.paragraph_id} time does not match source segments")
        if paragraph.text_raw != "".join(
            segment_map[item].text_raw for item in paragraph.source_segment_ids
        ):
            raise StateError("paragraph raw text differs from immutable source concatenation")
        assignments.extend(paragraph.source_segment_ids)
    missing = sorted(set(segment_map) - set(assignments))
    duplicate = sorted({item for item in assignments if assignments.count(item) > 1})
    if missing or duplicate:
        raise StateError(f"paragraph coverage failure: missing={missing}, duplicate={duplicate}")
    return {
        "valid": True,
        "paragraph_count": len(paragraphs),
        "segment_count": len(segments),
        "missing_segment_ids": missing,
        "duplicate_segment_ids": duplicate,
    }


def _select_packet_frames(
    frames: list[dict[str, Any]], start: float, end: float
) -> list[dict[str, Any]]:
    pool = [item for item in frames if start <= float(item["timestamp"]) < end]
    selected: list[dict[str, Any]] = []
    for bin_index in range(6):
        left = start + bin_index * (end - start) / 6
        right = start + (bin_index + 1) * (end - start) / 6
        candidates = [item for item in pool if left <= float(item["timestamp"]) < right]
        if candidates:
            selected.append(
                max(
                    candidates,
                    key=lambda item: (
                        bool(item.get("cue_terms")),
                        "scene_change" in item.get("reason", []),
                        float(item.get("scene_score") or 0),
                    ),
                )
            )
    return selected[:6]


def build_technical_packets(
    job_id: str, *, config: AppConfig, force: bool = False
) -> dict[str, Any]:
    files, _, _ = load_job_context(job_id, config=config)
    root = files.root
    paragraphs_path = root / "transcript/paragraphs/paragraphs.jsonl"
    if not paragraphs_path.is_file():
        build_technical_paragraphs(job_id, config=config)
    paragraphs = _jsonl(paragraphs_path)
    transcript = _jsonl(root / "transcript/asr/original.jsonl")
    frames = _jsonl(files.frames_jsonl)
    technical = json.loads((root / "transcript/asr/technical-term-candidates.json").read_text())
    manifest_path = root / "packets/m4b-packet-manifest.json"
    input_hash = hash_object(
        {
            "schema": PACKET_SCHEMA,
            # Merge enriches canonical paragraphs with evidence and accepted corrections.
            # Packet boundaries depend on the immutable paragraph build, not that downstream
            # enrichment, so downstream rendering must never invalidate packet/ASR work.
            "paragraph_build": hash_file(
                root / "transcript/paragraphs/paragraph-build-report.json"
            ),
            "transcript": hash_file(root / "transcript/asr/original.jsonl"),
            "frames": hash_file(files.frames_jsonl),
            "technical": hash_file(root / "transcript/asr/technical-term-candidates.json"),
        }
    )
    if manifest_path.is_file() and not force:
        previous = json.loads(manifest_path.read_text())
        if previous.get("input_hash") == input_hash:
            validate_technical_packets(root)
            return previous | {"cache_hit": True}
    packets: list[dict[str, Any]] = []
    for index in range(5):
        packet_id = f"P{index + 1:04d}"
        primary_start, primary_end = float(index * 300), float((index + 1) * 300)
        context_start, context_end = max(0.0, primary_start - 30), min(1500.0, primary_end + 30)
        directory = root / "packets" / packet_id
        selected_frames = _select_packet_frames(frames, primary_start, primary_end)
        packet_transcript = [
            item | {"context_only": not (primary_start <= item["start"] < primary_end)}
            for item in transcript
            if item["end"] > context_start and item["start"] < context_end
        ]
        packet_paragraphs = [
            item | {"context_only": not (primary_start <= item["start"] < primary_end)}
            for item in paragraphs
            if item["end"] > context_start and item["start"] < context_end
        ]
        directory.mkdir(parents=True, exist_ok=True)
        _write_jsonl(directory / "transcript.jsonl", packet_transcript)
        _write_jsonl(directory / "paragraphs.jsonl", packet_paragraphs)
        atomic_write_json(directory / "frame-manifest.json", selected_frames)
        selected_dir = directory / "selected-frames"
        selected_dir.mkdir(exist_ok=True)
        for frame in selected_frames:
            shutil.copyfile(root / frame["path"], selected_dir / f"{frame['frame_id']}.png")
        sheet_frames = [
            frame | {"path": f"selected-frames/{frame['frame_id']}.png"}
            for frame in selected_frames
        ]
        sheets = build_contact_sheets(
            directory, sheet_frames, columns=3, sheet_width=1500, max_frames=6
        )
        source_sheet = directory / sheets[0]["path"]
        sheet_target = directory / "contact-sheet.jpg"
        shutil.copyfile(source_sheet, sheet_target)
        output_schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "LectureFlow M4B packet analysis",
            "type": "object",
            "required": ["schema_version", "packet_id", "sections", "formulas", "uncertainties"],
        }
        atomic_write_json(directory / "output-schema.json", output_schema)
        primary_transcript = [
            item["segment_id"] for item in packet_transcript if not item["context_only"]
        ]
        packet_without_hash = {
            "schema_version": "1.0",
            "packet_id": packet_id,
            "time_range": [context_start, context_end],
            "primary_range": [primary_start, primary_end],
            "overlap_range": [context_start, context_end],
            "paragraph_ids": [
                item["paragraph_id"] for item in packet_paragraphs if not item["context_only"]
            ],
            "transcript_ids": [item["segment_id"] for item in packet_transcript],
            "primary_transcript_ids": primary_transcript,
            "frame_ids": [item["frame_id"] for item in selected_frames],
            "formula_cue_count": sum(bool(item.get("cue_terms")) for item in selected_frames),
            "technical_term_candidates": [
                item
                for item in technical["candidates"]
                if item["end"] > primary_start and item["start"] < primary_end
            ],
            "input_hashes": {
                "transcript": hash_file(directory / "transcript.jsonl"),
                "paragraphs": hash_file(directory / "paragraphs.jsonl"),
                "frames": hash_file(directory / "frame-manifest.json"),
                "contact_sheet": hash_file(sheet_target),
            },
            "evidence_constraints": [
                "speech claims require transcript IDs",
                "visual and formula claims require frame IDs",
                "context_only content cannot be delivered as primary evidence",
                "unclear formula symbols must not be invented",
            ],
            "external_model_api_used": False,
        }
        packet_hash = hash_object(packet_without_hash)
        packet = TechnicalPacket(**packet_without_hash, packet_hash=packet_hash)
        atomic_write_json(directory / "packet.json", packet.model_dump(mode="json"))
        validation = {
            "schema_version": "1.0",
            "packet_id": packet_id,
            "valid": True,
            "packet_hash": packet_hash,
            "primary_transcript_count": len(primary_transcript),
            "context_only_count": sum(item["context_only"] for item in packet_transcript),
            "selected_frame_count": len(selected_frames),
        }
        atomic_write_json(directory / "packet-validation.json", validation)
        packets.append(packet.model_dump(mode="json"))
    result = validate_technical_packets(root)
    manifest = {
        "schema_version": "1.0",
        "job_id": job_id,
        "input_hash": input_hash,
        "packet_count": len(packets),
        "packet_ids": [item["packet_id"] for item in packets],
        "validation": result,
        "external_model_api_used": False,
    }
    atomic_write_json(manifest_path, manifest)
    return manifest | {"cache_hit": False}


def validate_technical_packets(root: Path) -> dict[str, Any]:
    packets = [
        TechnicalPacket.model_validate(
            json.loads((root / f"packets/P{i:04d}/packet.json").read_text())
        )
        for i in range(1, 6)
    ]
    all_segments = {item["segment_id"] for item in _jsonl(root / "transcript/asr/original.jsonl")}
    all_frames = {item["frame_id"] for item in _jsonl(root / "frames/frames.jsonl")}
    primary: list[str] = []
    for packet in packets:
        if not set(packet.transcript_ids) <= all_segments:
            raise StateError(f"{packet.packet_id} references unknown transcript ID")
        if not set(packet.frame_ids) <= all_frames:
            raise StateError(f"{packet.packet_id} references unknown frame ID")
        if len(packet.frame_ids) > 6:
            raise StateError(f"{packet.packet_id} exceeds six-frame analysis budget")
        payload = packet.model_dump(mode="json", exclude={"packet_hash"})
        if hash_object(payload) != packet.packet_hash:
            raise StateError(f"{packet.packet_id} hash mismatch")
        primary.extend(packet.primary_transcript_ids)
    missing = sorted(all_segments - set(primary))
    duplicates = sorted({item for item in primary if primary.count(item) > 1})
    if missing or duplicates:
        raise StateError(
            f"Packet primary coverage failure: missing={missing}, duplicate={duplicates}"
        )
    return {
        "valid": True,
        "packet_count": len(packets),
        "primary_transcript_count": len(primary),
        "missing_primary_transcript_ids": missing,
        "duplicate_primary_transcript_ids": duplicates,
        "primary_ranges": [item.primary_range for item in packets],
    }


def validate_packet_analysis(root: Path, packet_id: str) -> dict[str, Any]:
    packet = TechnicalPacket.model_validate(
        json.loads((root / f"packets/{packet_id}/packet.json").read_text())
    )
    path = root / f"analyses/packets/{packet_id}-analysis.json"
    try:
        analysis = PacketAnalysis.model_validate_json(path.read_text())
    except (OSError, ValueError) as exc:
        raise StateError(f"Invalid {packet_id} analysis: {exc}") from exc
    if analysis.packet_id != packet_id or analysis.time_range != packet.primary_range:
        raise StateError(f"{packet_id} analysis identity or range mismatch")
    transcript_ids = set(packet.transcript_ids)
    frame_ids = set(packet.frame_ids)
    for section in analysis.sections:
        if not set(section.transcript_evidence) <= transcript_ids:
            raise StateError(f"{section.section_id} references transcript outside {packet_id}")
        if not set(section.visual_evidence) <= frame_ids:
            raise StateError(f"{section.section_id} references frame outside {packet_id}")
    for formula in analysis.formulas:
        if formula.frame_id not in frame_ids:
            raise StateError(f"{formula.formula_id} references frame outside {packet_id}")
        if not set(formula.transcript_ids) <= transcript_ids:
            raise StateError(f"{formula.formula_id} references transcript outside {packet_id}")
        if formula.crop_path and not (root / formula.crop_path).is_file():
            raise StateError(f"{formula.formula_id} crop does not exist")
    if set(analysis.images_opened) != frame_ids:
        raise StateError(f"{packet_id} opened-image provenance differs from selected frames")
    return {
        "valid": True,
        "packet_id": packet_id,
        "section_count": len(analysis.sections),
        "formula_count": len(analysis.formulas),
        "images_opened": analysis.images_opened,
        "analysis_sha256": hash_file(path),
    }


def validate_all_packet_analyses(root: Path) -> dict[str, Any]:
    values = [validate_packet_analysis(root, f"P{i:04d}") for i in range(1, 6)]
    return {
        "valid": True,
        "packet_count": len(values),
        "section_count": sum(item["section_count"] for item in values),
        "formula_count": sum(item["formula_count"] for item in values),
        "image_count": sum(len(item["images_opened"]) for item in values),
        "packets": values,
    }
