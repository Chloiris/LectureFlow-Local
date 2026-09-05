from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import lectureflow.cli as cli
from lectureflow.cli import app
from lectureflow.constants import MILESTONES
from lectureflow.media.service import MediaAcquireResult
from lectureflow.schemas.frames import FrameRunResult

runner = CliRunner()


def _frame_result(*, cache_hit: bool = False) -> FrameRunResult:
    return FrameRunResult(
        job_id="local-fixture",
        status="frames_ready",
        cache_hit=cache_hit,
        candidate_count=9,
        deduplicated_count=5,
        final_count=4,
        frames_path="frames/frames.jsonl",
        contact_sheet_paths=["frames/contact-sheets/contact-sheet-001.jpg"],
        coverage_path="reports/frame-coverage-report.json",
    )


def test_media_cli_json_and_human_output(monkeypatch: pytest.MonkeyPatch) -> None:
    result = MediaAcquireResult("local-fixture", False, "media/media-manifest.json", None, None)
    monkeypatch.setattr(cli, "acquire_media", lambda *args, **kwargs: result)
    acquired = runner.invoke(app, ["media", "acquire", "local-fixture", "--json"])
    assert acquired.exit_code == 0
    assert json.loads(acquired.stdout)["status"] == "media_ready"

    report = {
        "job_id": "local-fixture",
        "manifest": {},
        "integrity": {"exists": True, "sha256_matches": True},
    }
    monkeypatch.setattr(cli, "inspect_media", lambda *args, **kwargs: report)
    inspected = runner.invoke(app, ["media", "inspect", "local-fixture"])
    assert inspected.exit_code == 0
    assert "Integrity: ok" in inspected.stdout


def test_frames_cli_commands_and_force_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "extract_job_frames", lambda *args, **kwargs: _frame_result())
    extracted = runner.invoke(app, ["frames", "extract", "local-fixture", "--json"])
    assert extracted.exit_code == 0
    assert json.loads(extracted.stdout)["final_count"] == 4

    invalid = runner.invoke(
        app,
        ["frames", "extract", "local-fixture", "--force-stage", "metadata_ready"],
    )
    assert invalid.exit_code == 2
    assert "only supports --force-stage frames_ready" in invalid.output

    monkeypatch.setattr(
        cli,
        "inspect_frames",
        lambda *args, **kwargs: {
            "integrity": True,
            "result": {"final_count": 4},
            "coverage": {},
        },
    )
    inspected = runner.invoke(app, ["frames", "inspect", "local-fixture"])
    assert "Frames: 4" in inspected.stdout

    monkeypatch.setattr(
        cli,
        "rebuild_contact_sheets",
        lambda *args, **kwargs: {
            "job_id": "local-fixture",
            "contact_sheet_paths": ["one.jpg", "two.jpg"],
            "frame_count": 4,
        },
    )
    contacts = runner.invoke(app, ["frames", "contact-sheet", "local-fixture"])
    assert contacts.exit_code == 0
    assert "Contact sheets: 2" in contacts.stdout


def test_resume_uses_frame_stage_when_media_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_manifest = tmp_path / "media-manifest.json"
    media_manifest.write_text("{}", encoding="utf-8")
    files = SimpleNamespace(media_manifest=media_manifest)
    stage = SimpleNamespace(status=SimpleNamespace(value="succeeded"))
    state = SimpleNamespace(stages={"frames_ready": stage})
    monkeypatch.setattr(cli, "load_job_context", lambda *args, **kwargs: (files, None, state))
    monkeypatch.setattr(
        cli, "extract_job_frames", lambda *args, **kwargs: _frame_result(cache_hit=True)
    )
    result = runner.invoke(app, ["resume", "local-fixture"])
    assert result.exit_code == 0
    assert "Frames: 4" in result.stdout
    assert "Cache: hit" in result.stdout


def test_status_human_output_includes_m2_artifacts(monkeypatch: pytest.MonkeyPatch) -> None:
    stages = {
        name: {"status": "pending", "attempt_count": 0, "latest_attempt": None}
        for name in MILESTONES
    }
    stages["frames_ready"] = {
        "status": "failed",
        "attempt_count": 1,
        "latest_attempt": {"error": "interrupted"},
    }
    report = {
        "job_id": "local-fixture",
        "overall_status": "failed",
        "last_successful_milestone": "metadata_ready",
        "stages": stages,
        "suspected_interruption": ["frames_ready"],
        "subtitle_result": {
            "status": "no_public_subtitles",
            "selected_sources": {"p1": None},
        },
        "media_result": {"status": "media_ready"},
        "frame_result": {"status": "frames_ready", "final_count": 4},
        "resume_command": "lectureflow resume local-fixture",
    }
    monkeypatch.setattr(cli, "job_status", lambda *args, **kwargs: report)
    result = runner.invoke(app, ["status", "local-fixture"])
    assert result.exit_code == 0
    assert "Media result: media_ready" in result.stdout
    assert "Frame result: frames_ready (4 final)" in result.stdout
    assert "Possible interrupted stage" in result.stdout


def test_run_bilibili_to_frames_and_rejects_invalid_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = SimpleNamespace(kind="bilibili")
    subtitle = SimpleNamespace(job_id="local-fixture")
    monkeypatch.setattr(cli, "identify_source", lambda value: descriptor)
    monkeypatch.setattr(cli, "fetch_bilibili_subtitles", lambda *args, **kwargs: subtitle)
    monkeypatch.setattr(cli, "extract_job_frames", lambda *args, **kwargs: _frame_result())
    result = runner.invoke(
        app,
        ["run", "BV1xx411c7mD", "--end", "300", "--until", "frames_ready", "--json"],
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "frames_ready"

    bad_until = runner.invoke(app, ["run", "BV1xx411c7mD", "--until", "notes_ready"])
    assert bad_until.exit_code == 2
    bad_force = runner.invoke(
        app,
        ["run", "BV1xx411c7mD", "--force-stage", "metadata_ready"],
    )
    assert bad_force.exit_code == 2
