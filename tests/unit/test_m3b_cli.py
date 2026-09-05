from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import lectureflow.cli as cli
from lectureflow.asr.service import ASRProcessResult
from lectureflow.cli import app

runner = CliRunner()


def _asr_result(*, cache_hit: bool = True) -> ASRProcessResult:
    return ASRProcessResult(
        job_id="job",
        status="transcript_ready",
        backend="mlx-whisper",
        model="mlx-community/whisper-medium-mlx",
        audio_cache_hit=True,
        model_cache_hit=True,
        transcript_cache_hit=cache_hit,
        segment_count=17,
        elapsed_seconds=29.1,
        real_time_factor=0.097,
        transcript_root="transcript/asr",
    )


def test_asr_cli_doctor_reports_local_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    report = {
        "backend": "mlx-whisper",
        "available": True,
        "python": "3.12.13",
        "mlx_version": "0.32.0",
        "metal_available": True,
        "mlx_whisper_version": "0.4.3",
        "model": "mlx-community/whisper-medium-mlx",
        "revision": "pinned",
        "model_cached": True,
        "error": None,
    }
    monkeypatch.setattr(cli, "load_config", lambda value=None: object())
    monkeypatch.setattr(cli, "asr_doctor", lambda config, offline, backend: report)
    result = runner.invoke(app, ["asr", "doctor"])
    assert result.exit_code == 0
    assert "Metal=True" in result.stdout
    assert "Model cached: True" in result.stdout


def test_asr_cli_doctor_fails_when_local_runtime_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = {
        "backend": "mlx-whisper",
        "available": False,
        "python": "3.12",
        "mlx_version": None,
        "metal_available": False,
        "mlx_whisper_version": None,
        "model": "model",
        "revision": "revision",
        "model_cached": False,
        "error": "strict offline model cache miss",
    }
    monkeypatch.setattr(cli, "load_config", lambda value=None: object())
    monkeypatch.setattr(cli, "asr_doctor", lambda config, offline, backend: report)
    result = runner.invoke(app, ["asr", "doctor", "--json"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["available"] is False


def test_asr_cli_preflight_transcribe_and_inspect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "load_config", lambda value=None: object())
    monkeypatch.setattr(
        cli,
        "preflight_job",
        lambda job_id, config, seconds, backend: {
            "status": "passed",
            "segment_count": 3,
            "elapsed_seconds": 6.0,
            "sample_text": "本地预检",
        },
    )
    monkeypatch.setattr(cli, "transcribe_job", lambda *args, **kwargs: _asr_result())
    monkeypatch.setattr(
        cli,
        "inspect_asr",
        lambda job_id, config: {
            "audio_ready": True,
            "transcript_integrity": True,
        },
    )

    preflight = runner.invoke(app, ["asr", "preflight", "job", "--seconds", "25"])
    transcribe = runner.invoke(
        app,
        ["asr", "transcribe", "job", "--offline", "--force-stage", "transcript_ready"],
    )
    inspected = runner.invoke(app, ["asr", "inspect", "job"])

    assert preflight.exit_code == 0 and "本地预检" in preflight.stdout
    assert transcribe.exit_code == 0 and "transcript=True" in transcribe.stdout
    assert inspected.exit_code == 0 and "Transcript integrity: True" in inspected.stdout


def test_asr_cli_exposes_vibeasr_bitnet_without_enabling_downloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(cli, "load_config", lambda value=None: object())
    monkeypatch.setattr(
        cli,
        "asr_doctor",
        lambda config, offline, backend: {
            "backend": backend,
            "available": True,
            "python": "3.12",
            "executable": "/local/asr_infer",
            "threads": 4,
            "model": "microsoft/VibeVoice-ASR-BitNet",
            "revision": "pinned",
            "model_cached": True,
            "error": None,
        },
    )

    def transcribe(*args: object, **kwargs: object) -> ASRProcessResult:
        calls.append(kwargs)
        return _asr_result()

    monkeypatch.setattr(cli, "transcribe_job", transcribe)
    doctor = runner.invoke(app, ["asr", "doctor", "--backend", "vibeasr-bitnet"])
    result = runner.invoke(
        app,
        ["asr", "transcribe", "job", "--backend", "vibeasr-bitnet", "--offline"],
    )
    assert doctor.exit_code == 0 and "vibeasr-bitnet" in doctor.stdout
    assert result.exit_code == 0
    assert calls[0]["backend"] == "vibeasr-bitnet"


@pytest.mark.parametrize(
    "arguments",
    [
        ["--backend", "faster-whisper"],
        ["--start", "1"],
        ["--end", "299"],
        ["--force-stage", "frames_ready"],
    ],
)
def test_asr_cli_rejects_out_of_scope_options(arguments: list[str]) -> None:
    result = runner.invoke(app, ["asr", "transcribe", "job", *arguments])
    assert result.exit_code == 2
    assert "Error:" in result.stderr


def test_packet_and_evidence_cli_entrypoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = tmp_path / "notes/P0001-five-minute-integrated-note.md"
    note.parent.mkdir()
    note.write_text("note")
    files = SimpleNamespace(root=tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda value=None: object())
    monkeypatch.setattr(cli, "load_job_context", lambda *args, **kwargs: (files, None, None))
    monkeypatch.setattr(
        cli,
        "build_packet",
        lambda job_id, config: {"packet_id": "P0001", "cache_hit": True},
    )
    monkeypatch.setattr(
        cli,
        "validate_packet",
        lambda root, packet_id: {"packet_id": packet_id, "valid": True},
    )
    monkeypatch.setattr(
        cli,
        "validate_analysis",
        lambda root: {"packet_id": "P0001", "valid": True},
    )
    monkeypatch.setattr(
        cli,
        "validate_job_evidence",
        lambda root: {"valid": True, "evidence_count": 9},
    )
    recorded: list[str] = []
    monkeypatch.setattr(
        cli,
        "record_validated_artifact_stage",
        lambda job_id, **kwargs: recorded.append(kwargs["stage"]),
    )

    built = runner.invoke(app, ["packet", "build", "job"])
    alias = runner.invoke(app, ["build-packets", "job", "--json"])
    packet_valid = runner.invoke(app, ["packet", "validate", "job", "P0001"])
    analysis_valid = runner.invoke(app, ["validate", "job"])
    evidence_valid = runner.invoke(app, ["evidence", "validate", "job"])

    assert built.exit_code == 0 and "Cache: hit" in built.stdout
    assert alias.exit_code == 0 and json.loads(alias.stdout)["packet_id"] == "P0001"
    assert packet_valid.exit_code == 0 and "valid" in packet_valid.stdout
    assert analysis_valid.exit_code == 0 and json.loads(analysis_valid.stdout)["valid"]
    assert evidence_valid.exit_code == 0 and "9 records" in evidence_valid.stdout
    assert recorded == ["analysis_ready", "evidence_ready", "notes_ready"]


def test_packet_cli_rejects_non_m3b_range() -> None:
    result = runner.invoke(app, ["packet", "build", "job", "--start", "1"])
    assert result.exit_code == 2
    assert "restricted to 0–300" in result.stderr


def test_resume_recognizes_only_mlx_transcript_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = SimpleNamespace(tool="mlx-whisper")
    stage = SimpleNamespace(status=SimpleNamespace(value="succeeded"), attempts=[attempt])
    frames_stage = SimpleNamespace(status=SimpleNamespace(value="pending"))
    files = SimpleNamespace(root=tmp_path, media_manifest=tmp_path / "missing.json")
    state = SimpleNamespace(stages={"transcript_ready": stage, "frames_ready": frames_stage})
    monkeypatch.setattr(cli, "load_config", lambda value=None: object())
    monkeypatch.setattr(cli, "load_job_context", lambda *args, **kwargs: (files, None, state))
    monkeypatch.setattr(cli, "transcribe_job", lambda *args, **kwargs: _asr_result())

    result = runner.invoke(app, ["resume", "job", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["transcript_cache_hit"] is True
