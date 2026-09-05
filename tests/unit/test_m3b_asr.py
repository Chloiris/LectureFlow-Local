from __future__ import annotations

import builtins
import json
import sys
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from pydantic import ValidationError

import lectureflow.asr.mlx_backend as mlx_module
import lectureflow.asr.service as service
from lectureflow.asr.base import detect_asr_capabilities, require_asr_backend
from lectureflow.asr.mlx_backend import MLXWhisperBackend
from lectureflow.config import load_config
from lectureflow.errors import ASRUnavailableError
from lectureflow.schemas.asr import ASRTranscriptResult, TranscriptionRequest
from lectureflow.schemas.transcript import TranscriptSegment


def _segment(start: float = 0.0, end: float = 2.0) -> TranscriptSegment:
    return TranscriptSegment(
        segment_id="T000001",
        start=start,
        end=end,
        text_raw=" 原始 文本 ",
        text_clean="原始 文本",
        source="mlx_whisper",
        language="zh",
        part_id="P1",
    )


def test_mlx_backend_unavailable_has_clear_error(tmp_path: Path) -> None:
    backend = MLXWhisperBackend(
        model="mlx-community/whisper-medium-mlx",
        revision="revision",
        model_path=tmp_path / "missing",
    )
    request = TranscriptionRequest(
        job_id="job",
        audio_path=tmp_path / "audio.wav",
        raw_output_path=tmp_path / "raw.json",
        start=0,
        end=2,
        language="zh",
        model="mlx-community/whisper-medium-mlx",
        model_path=tmp_path / "missing",
        part_id="P1",
    )
    with pytest.raises(ASRUnavailableError, match="unavailable or the pinned model"):
        backend.transcribe(request)


def test_optional_huggingface_dependency_is_loaded_only_for_asr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def missing_optional(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("huggingface_hub"):
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_optional)
    with pytest.raises(ASRUnavailableError, match="extra asr-mlx"):
        service.snapshot_download(repo_id="model", local_files_only=True)


def test_strict_offline_missing_model_fails_without_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(cwd=tmp_path)

    def missing(**kwargs: object) -> str:
        assert kwargs["local_files_only"] is True
        raise service.LocalEntryNotFoundError("missing")

    monkeypatch.setattr(service, "snapshot_download", missing)
    with pytest.raises(ASRUnavailableError, match="strict offline model cache miss"):
        service.resolve_mlx_model(config, offline=True)


def test_audio_cache_hit_verifies_hash(tmp_path: Path) -> None:
    audio = tmp_path / "路径 含 空格/audio.wav"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"audio")
    manifest = tmp_path / "audio.json"
    manifest.write_text(
        json.dumps(
            {
                "input_hash": "input",
                "sha256": service.hash_file(audio),
                "sample_rate": 16000,
                "channels": 1,
            }
        )
    )
    assert service._audio_cache_valid(audio, manifest, "input")
    audio.write_bytes(b"changed")
    assert not service._audio_cache_valid(audio, manifest, "input")


def test_transcript_cache_hit_and_immutable_original(tmp_path: Path) -> None:
    root = tmp_path
    destination = root / "transcript/asr"
    destination.mkdir(parents=True)
    files = [
        "raw-mlx-whisper.json",
        "original.jsonl",
        "cleaned.jsonl",
        "transcript.txt",
        "transcript.srt",
        "transcript.vtt",
        "asr-runtime.json",
        "asr-quality-report.json",
    ]
    artifacts = {}
    for name in files:
        path = destination / name
        path.write_text(name)
        artifacts[f"transcript/asr/{name}"] = service.hash_file(path)
    manifest = {
        "input_hash": "input",
        "immutable_original_sha256": service.hash_file(destination / "original.jsonl"),
        "artifacts": artifacts,
    }
    (destination / "asr-manifest.json").write_text(json.dumps(manifest))
    assert service._cached_transcript(root, "input") == manifest
    (destination / "cleaned.jsonl").write_text("semantic rewrite")
    assert service._cached_transcript(root, "input") is None
    (destination / "cleaned.jsonl").write_text("cleaned.jsonl")
    (destination / "original.jsonl").write_text("overwritten")
    assert service._cached_transcript(root, "input") is None


def test_segment_time_outside_duration_fails() -> None:
    with pytest.raises(ValidationError, match="exceeds requested duration"):
        ASRTranscriptResult(
            backend="mlx-whisper",
            model="model",
            model_revision="revision",
            language="zh",
            duration_seconds=1,
            segments=[_segment(end=2)],
            raw_output_path="raw.json",
            elapsed_seconds=1,
            real_time_factor=1,
        )


def test_model_name_changes_asr_cache_key(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path)
    audio = {"sha256": "a" * 64}
    original = service._asr_input_hash(audio, config)
    changed = config.model_copy(
        update={"asr": config.asr.model_copy(update={"mlx_model": "different/model"})}
    )
    assert service._asr_input_hash(audio, changed) != original


def test_note_template_does_not_change_asr_cache_key(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path)
    audio = {"sha256": "a" * 64}
    before = service._asr_input_hash(audio, config)
    (tmp_path / "笔记 模板.md").write_text("changed")
    assert service._asr_input_hash(audio, config) == before


def test_frame_config_does_not_change_asr_cache_key(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path)
    audio = {"sha256": "a" * 64}
    changed = config.model_copy(
        update={
            "frames": config.frames.model_copy(
                update={"scene_threshold": config.frames.scene_threshold + 0.01}
            )
        }
    )
    assert service._asr_input_hash(audio, changed) == service._asr_input_hash(audio, config)


def test_backend_preserves_raw_text_and_does_not_invent_confidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}")
    (model / "weights.npz").write_bytes(b"weights")
    backend = MLXWhisperBackend(model="model", revision="revision", model_path=model)
    monkeypatch.setattr(mlx_module.importlib.util, "find_spec", lambda name: object())
    fake = type(
        "FakeMLXWhisper",
        (),
        {
            "transcribe": staticmethod(
                lambda *args, **kwargs: {
                    "segments": [{"start": 0, "end": 2, "text": " 原始  文本 "}]
                }
            )
        },
    )
    monkeypatch.setitem(__import__("sys").modules, "mlx_whisper", fake)
    result = backend.transcribe(
        TranscriptionRequest(
            job_id="job",
            audio_path=tmp_path / "音频 文件.wav",
            raw_output_path=tmp_path / "raw.json",
            start=0,
            end=2,
            language="zh",
            model="model",
            model_path=model,
            part_id="P1",
        )
    )
    assert result.segments[0].text_raw == " 原始  文本 "
    assert result.segments[0].text_clean == "原始 文本"
    assert result.segments[0].confidence is None
    assert " 原始  文本 " in (tmp_path / "raw.json").read_text()


def test_backend_clips_only_intersecting_range_boundary_and_keeps_raw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}")
    (model / "weights.npz").write_bytes(b"weights")
    backend = MLXWhisperBackend(model="model", revision="revision", model_path=model)
    monkeypatch.setattr(mlx_module.importlib.util, "find_spec", lambda name: object())
    fake = type(
        "BoundaryMLXWhisper",
        (),
        {
            "transcribe": staticmethod(
                lambda *args, **kwargs: {
                    "segments": [
                        {"start": 29, "end": 31, "text": "边界内"},
                        {"start": 31, "end": 33, "text": "范围外"},
                    ]
                }
            )
        },
    )
    monkeypatch.setitem(sys.modules, "mlx_whisper", fake)
    raw = tmp_path / "raw.json"
    result = backend.transcribe(
        TranscriptionRequest(
            job_id="job",
            audio_path=tmp_path / "audio.wav",
            raw_output_path=raw,
            start=0,
            end=30,
            language="zh",
            model="model",
            model_path=model,
            part_id="P6",
        )
    )
    assert [(item.start, item.end) for item in result.segments] == [(29, 30)]
    assert result.segments[0].source_ref["range_boundary_clipped"] is True
    assert "范围外" in raw.read_text()


def test_asr_capability_detection_and_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(cwd=tmp_path).asr.model_copy(
        update={"preferred_backend": "auto", "model_path": str(tmp_path / "本地 模型")}
    )
    Path(config.model_path).mkdir()
    monkeypatch.setattr(
        "lectureflow.asr.base.importlib.util.find_spec",
        lambda module: object() if module == "mlx_whisper" else None,
    )
    capabilities = detect_asr_capabilities(config)
    assert capabilities[0].available is True
    assert capabilities[1].available is False
    assert require_asr_backend(config).backend == "mlx_whisper"


def test_asr_capability_reports_missing_model_without_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(cwd=tmp_path).asr.model_copy(
        update={"preferred_backend": "mlx-whisper", "model_path": str(tmp_path / "missing")}
    )
    monkeypatch.setattr("lectureflow.asr.base.importlib.util.find_spec", lambda module: object())
    with pytest.raises(ASRUnavailableError, match="No model download was attempted"):
        require_asr_backend(config)


def test_mlx_backend_description_and_request_model_path_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}")
    (model / "weights.safetensors").write_bytes(b"weights")
    backend = MLXWhisperBackend(model="model", revision="revision", model_path=model)
    monkeypatch.setattr(mlx_module.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(mlx_module, "_package_version", lambda name: "0.4.3")
    assert backend.describe().model_cached is True
    request = TranscriptionRequest(
        job_id="job",
        audio_path=tmp_path / "audio.wav",
        raw_output_path=tmp_path / "raw.json",
        start=0,
        end=2,
        language="zh",
        model="model",
        model_path=tmp_path / "different",
        part_id="P1",
    )
    with pytest.raises(ASRUnavailableError, match="model path differs"):
        backend.transcribe(request)


def test_resolve_mlx_model_downloads_only_audited_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(cwd=tmp_path)
    model = tmp_path / "cached-model"
    model.mkdir()
    calls = 0

    def download(**kwargs: object) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise service.LocalEntryNotFoundError("not cached")
        assert kwargs["token"] is False
        return str(model)

    monkeypatch.setattr(service, "snapshot_download", download)
    resolved, cache_hit = service.resolve_mlx_model(config, offline=False)
    assert resolved == model
    assert cache_hit is False
    changed = config.model_copy(
        update={"asr": config.asr.model_copy(update={"mlx_model": "unaudited/model"})}
    )
    calls = 0
    with pytest.raises(ASRUnavailableError, match="audited pinned"):
        service.resolve_mlx_model(changed, offline=False)


def test_probe_audio_parses_ffprobe_and_rejects_invalid_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service.shutil, "which", lambda command: "/usr/bin/ffprobe")
    monkeypatch.setattr(
        service,
        "run_command",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=json.dumps(
                {
                    "streams": [{"codec_type": "audio", "sample_rate": "16000", "channels": 1}],
                    "format": {"duration": "300.0"},
                }
            )
        ),
    )
    assert service._probe_audio(tmp_path / "audio.wav") == (300.0, 16000, 1)
    monkeypatch.setattr(
        service,
        "run_command",
        lambda *args, **kwargs: SimpleNamespace(stdout="not-json"),
    )
    with pytest.raises(ASRUnavailableError, match="invalid ASR audio metadata"):
        service._probe_audio(tmp_path / "audio.wav")


def test_quality_report_measures_gaps_without_claiming_accuracy() -> None:
    result = ASRTranscriptResult(
        backend="mlx-whisper",
        model="model",
        model_revision="revision",
        language="zh",
        duration_seconds=300,
        segments=[_segment(start=10, end=12)],
        raw_output_path="raw.json",
        elapsed_seconds=30,
        real_time_factor=0.1,
    )
    report = service._quality_report(result)
    assert report["max_silent_or_untranscribed_gap_seconds"] == 288
    assert report["possible_background_music_or_silence_intervals"] == [
        [0.0, 10.0],
        [12.0, 300],
    ]
    assert report["manual_spot_checks"] == []
    assert "not WER" in report["accuracy_note"]


def test_long_range_audio_path_and_technical_cues_are_deterministic(tmp_path: Path) -> None:
    audio, manifest = service._audio_paths(tmp_path, 1500)
    assert audio.name == "asr-input-000-1500.wav"
    assert manifest.name == "asr-input-000-1500.json"
    result = ASRTranscriptResult(
        backend="mlx-whisper",
        model="model",
        model_revision="revision",
        language="zh",
        duration_seconds=1500,
        segments=[
            _segment(start=10, end=12).model_copy(
                update={"text_raw": "这个公式用 KV cache 矩阵求和", "text_clean": "同原文"}
            )
        ],
        raw_output_path="raw.json",
        elapsed_seconds=100,
        real_time_factor=1 / 15,
    )
    technical = service._technical_term_candidates(result)
    assert technical["formula_cue_counts"]["公式"] == 1
    assert technical["formula_cue_counts"]["矩阵"] == 1
    assert technical["candidates"][0]["english_or_abbreviations"] == ["KV", "cache"]
    report = service._quality_report(result, technical)
    assert report["audio_duration_seconds"] == 1500
    assert report["formula_cue_total"] == 3


def test_audio_extraction_preserves_timestamp_gaps() -> None:
    """Cut media can contain packet gaps; ASR WAV must retain the media timeline."""
    source = Path(service.__file__).read_text(encoding="utf-8")
    assert '"aresample=async=1:first_pts=0"' in source
    assert service.AUDIO_STAGE_SCHEMA == "asr-audio-1.2.0"


def test_cached_duration_normalization_preserves_original(tmp_path: Path) -> None:
    destination = tmp_path / "transcript/asr"
    destination.mkdir(parents=True)
    segment = _segment(start=1498, end=1500)
    original = json.dumps(segment.model_dump(mode="json"), ensure_ascii=False) + "\n"
    (destination / "original.jsonl").write_text(original)
    technical = service._technical_term_candidates(
        ASRTranscriptResult(
            backend="mlx-whisper",
            model="model",
            model_revision="revision",
            language="zh",
            duration_seconds=1500.01,
            segments=[segment],
            raw_output_path="raw.json",
            elapsed_seconds=150,
            real_time_factor=0.1,
        )
    )
    (destination / "technical-term-candidates.json").write_text(json.dumps(technical))
    cached = {
        "duration_seconds": 1500.01,
        "model": "model",
        "model_revision": "revision",
        "language": "zh",
        "raw_output_path": "transcript/asr/raw-mlx-whisper.json",
        "elapsed_seconds": 150,
        "real_time_factor": 0.1,
        "artifacts": {},
    }
    digest = service.hash_file(destination / "original.jsonl")
    normalized = service._normalize_cached_duration(tmp_path, cached, 1500)
    assert normalized["duration_seconds"] == 1500
    assert service.hash_file(destination / "original.jsonl") == digest


def test_write_transcript_artifacts_keeps_original_and_exports_all_formats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "transcript/asr"
    destination.mkdir(parents=True)
    (destination / "raw-mlx-whisper.json").write_text("{}")
    result = ASRTranscriptResult(
        backend="mlx-whisper",
        model="model",
        model_revision="revision",
        language="zh",
        duration_seconds=300,
        segments=[_segment()],
        raw_output_path="transcript/asr/raw-mlx-whisper.json",
        elapsed_seconds=1,
        real_time_factor=1 / 300,
    )
    monkeypatch.setattr(service, "_package_version", lambda name: "test-version")
    manifest = service._write_transcript_artifacts(
        tmp_path,
        result,
        input_hash="input",
        audio_manifest={"path": "audio.wav", "sha256": "a" * 64},
        model_path=tmp_path / "model",
        model_cache_hit=True,
    )
    original = destination / "original.jsonl"
    assert json.loads(original.read_text())["text_raw"] == " 原始 文本 "
    assert manifest["immutable_original_sha256"] == service.hash_file(original)
    assert (destination / "transcript.srt").read_text().startswith("1\n")
    assert (destination / "transcript.vtt").read_text().startswith("WEBVTT")


def test_preflight_and_inspect_record_only_local_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "job"
    root.mkdir()
    audio = root / "audio.wav"
    audio.write_bytes(b"audio")
    files = SimpleNamespace(root=root)
    state = SimpleNamespace(
        stages={"transcript_ready": SimpleNamespace(status=SimpleNamespace(value="succeeded"))}
    )
    config = load_config(cwd=tmp_path)
    monkeypatch.setattr(service, "load_job_context", lambda *args, **kwargs: (files, None, state))
    monkeypatch.setattr(
        service,
        "ensure_asr_audio",
        lambda *args, **kwargs: (audio, {"sha256": service.hash_file(audio)}, True),
    )
    model = tmp_path / "model"
    model.mkdir()
    monkeypatch.setattr(service, "resolve_mlx_model", lambda *args, **kwargs: (model, True))

    class Backend:
        def __init__(self, **kwargs: object) -> None:
            pass

        def transcribe(self, request: TranscriptionRequest) -> ASRTranscriptResult:
            request.raw_output_path.parent.mkdir(parents=True, exist_ok=True)
            request.raw_output_path.write_text("{}")
            return ASRTranscriptResult(
                backend="mlx-whisper",
                model=config.asr.mlx_model,
                model_revision=config.asr.mlx_revision,
                language="zh",
                duration_seconds=request.end,
                segments=[_segment(end=2)],
                raw_output_path=str(request.raw_output_path),
                elapsed_seconds=1,
                real_time_factor=1 / request.end,
            )

    monkeypatch.setattr(service, "MLXWhisperBackend", Backend)
    preflight = service.preflight_job("job", config=config, seconds=30)
    assert preflight["status"] == "passed"
    assert preflight["external_model_api_used"] is False

    audio_manifest_path = root / "media/audio/asr-input-000-300.json"
    audio_path = root / "media/audio/asr-input-000-300.wav"
    audio_path.parent.mkdir(parents=True)
    audio_path.write_bytes(b"audio")
    audio_manifest_path.write_text(json.dumps({"sha256": service.hash_file(audio_path)}))
    inspected = service.inspect_asr("job", config=config)
    assert inspected["audio_sha256_matches"] is True
    assert inspected["transcript_ready"] is False


@pytest.mark.parametrize(
    ("raw_segment", "message"),
    [
        ("not-an-object", "is not an object"),
        ({"start": 0, "end": 2}, "Invalid MLX-Whisper segment"),
        ({"start": 2, "end": 1, "text": "反向"}, "segment timing"),
    ],
)
def test_mlx_backend_rejects_malformed_local_segments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_segment: object,
    message: str,
) -> None:
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}")
    (model / "weights.npz").write_bytes(b"weights")
    backend = MLXWhisperBackend(model="model", revision="revision", model_path=model)
    monkeypatch.setattr(mlx_module.importlib.util, "find_spec", lambda name: object())
    fake = type(
        "MalformedMLXWhisper",
        (),
        {"transcribe": staticmethod(lambda *args, **kwargs: {"segments": [raw_segment]})},
    )
    monkeypatch.setitem(sys.modules, "mlx_whisper", fake)
    request = TranscriptionRequest(
        job_id="job",
        audio_path=tmp_path / "audio.wav",
        raw_output_path=tmp_path / "raw.json",
        start=0,
        end=2,
        language="zh",
        model="model",
        model_path=model,
        part_id="P1",
    )
    with pytest.raises(ASRUnavailableError, match=message):
        backend.transcribe(request)


def test_asr_doctor_verifies_metal_and_pinned_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(cwd=tmp_path)
    model = tmp_path / "model"
    model.mkdir()
    monkeypatch.setattr(
        service,
        "_package_version",
        lambda name: {"mlx": "0.32.0", "mlx-whisper": "0.4.3"}[name],
    )
    monkeypatch.setattr(service, "resolve_mlx_model", lambda *args, **kwargs: (model, True))
    mlx_package = ModuleType("mlx")
    mlx_package.__path__ = []  # type: ignore[attr-defined]
    mlx_core = ModuleType("mlx.core")
    mlx_core.metal = SimpleNamespace(is_available=lambda: True)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx", mlx_package)
    monkeypatch.setitem(sys.modules, "mlx.core", mlx_core)
    report = service.asr_doctor(config, offline=True)
    assert report["available"] is True
    assert report["metal_available"] is True
    assert report["external_model_api_used"] is False


def test_asr_json_load_and_package_metadata_errors_are_structured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    malformed = tmp_path / "malformed.json"
    malformed.write_text("not-json")
    with pytest.raises(ASRUnavailableError, match="Cannot read ASR audit file"):
        service._load_json(malformed)
    array = tmp_path / "array.json"
    array.write_text("[]")
    with pytest.raises(ASRUnavailableError, match="Expected JSON object"):
        service._load_json(array)
    monkeypatch.setattr(
        service,
        "version",
        lambda name: (_ for _ in ()).throw(PackageNotFoundError(name)),
    )
    assert service._package_version("missing") is None
