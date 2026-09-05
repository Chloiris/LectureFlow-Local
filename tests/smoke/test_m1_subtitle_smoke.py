from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import lectureflow.pipeline as pipeline_module
import lectureflow.subtitles.service as service_module
from lectureflow.cli import app
from lectureflow.config import AppConfig, load_config
from lectureflow.errors import StateError, SubtitleError
from lectureflow.pipeline import job_status
from lectureflow.state import invalidate_from, load_state, save_state, start_stage
from lectureflow.subtitles.service import (
    export_job_subtitles,
    import_local_subtitle,
    inspect_subtitles,
    resume_to_transcript,
)

FIXTURE = Path(__file__).parents[1] / "fixtures/subtitles/sample.srt"


def _config(tmp_path: Path) -> AppConfig:
    base = load_config(cwd=tmp_path)
    return base.model_copy(update={"workspace_root": tmp_path / "工作 空间"})


def _config_file(tmp_path: Path) -> Path:
    path = tmp_path / "配置 文件.toml"
    workspace = tmp_path / "CLI 工作 空间"
    path.write_text(f'workspace_root = "{workspace}"\n', encoding="utf-8")
    return path


def test_local_import_cache_resume_export_and_immutable_raw(tmp_path: Path) -> None:
    source = tmp_path / "中文 字幕.srt"
    original_bytes = FIXTURE.read_bytes()
    source.write_bytes(original_bytes)
    config = _config(tmp_path)

    first = import_local_subtitle(source, config=config, language="zh-CN")
    second = import_local_subtitle(source, config=config, language="zh-CN")
    resumed = resume_to_transcript(first.job_id, config=config)
    exported = export_job_subtitles(first.job_id, config=config)

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert resumed.cache_hit is True
    assert exported.message == "Exports regenerated from immutable original.json."
    assert first.selected_sources == {"local": "local_file"}
    assert all(
        (first.transcript_root / name).is_file()
        for name in (
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
    )
    archived = list((first.transcript_root.parent / "raw/subtitles/local").glob("*.original.srt"))
    assert len(archived) == 1
    assert archived[0].read_bytes() == original_bytes

    source.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n内容已更新\n",
        encoding="utf-8",
    )
    changed = import_local_subtitle(source, config=config, language="zh-CN")
    archives_after = list(
        (changed.transcript_root.parent / "raw/subtitles/local").glob("*.original.srt")
    )
    assert changed.cache_hit is False
    assert len(archives_after) == 2
    assert archived[0].read_bytes() == original_bytes


def test_force_only_restarts_subtitle_and_descendants(tmp_path: Path) -> None:
    source = tmp_path / "force 字幕.srt"
    source.write_bytes(FIXTURE.read_bytes())
    config = _config(tmp_path)
    first = import_local_subtitle(source, config=config, language="zh-CN")
    before = job_status(first.job_id, config=config)
    forced = import_local_subtitle(
        source,
        config=config,
        language="zh-CN",
        force_stage=True,
    )
    after = job_status(first.job_id, config=config)
    assert forced.cache_hit is False
    assert (
        after["stages"]["metadata_ready"]["attempt_count"]
        == before["stages"]["metadata_ready"]["attempt_count"]
    )
    assert after["stages"]["subtitle_ready"]["attempt_count"] == 2
    assert after["stages"]["transcript_ready"]["attempt_count"] == 2


def test_failed_export_resumes_without_losing_raw(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "resume 字幕.srt"
    source.write_bytes(FIXTURE.read_bytes())
    config = _config(tmp_path)
    real_export = service_module.export_document
    calls = 0

    def fail_once(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated export interruption")
        return real_export(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(service_module, "export_document", fail_once)
    with pytest.raises(SubtitleError, match="export failed"):
        import_local_subtitle(source, config=config, language="zh-CN")
    job_dir = next(item for item in config.workspace_root.iterdir() if item.is_dir())
    archived = next((job_dir / "raw/subtitles/local").glob("*.original.srt"))
    assert archived.read_bytes() == FIXTURE.read_bytes()

    resumed = resume_to_transcript(job_dir.name, config=config)
    assert resumed.status == "transcript_ready"
    assert calls == 2
    report = job_status(job_dir.name, config=config)
    assert report["stages"]["subtitle_ready"]["attempt_count"] == 1
    assert report["stages"]["transcript_ready"]["attempt_count"] == 2


def test_resume_marks_interrupted_subtitle_stage_and_continues(tmp_path: Path) -> None:
    source = tmp_path / "interrupted 字幕.srt"
    source.write_bytes(FIXTURE.read_bytes())
    config = _config(tmp_path)
    result = import_local_subtitle(source, config=config, language="zh-CN")
    state_path = result.transcript_root.parent / "pipeline-state.json"
    state = load_state(state_path)
    invalidate_from(state, "transcript_ready")
    start_stage(
        state,
        "transcript_ready",
        input_hash="interrupted-input",
        config_hash="interrupted-config",
        tool="simulated-process",
        tool_version="test",
    )
    save_state(state_path, state)

    resumed = resume_to_transcript(result.job_id, config=config)
    report = job_status(result.job_id, config=config)
    assert resumed.status == "transcript_ready"
    assert report["stages"]["transcript_ready"]["status"] == "succeeded"
    events = (result.transcript_root.parent / "logs/events.jsonl").read_text()
    assert "stage_interrupted" in events


def test_tampered_result_is_reported_then_rebuilt(tmp_path: Path) -> None:
    source = tmp_path / "tampered 字幕.srt"
    source.write_bytes(FIXTURE.read_bytes())
    config = _config(tmp_path)
    result = import_local_subtitle(source, config=config, language="zh-CN")
    result_path = result.transcript_root / "subtitle-result.json"
    payload = json.loads(result_path.read_text())
    payload["selected_sources"] = {"local": "uploader"}
    result_path.write_text(json.dumps(payload), encoding="utf-8")

    status = job_status(result.job_id, config=config)
    assert status["subtitle_result"]["status"] == "invalid_result_file"
    with pytest.raises(StateError, match="audit hash"):
        inspect_subtitles(result.job_id, config=config)
    rebuilt = resume_to_transcript(result.job_id, config=config)
    assert rebuilt.cache_hit is False
    assert rebuilt.selected_sources == {"local": "local_file"}


def test_local_subtitle_bootstrap_crash_is_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "bootstrap 字幕.srt"
    source.write_bytes(FIXTURE.read_bytes())
    config = _config(tmp_path)
    real_new_state = pipeline_module.new_state

    def crash(job_id: str) -> object:
        raise RuntimeError("simulated bootstrap interruption")

    monkeypatch.setattr(pipeline_module, "new_state", crash)
    with pytest.raises(RuntimeError, match="bootstrap interruption"):
        import_local_subtitle(source, config=config, language="zh-CN")
    job_dir = next(item for item in config.workspace_root.iterdir() if item.is_dir())
    assert (job_dir / "manifest.json").is_file()
    assert not (job_dir / "pipeline-state.json").exists()

    monkeypatch.setattr(pipeline_module, "new_state", real_new_state)
    recovered = import_local_subtitle(source, config=config, language="zh-CN")
    assert recovered.status == "transcript_ready"
    assert "bootstrap_recovered" in (job_dir / "logs/events.jsonl").read_text()


def test_cli_offline_subtitle_smoke_with_chinese_space_path(tmp_path: Path) -> None:
    runner = CliRunner()
    source = tmp_path / "中文 课程 字幕.srt"
    source.write_bytes(FIXTURE.read_bytes())
    config_path = _config_file(tmp_path)

    first = runner.invoke(
        app,
        [
            "subtitles",
            "import",
            str(source),
            "--language",
            "zh-CN",
            "--config",
            str(config_path),
            "--json",
        ],
    )
    assert first.exit_code == 0, first.output
    first_payload = json.loads(first.stdout)
    assert first_payload["cache_hit"] is False
    assert first_payload["selected_sources"] == {"local": "local_file"}
    job_id = first_payload["job_id"]

    second = runner.invoke(
        app,
        [
            "subtitles",
            "import",
            str(source),
            "--language",
            "zh-CN",
            "--config",
            str(config_path),
            "--json",
        ],
    )
    assert second.exit_code == 0, second.output
    assert json.loads(second.stdout)["cache_hit"] is True

    for command in (
        ["subtitles", "inspect", job_id, "--config", str(config_path), "--json"],
        ["subtitles", "export", job_id, "--config", str(config_path), "--json"],
        ["status", job_id, "--config", str(config_path), "--json"],
        ["resume", job_id, "--config", str(config_path), "--json"],
    ):
        result = runner.invoke(app, command)
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["job_id"] == job_id

    one_shot = runner.invoke(
        app,
        [
            "run",
            str(source),
            "--until",
            "transcript_ready",
            "--config",
            str(config_path),
            "--json",
        ],
    )
    assert one_shot.exit_code == 0, one_shot.output
    assert json.loads(one_shot.stdout)["cache_hit"] is False

    for command in (
        ["subtitles", "--help"],
        ["subtitles", "fetch", "--help"],
        ["subtitles", "import", "--help"],
        ["run", "--help"],
    ):
        help_result = runner.invoke(app, command)
        assert help_result.exit_code == 0
        assert "Traceback" not in help_result.output
