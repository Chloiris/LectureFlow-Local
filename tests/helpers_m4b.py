from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from lectureflow.atomic import atomic_write_json, atomic_write_text
from lectureflow.config import AppConfig, load_config
from lectureflow.hashing import hash_file
from lectureflow.m4b.service import build_technical_packets, build_technical_paragraphs
from lectureflow.paths import JobFiles, ensure_job_layout
from lectureflow.schemas.state import PipelineState
from lectureflow.state import new_state


def write_jsonl(path: Path, values: list[dict[str, object]]) -> None:
    atomic_write_text(
        path,
        "".join(
            json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n" for item in values
        ),
    )


def m4b_fixture(
    tmp_path: Path, monkeypatch: object
) -> tuple[AppConfig, JobFiles, object, PipelineState]:
    import lectureflow.m4b.service as service

    config = load_config(cwd=tmp_path)
    root = ensure_job_layout(config.workspace_root, "m4b-fixture")
    files = JobFiles(root)
    state = new_state("m4b-fixture")
    manifest = SimpleNamespace(
        source=SimpleNamespace(page=6, bvid="BV1pf421z757"),
        requested_range=SimpleNamespace(start=0.0, end=1500.0),
    )

    segments: list[dict[str, object]] = []
    for index in range(1, 751):
        start = float((index - 1) * 2)
        text = "技术课程内容。"
        if index == 1:
            text = "soft max 与托伦特是本段的可疑识别。"
        segments.append(
            {
                "schema_version": "1.0.0",
                "segment_id": f"T{index:06d}",
                "start": start,
                "end": start + 2.0,
                "text_raw": text,
                "text_clean": text,
                "source": "mlx_whisper",
                "language": "zh",
                "confidence": None,
                "speaker": None,
                "chapter_id": None,
                "part_id": "P6",
                "source_ref": {"raw_segment_index": index - 1},
            }
        )
    write_jsonl(root / "transcript/asr/original.jsonl", segments)
    atomic_write_json(root / "transcript/asr/technical-term-candidates.json", {"candidates": []})

    frames: list[dict[str, object]] = []
    for index in range(1, 31):
        frame_id = f"F{index:06d}"
        path = root / f"frames/originals/{frame_id}.png"
        Image.new("RGB", (64, 64), (index * 7 % 255, 80, 160)).save(path)
        frames.append(
            {
                "frame_id": frame_id,
                "timestamp": float((index - 1) * 50 + 25),
                "path": f"frames/originals/{frame_id}.png",
                "sha256": hash_file(path),
                "reason": ["scene_change", "subtitle_cue"],
                "scene_score": 0.5,
                "cue_terms": ["公式"] if index % 4 == 0 else [],
            }
        )
    write_jsonl(files.frames_jsonl, frames)
    atomic_write_json(
        root / "frames/frame-coverage-report.json",
        {"cue_coverage": {"segment_coverage_ratio": 0.9}},
    )

    media = root / "media/video/source.mp4"
    media.write_bytes(bytes(range(256)) * 8)
    atomic_write_json(
        files.media_manifest,
        {
            "schema_version": "1.0.0",
            "source_kind": "bilibili",
            "source_id": "BV1pf421z757",
            "part_id": "P6",
            "selected_part": 6,
            "part_selection_reason": "fixture",
            "source_url": "https://www.bilibili.com/video/BV1pf421z757?p=6",
            "requested_start": 0.0,
            "requested_end": 1500.0,
            "media_path": "media/video/source.mp4",
            "storage": "job_local",
            "container": "mp4",
            "duration": 1500.0,
            "source_duration": 1500.0,
            "width": 1920,
            "height": 1080,
            "file_size": media.stat().st_size,
            "sha256": hash_file(media),
            "video_codec": "h264",
            "audio_included": True,
            "format_selector": None,
            "tool_versions": {"ffprobe": "fixture", "yt-dlp": None},
            "input_hash": "input",
            "config_hash": "config",
            "generated_at": "2026-08-13T00:00:00Z",
        },
    )

    context = lambda *args, **kwargs: (files, manifest, state)  # noqa: E731
    monkeypatch.setattr(service, "load_job_context", context)
    build_technical_paragraphs("m4b-fixture", config=config)
    build_technical_packets("m4b-fixture", config=config)

    paragraphs = [
        json.loads(line)
        for line in (root / "transcript/paragraphs/paragraphs.jsonl").read_text().splitlines()
        if line
    ]
    for packet_index in range(1, 6):
        packet_id = f"P{packet_index:04d}"
        packet = json.loads((root / f"packets/{packet_id}/packet.json").read_text())
        transcript_id = packet["primary_transcript_ids"][0]
        frame_id = packet["frame_ids"][0]
        primary_start, primary_end = packet["primary_range"]
        terms = [
            {
                "term": "Softmax",
                "aliases": ["soft max"],
                "transcript_ids": [transcript_id],
                "frame_ids": [frame_id],
                "confidence": "high",
            }
        ]
        corrections: list[dict[str, object]] = []
        if packet_index == 1:
            corrections = [
                {
                    "original": "soft max",
                    "suggested": "Softmax",
                    "transcript_ids": ["T000001"],
                    "frame_ids": [frame_id],
                    "confidence": "high",
                    "decision": "accepted",
                },
                {
                    "original": "托伦特",
                    "suggested": "Torrent",
                    "transcript_ids": ["T000001"],
                    "frame_ids": [],
                    "confidence": "medium",
                    "decision": "pending",
                },
            ]
        formulas: list[dict[str, object]] = []
        if packet_index == 5:
            timestamp = float(primary_start + 25)
            paragraph_id = next(
                item["paragraph_id"]
                for item in paragraphs
                if item["start"] <= timestamp < item["end"]
            )
            formulas = [
                {
                    "schema_version": "1.0",
                    "formula_id": "FORM0001",
                    "frame_id": frame_id,
                    "timestamp": timestamp,
                    "crop_path": None,
                    "visual_transcription": "画面显示 y 等于 Ax。",
                    "latex": "y = Ax",
                    "spoken_context": "讲者用矩阵乘法说明状态更新。",
                    "transcript_ids": [transcript_id],
                    "paragraph_ids": [paragraph_id],
                    "variables": [{"symbol": "x", "meaning": "输入", "evidence": [frame_id]}],
                    "assumptions": [],
                    "role": "definition",
                    "confidence": "high",
                    "uncertain_symbols": [],
                    "verification_status": "verified",
                }
            ]
        analysis = {
            "schema_version": "1.0",
            "packet_id": packet_id,
            "time_range": packet["primary_range"],
            "sections": [
                {
                    "section_id": f"S{packet_index:04d}",
                    "title": f"技术主题 {packet_index}",
                    "start": primary_start,
                    "end": primary_end,
                    "summary": f"Packet {packet_index} 的可审计摘要。",
                    "transcript_evidence": [transcript_id],
                    "visual_evidence": [frame_id],
                    "confidence": "high",
                    "uncertainties": [],
                }
            ],
            "concepts": [
                {
                    "concept": "长序列",
                    "transcript_ids": [transcript_id],
                    "frame_ids": [frame_id],
                }
            ],
            "definitions": [
                {
                    "term": "Softmax",
                    "definition": "归一化映射。",
                    "transcript_ids": [transcript_id],
                    "frame_ids": [frame_id],
                }
            ],
            "technical_terms": terms,
            "formulas": formulas,
            "derivations": [],
            "examples": [],
            "comparisons": [],
            "visual_findings": [],
            "asr_correction_suggestions": corrections,
            "pitfalls": [],
            "uncertainties": [],
            "coverage": {"primary_range_covered": True},
            "images_opened": packet["frame_ids"],
            "image_tool": "view_image",
            "external_model_api_used": False,
        }
        atomic_write_json(root / f"analyses/packets/{packet_id}-analysis.json", analysis)
    return config, files, manifest, state
