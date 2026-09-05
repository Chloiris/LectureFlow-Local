from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

import lectureflow.pipeline as pipeline_module
from lectureflow.cli import app
from lectureflow.config import load_config
from lectureflow.pipeline import job_status, prepare_job
from lectureflow.process import run_command


def test_offline_registration_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "离线 M0 smoke.mov"
    source.write_bytes(b"not real media; ffprobe is isolated in this M0 smoke")
    config = load_config(cwd=tmp_path).model_copy(
        update={"workspace_root": tmp_path / "workspaces"}
    )
    monkeypatch.setattr(
        pipeline_module,
        "probe_local",
        lambda descriptor: (
            {
                "format": {"duration": "30.0", "format_name": "fixture"},
                "streams": [{"codec_type": "video", "width": 1280, "height": 720}],
            },
            "fixture-1.0",
        ),
    )
    result = prepare_job(str(source), config=config, start=0, end=30)
    assert result.metadata_path and result.metadata_path.is_file()
    assert (result.job_dir / "pipeline-state.json").is_file()
    assert (result.job_dir / "logs/events.jsonl").is_file()
    assert job_status(result.job_id, config=config)["last_successful_milestone"] == "metadata_ready"


def test_real_ffmpeg_ffprobe_smoke_with_unicode_path(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("real media smoke requires the doctor-level FFmpeg tools")
    source = tmp_path / "真实 离线夹具.mp4"
    run_command(
        (
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000",
            "-t",
            "2",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            source,
        ),
        timeout=30,
    )
    config = load_config(cwd=tmp_path).model_copy(
        update={"workspace_root": tmp_path / "真实 工作空间"}
    )
    first = prepare_job(str(source), config=config, start=0, end=2)
    second = prepare_job(str(source), config=config, start=0, end=2)
    assert first.cache_hit is False
    assert second.cache_hit is True
    metadata = (first.job_dir / "raw/metadata/metadata.json").read_text(encoding="utf-8")
    assert '"codec_type": "video"' in metadata


def test_real_cli_round_trip_is_idempotent_with_unicode_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("real CLI smoke requires the doctor-level FFmpeg tools")
    source = tmp_path / "CLI 中文 空格.mp4"
    run_command(
        (
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:size=160x90:rate=2",
            "-t",
            "1",
            "-c:v",
            "mpeg4",
            source,
        ),
        timeout=30,
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    command = ["prepare", str(source), "--start", "0", "--end", "1", "--json"]
    first = runner.invoke(app, command)
    second = runner.invoke(app, command)
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    first_payload = json.loads(first.stdout)
    second_payload = json.loads(second.stdout)
    assert first_payload["cache_hit"] is False
    assert second_payload["cache_hit"] is True
    assert first_payload["job_id"] == second_payload["job_id"]
    status = runner.invoke(app, ["status", first_payload["job_id"], "--json"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.stdout)["last_successful_milestone"] == "metadata_ready"
