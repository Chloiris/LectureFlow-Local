from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lectureflow.atomic import atomic_write_json
from lectureflow.config import load_config
from lectureflow.frames.service import extract_frames, inspect_frames, rebuild_contact_sheets
from lectureflow.media.service import acquire_media
from lectureflow.pipeline import prepare_job
from lectureflow.state import invalidate_from, load_state, save_state, start_stage

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg suite is required for the deterministic fixture",
)


def _fixture(tmp_path: Path) -> Path:
    target = tmp_path / "中文 空格" / "课程 fixture.mp4"
    script = Path(__file__).parents[1] / "fixtures/generate_m2_video.py"
    subprocess.run([sys.executable, str(script), str(target)], check=True)
    return target


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    return load_config(cwd=tmp_path)


def _add_cue_transcript(job_dir: Path) -> None:
    atomic_write_json(
        job_dir / "transcript/original.json",
        {
            "schema_version": "1.0.0",
            "part_id": "local",
            "language": "zh-CN",
            "source": "local_file",
            "timed": True,
            "segments": [
                {
                    "segment_id": "T000001",
                    "start": 8.0,
                    "end": 11.0,
                    "text_raw": "看这里，这个公式",
                    "text_clean": "看这里，这个公式",
                },
                {
                    "segment_id": "T000002",
                    "start": 24.0,
                    "end": 28.0,
                    "text_raw": "接下来演示界面上内容",
                    "text_clean": "接下来演示界面上内容",
                },
            ],
        },
    )


def test_offline_m2_full_cache_and_single_frame_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _fixture(tmp_path)
    config = _config(tmp_path, monkeypatch)
    prepared = prepare_job(str(source), config=config, start=0, end=32)
    _add_cue_transcript(prepared.job_dir)

    first_media = acquire_media(prepared.job_id, config=config)
    second_media = acquire_media(prepared.job_id, config=config)
    assert first_media.cache_hit is False
    assert second_media.cache_hit is True

    first = extract_frames(prepared.job_id, config=config)
    second = extract_frames(prepared.job_id, config=config)
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert 1 <= first.final_count <= 30

    report = inspect_frames(prepared.job_id, config=config)
    assert report["integrity"] is True
    coverage = report["coverage"]
    assert coverage["counts"]["scene_candidates"] >= 2
    assert coverage["counts"]["cue_candidates"] >= 2
    assert coverage["counts"]["periodic_candidates"] >= 1
    assert coverage["quality_flags"].get("black", 0) >= 1
    assert coverage["minute_buckets"]
    assert all("has_evidence" in item for item in coverage["minute_buckets"])
    assert coverage["cue_coverage"]["segment_coverage_ratio"] is not None

    frames = [
        json.loads(line)
        for line in (prepared.job_dir / "frames/frames.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert all(0 <= item["timestamp"] <= 32 for item in frames)
    candidates = [
        json.loads(line)
        for line in (prepared.job_dir / "frames/frame-candidates.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert any(item["cue_offset_types"] for item in candidates)
    sheets = json.loads(
        (prepared.job_dir / "frames/contact-sheet-manifest.json").read_text(encoding="utf-8")
    )["sheets"]
    assert all(item["frame_count"] <= 12 for item in sheets)

    rebuilt = rebuild_contact_sheets(prepared.job_id, config=config)
    assert rebuilt["frame_count"] == first.final_count
    assert inspect_frames(prepared.job_id, config=config)["integrity"] is True

    damaged = prepared.job_dir / frames[0]["path"]
    damaged.unlink()
    repaired = extract_frames(prepared.job_id, config=config)
    assert repaired.repaired_frame_ids == [frames[0]["frame_id"]]
    assert damaged.is_file()
    assert inspect_frames(prepared.job_id, config=config)["integrity"] is True
    repaired_manifest = json.loads(
        (prepared.job_dir / "frames/frame-artifact-manifest.json").read_text(encoding="utf-8")
    )
    assert any(
        item["path"] == "reports/frame-coverage-report.json"
        for item in repaired_manifest["artifacts"]
    )


def test_frame_config_change_invalidates_frames_not_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _fixture(tmp_path)
    config = _config(tmp_path, monkeypatch)
    prepared = prepare_job(str(source), config=config, start=0, end=20)
    extract_frames(prepared.job_id, config=config)
    media_before = (prepared.job_dir / "media/media-manifest.json").read_bytes()

    changed = config.model_copy(deep=True)
    changed.frames.periodic_interval_seconds = 3
    result = extract_frames(prepared.job_id, config=changed)
    assert result.cache_hit is False
    assert (prepared.job_dir / "media/media-manifest.json").read_bytes() == media_before


def test_interrupted_frame_attempt_is_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _fixture(tmp_path)
    config = _config(tmp_path, monkeypatch)
    prepared = prepare_job(str(source), config=config, start=0, end=12)
    extract_frames(prepared.job_id, config=config)
    state_path = prepared.job_dir / "pipeline-state.json"
    state = load_state(state_path)
    invalidate_from(state, "frames_ready")
    start_stage(
        state,
        "frames_ready",
        input_hash="interrupted",
        config_hash="interrupted",
        tool="fixture",
        tool_version="1",
    )
    save_state(state_path, state)

    recovered = extract_frames(prepared.job_id, config=config)
    assert recovered.cache_hit is True
    current = load_state(state_path)
    attempts = current.stages["frames_ready"].attempts
    assert attempts[-2].recoverable is True
    assert "Previous process ended" in str(attempts[-2].error)
    assert attempts[-1].error is None


def test_final_frame_budget_is_hard_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "dynamic budget.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=10:duration=36",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(source),
        ],
        check=True,
    )
    config = _config(tmp_path, monkeypatch).model_copy(deep=True)
    config.frames.periodic_interval_seconds = 1
    config.frames.scene_threshold = 1
    config.frames.duplicate_threshold = 0
    config.frames.max_final_frames = 10
    prepared = prepare_job(str(source), config=config, start=0, end=36)
    result = extract_frames(prepared.job_id, config=config)
    report = inspect_frames(prepared.job_id, config=config)
    assert result.final_count == 10
    assert report["coverage"]["budgets"]["final_dropped"] > 0
