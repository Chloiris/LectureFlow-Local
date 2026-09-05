from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from lectureflow.atomic import atomic_write_json, atomic_write_text
from lectureflow.config import AppConfig, load_config
from lectureflow.hashing import hash_file
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


def m4a_fixture(tmp_path: Path) -> tuple[AppConfig, JobFiles, object, PipelineState]:
    config = load_config(cwd=tmp_path)
    root = ensure_job_layout(config.workspace_root, "m4a-fixture")
    files = JobFiles(root)
    state = new_state("m4a-fixture")
    manifest = SimpleNamespace(
        source=SimpleNamespace(bvid="BV1pf421z757"),
        requested_range=SimpleNamespace(start=0.0, end=300.0),
    )
    atomic_write_json(
        files.manifest,
        {
            "source": {
                "kind": "bilibili",
                "normalized": "https://www.bilibili.com/video/BV1pf421z757",
                "bvid": "BV1pf421z757",
            }
        },
    )
    segments: list[dict[str, object]] = []
    for index in range(1, 18):
        text = (
            "这一段包含<script>alert(1)</script>但只应作为文字显示。"
            if index == 2
            else "课程围绕大元模型展开。"
            if index == 7
            else "课程段落内容。"
        )
        segments.append(
            {
                "schema_version": "1.0.0",
                "segment_id": f"T{index:06d}",
                "start": float((index - 1) * 10),
                "end": float(index * 10),
                "text_raw": text,
                "text_clean": text,
                "source": "mlx_whisper",
                "language": "zh",
                "confidence": None,
                "speaker": None,
                "chapter_id": None,
                "part_id": "P1",
                "source_ref": {"raw_segment_index": index - 1},
            }
        )
    write_jsonl(root / "transcript/asr/original.jsonl", segments)
    (root / "transcript/asr/raw-mlx-whisper.json").write_text("{}")
    groups = [(1, 3), (4, 6), (7, 9), (10, 12), (13, 14), (15, 17)]
    atomic_write_json(
        root / "analyses/m4a/paragraph-plan.json",
        {
            "schema_version": "1.0",
            "paragraphs": [
                {
                    "title": f"段落 {number}",
                    "source_segment_ids": [f"T{item:06d}" for item in range(start, end + 1)],
                    "confidence": "high",
                    "uncertainties": [],
                }
                for number, (start, end) in enumerate(groups, 1)
            ],
        },
    )
    for frame_id, color in (("F000001", "blue"), ("F000002", "green")):
        path = root / f"frames/originals/{frame_id}.png"
        Image.new("RGB", (64, 64), color).save(path)
    frames = [
        {
            "frame_id": frame_id,
            "timestamp": timestamp,
            "path": f"frames/originals/{frame_id}.png",
            "sha256": hash_file(root / f"frames/originals/{frame_id}.png"),
        }
        for frame_id, timestamp in (("F000001", 0.0), ("F000002", 60.0))
    ]
    write_jsonl(files.frames_jsonl, frames)
    evidence: list[dict[str, object]] = []
    evidence_segments = [
        (1, 3),
        (4, 6),
        (7, 9),
        (10, 11),
        (12, 12),
        (13, 13),
        (14, 14),
        (15, 16),
        (17, 17),
    ]
    for index, (start, end) in enumerate(evidence_segments, 1):
        evidence.append(
            {
                "schema_version": "1.0",
                "evidence_id": f"E{index:06d}",
                "start": float((start - 1) * 10),
                "end": float(end * 10),
                "topic": f"主题 {index}",
                "claim": f"可审计结论 {index}",
                "transcript_ids": [f"T{item:06d}" for item in range(start, end + 1)],
                "frame_ids": ["F000002"] if index in {2, 4, 5, 6, 7, 8} else [],
                "evidence_type": "speech+slide" if index in {2, 4, 5, 6, 7, 8} else "speech",
                "confidence": "high",
                "uncertainty": None,
            }
        )
    write_jsonl(root / "evidence/evidence-ledger.jsonl", evidence)
    section_segments = [(1, 3), (4, 9), (10, 12), (13, 14), (15, 17)]
    atomic_write_json(
        root / "analyses/P0001-analysis.json",
        {
            "sections": [
                {
                    "title": f"章节 {index}",
                    "summary": f"有证据的章节摘要 {index}",
                    "transcript_evidence": [f"T{item:06d}" for item in range(start, end + 1)],
                    "visual_evidence": ["F000002"] if index > 1 else ["F000001"],
                    "confidence": "high",
                    "uncertainties": [],
                }
                for index, (start, end) in enumerate(section_segments, 1)
            ],
            "course_information": [],
        },
    )
    atomic_write_json(
        root / "analyses/m4a/correction-plan.json",
        {
            "schema_version": "1.0",
            "corrections": [
                {
                    "schema_version": "1.0",
                    "correction_id": "C000001",
                    "paragraph_id": "PAR0003",
                    "source_segment_ids": ["T000007"],
                    "start": 60.0,
                    "end": 70.0,
                    "original_text": "大元模型",
                    "suggested_text": "大语言模型",
                    "change_type": "term",
                    "reason": "F000002 显示 Large Language Models。",
                    "transcript_evidence": ["T000007"],
                    "visual_evidence": ["F000002"],
                    "confidence": "high",
                    "decision": "accepted",
                    "applied": True,
                },
                {
                    "schema_version": "1.0",
                    "correction_id": "C000002",
                    "paragraph_id": "PAR0001",
                    "source_segment_ids": ["T000001"],
                    "start": 0.0,
                    "end": 10.0,
                    "original_text": "课程",
                    "suggested_text": "本课程",
                    "change_type": "other",
                    "reason": "缺少足够独立依据，等待核验。",
                    "transcript_evidence": ["T000001"],
                    "visual_evidence": [],
                    "confidence": "low",
                    "decision": "pending",
                    "applied": False,
                },
            ],
        },
    )
    media = root / "media/video/source.mp4"
    media.write_bytes(bytes(range(256)) * 8)
    atomic_write_json(
        files.media_manifest,
        {
            "schema_version": "1.0.0",
            "source_kind": "bilibili",
            "source_id": "BV1pf421z757",
            "part_id": "P1",
            "selected_part": 1,
            "part_selection_reason": "fixture",
            "source_url": "https://www.bilibili.com/video/BV1pf421z757?p=1",
            "requested_start": 0.0,
            "requested_end": 300.0,
            "media_path": "media/video/source.mp4",
            "storage": "job_local",
            "container": "mp4",
            "duration": 300.0,
            "source_duration": 300.0,
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
    return config, files, manifest, state
