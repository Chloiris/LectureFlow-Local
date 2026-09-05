from __future__ import annotations

import json
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

import lectureflow.asr.base as base_module
import lectureflow.asr.service as service
import lectureflow.asr.vibeasr_backend as vibe_module
from lectureflow.asr.base import detect_asr_capabilities, require_asr_backend
from lectureflow.asr.service import VibeASRRuntime
from lectureflow.asr.vibeasr_backend import (
    VIBEASR_LM_BYTES,
    VIBEASR_LM_FILENAME,
    VIBEASR_LM_SHA256,
    VIBEASR_MODEL_BYTES,
    VIBEASR_VAE_BYTES,
    VIBEASR_VAE_FILENAME,
    VIBEASR_VAE_SHA256,
    VibeASRBitNetBackend,
)
from lectureflow.config import load_config
from lectureflow.errors import ASRUnavailableError, ConfigurationError
from lectureflow.process import CommandResult
from lectureflow.schemas.asr import ASRTranscriptResult, TranscriptionRequest
from lectureflow.schemas.transcript import TranscriptSegment


def _runtime_files(tmp_path: Path) -> tuple[Path, Path]:
    executable = tmp_path / "运行 时/asr_infer"
    executable.parent.mkdir(parents=True)
    executable.write_text("fixture")
    executable.chmod(0o755)
    model = tmp_path / "模型 目录"
    model.mkdir()
    with (model / VIBEASR_VAE_FILENAME).open("wb") as handle:
        handle.truncate(VIBEASR_VAE_BYTES)
    with (model / VIBEASR_LM_FILENAME).open("wb") as handle:
        handle.truncate(VIBEASR_LM_BYTES)
    return executable, model


def _pcm_wave(path: Path, *, seconds: int = 2, sample_rate: int = 24000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\0\0" * sample_rate * seconds)


def test_vibeasr_config_is_explicit_and_rejects_unsafe_context(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path)
    assert config.asr.vibeasr_model == "microsoft/VibeVoice-ASR-BitNet"
    assert config.asr.vibeasr_model_path == ""
    assert config.asr.vibeasr_threads == 4
    unsafe = tmp_path / "unsafe.toml"
    unsafe.write_text('[asr]\nvibeasr_hotwords = ["line\\nbreak"]\n')
    with pytest.raises(ConfigurationError, match="single-line"):
        load_config(unsafe)


def test_vibeasr_doctor_never_downloads_and_explains_missing_runtime(tmp_path: Path) -> None:
    report = service.asr_doctor(load_config(cwd=tmp_path), offline=True, backend="vibeasr-bitnet")
    assert report["available"] is False
    assert report["automatic_download_enabled"] is False
    assert report["model_size_bytes"] == VIBEASR_MODEL_BYTES
    assert "does not clone or build" in report["error"]


def test_vibeasr_doctor_and_backend_factory_report_pinned_local_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "asr_infer"
    executable.write_text("binary")
    model = tmp_path / "model"
    runtime = VibeASRRuntime(
        executable=executable,
        model_path=model,
        vae_path=model / VIBEASR_VAE_FILENAME,
        lm_path=model / VIBEASR_LM_FILENAME,
        executable_sha256="e" * 64,
        runtime_revision="5cbce71c65911a7e10639ac13b6ab6929e4c8f9e",
        vae_sha256=VIBEASR_VAE_SHA256,
        lm_sha256=VIBEASR_LM_SHA256,
    )
    monkeypatch.setattr(service, "resolve_vibeasr_runtime", lambda *args, **kwargs: runtime)
    config = load_config(cwd=tmp_path)
    report = service.asr_doctor(config, backend="vibeasr-bitnet")
    backend, path, hit, returned_runtime, raw_name, details, version = service._build_backend(
        config, "vibeasr-bitnet", offline=True
    )
    assert report["available"] is True
    assert report["executable_sha256"] == "e" * 64
    assert isinstance(backend, VibeASRBitNetBackend)
    assert path == model and hit is True and returned_runtime == runtime
    assert raw_name == "raw-vibeasr-bitnet.json"
    assert details["automatic_download_enabled"] is False
    assert version == f"sha256:{'e' * 12}"


def test_vibeasr_runtime_requires_pinned_two_file_integrity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable, model = _runtime_files(tmp_path)
    config_path = tmp_path / "vibe.toml"
    config_path.write_text(
        f'[asr]\nvibeasr_executable = "{executable}"\n'
        f'vibeasr_executable_sha256 = "{"e" * 64}"\n'
        f'vibeasr_model_path = "{model}"\n'
    )
    config = load_config(config_path)

    def digest(path: Path) -> str:
        return {
            VIBEASR_VAE_FILENAME: VIBEASR_VAE_SHA256,
            VIBEASR_LM_FILENAME: VIBEASR_LM_SHA256,
        }.get(path.name, "e" * 64)

    monkeypatch.setattr(service, "hash_file", digest)
    runtime = service.resolve_vibeasr_runtime(config)
    assert runtime.model_path == model
    assert runtime.vae_sha256 == VIBEASR_VAE_SHA256
    with (model / VIBEASR_LM_FILENAME).open("r+b") as handle:
        handle.truncate(VIBEASR_LM_BYTES - 1)
    with pytest.raises(ASRUnavailableError, match="size mismatch"):
        service.resolve_vibeasr_runtime(config)


def test_vibeasr_resolver_rejects_unpinned_identity_and_binary(
    tmp_path: Path,
) -> None:
    config = load_config(cwd=tmp_path)
    wrong_model = config.model_copy(
        update={"asr": config.asr.model_copy(update={"vibeasr_model": "other/model"})}
    )
    with pytest.raises(ASRUnavailableError, match="official model"):
        service.resolve_vibeasr_runtime(wrong_model)
    wrong_runtime = config.model_copy(
        update={"asr": config.asr.model_copy(update={"vibeasr_runtime_revision": "0" * 40})}
    )
    with pytest.raises(ASRUnavailableError, match="source revision"):
        service.resolve_vibeasr_runtime(wrong_runtime)

    executable = tmp_path / "asr_infer"
    executable.write_text("binary")
    executable.chmod(0o755)
    configured = config.model_copy(
        update={"asr": config.asr.model_copy(update={"vibeasr_executable": str(executable)})}
    )
    with pytest.raises(ASRUnavailableError, match="SHA-256 is not configured"):
        service.resolve_vibeasr_runtime(configured)
    wrong_sha = configured.model_copy(
        update={"asr": configured.asr.model_copy(update={"vibeasr_executable_sha256": "f" * 64})}
    )
    with pytest.raises(ASRUnavailableError, match="does not match"):
        service.resolve_vibeasr_runtime(wrong_sha)
    correct_sha = configured.model_copy(
        update={
            "asr": configured.asr.model_copy(
                update={"vibeasr_executable_sha256": service.hash_file(executable)}
            )
        }
    )
    with pytest.raises(ASRUnavailableError, match="model directory is not configured"):
        service.resolve_vibeasr_runtime(correct_sha)
    with pytest.raises(ASRUnavailableError, match="supports mlx-whisper"):
        service.asr_doctor(config, backend="cloud-asr")


def test_capability_requires_full_pin_and_auto_never_selects_vibeasr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable, model = _runtime_files(tmp_path)
    config_path = tmp_path / "configured.toml"
    config_path.write_text(
        "[asr]\n"
        'preferred_backend = "vibeasr-bitnet"\n'
        f'vibeasr_executable = "{executable}"\n'
        f'vibeasr_executable_sha256 = "{"e" * 64}"\n'
        f'vibeasr_model_path = "{model}"\n'
    )
    config = load_config(config_path)

    def digest(path: Path) -> str:
        return {
            VIBEASR_VAE_FILENAME: VIBEASR_VAE_SHA256,
            VIBEASR_LM_FILENAME: VIBEASR_LM_SHA256,
        }.get(path.name, "e" * 64)

    monkeypatch.setattr(base_module, "hash_file", digest)
    monkeypatch.setattr(base_module.importlib.util, "find_spec", lambda module: None)
    capabilities = detect_asr_capabilities(config.asr)
    assert capabilities[-1].available is True
    assert require_asr_backend(config.asr).backend == "vibeasr_bitnet"
    automatic = config.asr.model_copy(update={"preferred_backend": "auto"})
    with pytest.raises(ASRUnavailableError, match="No configured local ASR"):
        require_asr_backend(automatic)


def test_vibeasr_backend_maps_plain_text_to_deterministic_chunk_without_confidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable, model = _runtime_files(tmp_path)
    audio = tmp_path / "课程 音频.wav"
    _pcm_wave(audio)
    calls: list[tuple[str, ...]] = []

    def command(argv: object, **kwargs: object) -> CommandResult:
        normalized = tuple(str(item) for item in argv)  # type: ignore[arg-type]
        calls.append(normalized)
        return CommandResult(
            argv=normalized,
            returncode=0,
            stdout="\n大语言模型与 Scaling Law\n",
            stderr="Audio: 2.00s | Tokens: 12 | RTF: 0.4200",
        )

    monkeypatch.setattr(vibe_module, "run_command", command)
    backend = VibeASRBitNetBackend(
        executable=executable,
        model="microsoft/VibeVoice-ASR-BitNet",
        revision="pinned",
        model_path=model,
    )
    description = backend.describe()
    assert description.available is True
    assert description.package_version.startswith("sha256:")
    raw = tmp_path / "输出/raw.json"
    result = backend.transcribe(
        TranscriptionRequest(
            job_id="job",
            audio_path=audio,
            raw_output_path=raw,
            start=0,
            end=2,
            language="zh",
            model="microsoft/VibeVoice-ASR-BitNet",
            model_path=model,
            part_id="P1",
            context="Scaling Law, 大语言模型",
        )
    )
    assert [item.speaker for item in result.segments] == [None]
    assert all(item.source == "vibeasr_bitnet" for item in result.segments)
    assert all(item.confidence is None for item in result.segments)
    assert [(item.start, item.end) for item in result.segments] == [(0.0, 2.0)]
    assert result.segments[0].source_ref["timestamp_basis"] == ("deterministic_audio_chunk_bounds")
    assert "--prompt-format" in calls[0] and "text" in calls[0]
    assert calls[0][calls[0].index("-c") + 1] == "16384"
    assert "--context" in calls[0]
    payload = json.loads(raw.read_text())
    assert payload["chunks"][0]["stdout"].startswith("\n大语言模型")
    assert payload["chunks"][0]["reported_rtf"] == 0.42
    assert payload["model_timestamps_available"] is False
    assert payload["speaker_diarization_available"] is False
    assert "Scaling Law" not in json.dumps(payload["context_sha256"])
    assert payload["external_model_api_used"] is False


def test_vibeasr_backend_slices_preflight_without_faking_timestamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable, model = _runtime_files(tmp_path)
    audio = tmp_path / "audio.wav"
    _pcm_wave(audio, seconds=2)
    observed: dict[str, object] = {}

    def command(argv: object, **kwargs: object) -> CommandResult:
        normalized = tuple(str(item) for item in argv)  # type: ignore[arg-type]
        sliced = Path(normalized[normalized.index("--audio") + 1])
        observed["path"] = sliced
        observed["duration"] = vibe_module._wave_facts(sliced)[0]
        return CommandResult(normalized, 0, "预检文本", "Tokens: 5 | RTF: 0.5")

    monkeypatch.setattr(vibe_module, "run_command", command)
    backend = VibeASRBitNetBackend(
        executable=executable,
        model="microsoft/VibeVoice-ASR-BitNet",
        revision="pinned",
        model_path=model,
    )
    result = backend.transcribe(
        TranscriptionRequest(
            job_id="job",
            audio_path=audio,
            raw_output_path=tmp_path / "raw.json",
            start=0,
            end=1,
            language="zh",
            model="microsoft/VibeVoice-ASR-BitNet",
            model_path=model,
            part_id="P1",
        )
    )
    assert observed["duration"] == 1.0
    assert not Path(observed["path"]).exists()  # type: ignore[arg-type]
    assert result.segments[0].end == 1


def test_vibeasr_rejects_context_overflow_runtime_failure_and_token_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable, model = _runtime_files(tmp_path)
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"fixture")
    backend = VibeASRBitNetBackend(
        executable=executable,
        model="microsoft/VibeVoice-ASR-BitNet",
        revision="pinned",
        model_path=model,
        chunk_seconds=5,
        context_size=4096,
        max_tokens=4096,
    )
    monkeypatch.setattr(vibe_module, "_wave_facts", lambda path: (5.0, 24000, 1, 2))
    long_request = TranscriptionRequest(
        job_id="job",
        audio_path=audio,
        raw_output_path=tmp_path / "raw.json",
        start=0,
        end=5,
        language="zh",
        model="microsoft/VibeVoice-ASR-BitNet",
        model_path=model,
        part_id="P1",
    )
    with pytest.raises(ASRUnavailableError, match="context budget"):
        backend.transcribe(long_request)

    backend.context_size = 16384
    monkeypatch.setattr(vibe_module, "_wave_facts", lambda path: (2.0, 24000, 1, 2))
    short_request = long_request.model_copy(update={"end": 2})
    monkeypatch.setattr(
        vibe_module,
        "run_command",
        lambda *args, **kwargs: CommandResult(("asr_infer",), 0, "partial", "llama_decode error"),
    )
    with pytest.raises(ASRUnavailableError, match="fatal runtime"):
        backend.transcribe(short_request)
    monkeypatch.setattr(
        vibe_module,
        "run_command",
        lambda *args, **kwargs: CommandResult(
            ("asr_infer",), 0, "", "Error: VAE acoustic encoding failed"
        ),
    )
    with pytest.raises(ASRUnavailableError, match="fatal runtime"):
        backend.transcribe(short_request)
    monkeypatch.setattr(
        vibe_module,
        "run_command",
        lambda *args, **kwargs: CommandResult(
            ("asr_infer",), 0, "partial", "Tokens: 4096 | RTF: 0.4"
        ),
    )
    with pytest.raises(ASRUnavailableError, match="output token limit"):
        backend.transcribe(short_request)
    monkeypatch.setattr(
        vibe_module,
        "run_command",
        lambda *args, **kwargs: CommandResult(("asr_infer",), 0, "", "Tokens: 1 | RTF: 0.4"),
    )
    with pytest.raises(ASRUnavailableError, match="no transcript text"):
        backend.transcribe(short_request)


def test_vibeasr_chunks_long_input_at_audited_plain_text_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable, model = _runtime_files(tmp_path)
    audio = tmp_path / "audio.wav"
    _pcm_wave(audio, seconds=65)
    calls = 0

    def command(*args: object, **kwargs: object) -> CommandResult:
        nonlocal calls
        calls += 1
        return CommandResult(
            ("asr_infer",),
            0,
            "" if calls == 2 else f"chunk {calls}",
            "Tokens: 10 | RTF: 0.4",
        )

    monkeypatch.setattr(vibe_module, "run_command", command)
    backend = VibeASRBitNetBackend(
        executable=executable,
        model="microsoft/VibeVoice-ASR-BitNet",
        revision="pinned",
        model_path=model,
    )
    result = backend.transcribe(
        TranscriptionRequest(
            job_id="job",
            audio_path=audio,
            raw_output_path=tmp_path / "raw.json",
            start=0,
            end=65,
            language="zh",
            model="microsoft/VibeVoice-ASR-BitNet",
            model_path=model,
            part_id="P1",
        )
    )
    assert calls == 3
    assert [(item.start, item.end) for item in result.segments] == [
        (0.0, 30.0),
        (60.0, 65),
    ]
    payload = json.loads((tmp_path / "raw.json").read_text())
    assert payload["chunks"][1]["empty_text"] is True


def test_vibeasr_backend_selection_is_explicit_and_deterministic(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path)
    assert service._select_backend(config, "vibeasr_bitnet") == "vibeasr-bitnet"
    assert service._select_backend(config, None) == "mlx-whisper"
    with pytest.raises(ASRUnavailableError, match="Runnable local ASR"):
        service._select_backend(config, "cloud-asr")


def test_first_transcript_attempt_locks_backend_across_failure_and_resume() -> None:
    failed_vibe = SimpleNamespace(
        stages={
            "transcript_ready": SimpleNamespace(attempts=[SimpleNamespace(tool="vibeasr-bitnet")])
        }
    )
    assert service._locked_transcript_backend(failed_vibe) == "vibeasr-bitnet"
    conflict = SimpleNamespace(
        stages={
            "transcript_ready": SimpleNamespace(
                attempts=[
                    SimpleNamespace(tool="vibeasr-bitnet"),
                    SimpleNamespace(tool="mlx-whisper"),
                ]
            )
        }
    )
    with pytest.raises(service.StateError, match="conflicting ASR"):
        service._locked_transcript_backend(conflict)


def test_preflight_refuses_cross_backend_before_creating_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = SimpleNamespace(root=tmp_path)
    state = SimpleNamespace(
        stages={"transcript_ready": SimpleNamespace(attempts=[SimpleNamespace(tool="mlx-whisper")])}
    )
    monkeypatch.setattr(service, "load_job_context", lambda *args, **kwargs: (files, None, state))
    called = False

    def audio(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("audio preparation must not run")

    monkeypatch.setattr(service, "ensure_asr_audio", audio)
    with pytest.raises(ASRUnavailableError, match="locked to backend"):
        service.preflight_job(
            "job",
            config=load_config(cwd=tmp_path),
            backend="vibeasr-bitnet",
        )
    assert called is False


def test_concurrent_same_backend_completion_becomes_cache_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = tmp_path / "transcript/asr/asr-manifest.json"
    files = SimpleNamespace(
        root=tmp_path,
        lock=tmp_path / ".lock",
        state=tmp_path / "pipeline-state.json",
        events=tmp_path / "events.jsonl",
    )
    manifest = SimpleNamespace(
        requested_range=SimpleNamespace(start=0.0, end=300.0),
        source=SimpleNamespace(page=1),
    )
    state = SimpleNamespace(
        stages={
            "transcript_ready": SimpleNamespace(
                status=service.StageStatus.SUCCEEDED,
                attempts=[],
            )
        }
    )
    monkeypatch.setattr(
        service, "load_job_context", lambda *args, **kwargs: (files, manifest, state)
    )
    monkeypatch.setattr(service, "load_state", lambda path: state)
    monkeypatch.setattr(
        service,
        "ensure_asr_audio",
        lambda *args, **kwargs: (
            tmp_path / "audio.wav",
            {"sha256": "a" * 64},
            True,
        ),
    )
    backend = SimpleNamespace(is_available=lambda: True)
    monkeypatch.setattr(
        service,
        "_build_backend",
        lambda *args, **kwargs: (
            backend,
            tmp_path / "model",
            True,
            None,
            "raw-mlx-whisper.json",
            {},
            "version",
        ),
    )
    monkeypatch.setattr(service, "_asr_input_hash", lambda *args, **kwargs: "input")
    cached = {
        "model": "model",
        "segment_count": 2,
        "elapsed_seconds": 1.0,
        "real_time_factor": 0.01,
    }
    cache_calls = 0

    def cache(*args: object, **kwargs: object) -> dict[str, object] | None:
        nonlocal cache_calls
        cache_calls += 1
        return None if cache_calls == 1 else cached

    monkeypatch.setattr(service, "_cached_transcript", cache)
    monkeypatch.setattr(service, "record_transition", lambda *args, **kwargs: None)

    class Lock:
        def __init__(self, path: Path) -> None:
            self.path = path

        def __enter__(self) -> Lock:
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text('{"backend":"mlx-whisper","input_hash":"input"}')
            return self

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(service, "JobLock", Lock)
    result = service.transcribe_job(
        "job", config=load_config(cwd=tmp_path), backend="mlx-whisper", offline=True
    )
    assert result.transcript_cache_hit is True
    assert result.segment_count == 2


def test_vibeasr_cache_key_tracks_binary_models_and_hotwords(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path)
    runtime = VibeASRRuntime(
        executable=tmp_path / "asr_infer",
        model_path=tmp_path / "model",
        vae_path=tmp_path / "vae.gguf",
        lm_path=tmp_path / "lm.gguf",
        executable_sha256="e" * 64,
        runtime_revision="5cbce71c65911a7e10639ac13b6ab6929e4c8f9e",
        vae_sha256="v" * 64,
        lm_sha256="l" * 64,
    )
    audio = {"sha256": "a" * 64}
    first = service._asr_input_hash(audio, config, backend="vibeasr-bitnet", runtime=runtime)
    changed = config.model_copy(
        update={"asr": config.asr.model_copy(update={"vibeasr_hotwords": ["Scaling Law"]})}
    )
    second = service._asr_input_hash(audio, changed, backend="vibeasr-bitnet", runtime=runtime)
    assert first != second


def test_vibeasr_uses_a_separate_24khz_audio_cache_path(tmp_path: Path) -> None:
    mlx_audio, _ = service._audio_paths(tmp_path, 300)
    vibe_audio, _ = service._audio_paths(tmp_path, 300, backend="vibeasr-bitnet")
    assert mlx_audio != vibe_audio
    assert "vibeasr-bitnet" in vibe_audio.name


def test_vibeasr_artifacts_preserve_raw_speaker_timeline_and_backend(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "transcript/asr"
    destination.mkdir(parents=True)
    (destination / "raw-vibeasr-bitnet.json").write_text('{"stdout":"raw"}')
    result = ASRTranscriptResult(
        backend="vibeasr-bitnet",
        model="microsoft/VibeVoice-ASR-BitNet",
        model_revision="pinned",
        language="zh",
        duration_seconds=2,
        segments=[
            TranscriptSegment(
                segment_id="T000001",
                start=0,
                end=2,
                text_raw="Scaling Law",
                text_clean="Scaling Law",
                source="vibeasr_bitnet",
                language="zh",
                speaker=None,
                part_id="P1",
            )
        ],
        raw_output_path="transcript/asr/raw-vibeasr-bitnet.json",
        elapsed_seconds=1,
        real_time_factor=0.5,
    )
    stored = service._write_transcript_artifacts(
        tmp_path,
        result,
        input_hash="input",
        audio_manifest={"path": "media/audio/input.wav", "sha256": "a" * 64},
        model_path=tmp_path / "model",
        model_cache_hit=True,
        raw_output_filename="raw-vibeasr-bitnet.json",
        runtime_details={"automatic_download_enabled": False},
    )
    original = json.loads((destination / "original.jsonl").read_text())
    assert stored["backend"] == "vibeasr-bitnet"
    assert stored["raw_output_path"].endswith("raw-vibeasr-bitnet.json")
    assert stored["model_size_bytes"] == VIBEASR_MODEL_BYTES
    assert original["source"] == "vibeasr_bitnet"
    assert original["speaker"] is None
    assert (
        json.loads((destination / "asr-runtime.json").read_text())["automatic_download_enabled"]
        is False
    )


def test_immutable_original_refuses_different_retry_content(tmp_path: Path) -> None:
    original = tmp_path / "original.jsonl"
    service._write_immutable_text(original, "first\n")
    service._write_immutable_text(original, "first\n")
    with pytest.raises(ASRUnavailableError, match="Immutable ASR artifact"):
        service._write_immutable_text(original, "second\n")
    assert original.read_text() == "first\n"
