from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import lectureflow.cli as cli_module
import lectureflow.doctor as doctor_module
from lectureflow.cli import app

runner = CliRunner()


def test_doctor_reports_required_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_module.shutil, "which", lambda name: None)
    report = doctor_module.run_doctor()
    assert report["ok"] is False
    failures = {item["name"] for item in report["checks"] if item["status"] == "fail"}
    assert {"ffmpeg", "ffprobe"} <= failures
    assert report["privacy"]["network_used"] is False


def test_doctor_rejects_executable_whose_version_command_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor_module.shutil, "which", lambda name: f"/fake/{name}")
    monkeypatch.setattr(doctor_module, "first_version_line", lambda *args: None)
    report = doctor_module.run_doctor()
    checks = {item["name"]: item for item in report["checks"]}
    assert checks["ffmpeg"]["status"] == "fail"
    assert checks["ffprobe"]["status"] == "fail"
    assert checks["yt-dlp"]["status"] == "warn"
    assert report["ok"] is False


def test_doctor_json_stdout_is_machine_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    report = {
        "schema_version": "1.0.0",
        "ok": True,
        "strict_ok": True,
        "checks": [],
        "privacy": {},
    }
    monkeypatch.setattr(cli_module, "run_doctor", lambda workspace_root=None: report)
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == report


def test_missing_config_has_concise_error_without_traceback(tmp_path: Path) -> None:
    result = runner.invoke(app, ["doctor", "--config", str(tmp_path / "missing.toml")])
    assert result.exit_code == 2
    assert "Configuration file does not exist" in result.output
    assert "Traceback" not in result.output


def test_inverted_range_has_concise_error_without_traceback(tmp_path: Path) -> None:
    source = tmp_path / "video.mp4"
    source.write_bytes(b"fixture")
    result = runner.invoke(
        app,
        ["prepare", str(source), "--start", "2", "--end", "1"],
    )
    assert result.exit_code == 2
    assert "Invalid requested time range" in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize(
    "command",
    [
        "doctor",
        "prepare",
        "status",
        "transcribe",
        "extract-frames",
        "build-packets",
        "validate",
        "render",
        "export-obsidian",
        "serve",
        "resume",
        "run",
    ],
)
def test_all_required_commands_have_help(command: str) -> None:
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0, result.output


def test_cli_version_exits_without_requiring_a_command() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "0.3.0"
