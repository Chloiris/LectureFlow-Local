from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from math import ceil
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from lectureflow.asr.mlx_backend import MLXWhisperBackend
from lectureflow.asr.vibeasr_backend import (
    VIBEASR_LM_BYTES,
    VIBEASR_LM_FILENAME,
    VIBEASR_LM_SHA256,
    VIBEASR_MODEL_BYTES,
    VIBEASR_MODEL_ID,
    VIBEASR_MODEL_REVISION,
    VIBEASR_RUNTIME_REVISION,
    VIBEASR_VAE_BYTES,
    VIBEASR_VAE_FILENAME,
    VIBEASR_VAE_SHA256,
    VibeASRBitNetBackend,
)
from lectureflow.atomic import atomic_write_json, atomic_write_text
from lectureflow.config import AppConfig
from lectureflow.constants import MILESTONES
from lectureflow.errors import ASRUnavailableError, CommandError, StateError
from lectureflow.hashing import hash_file, hash_object
from lectureflow.media.service import resolve_media_path
from lectureflow.pipeline import load_job_context
from lectureflow.process import run_command
from lectureflow.schemas.asr import ASRTranscriptResult, TranscriptionRequest
from lectureflow.schemas.frames import MediaManifest
from lectureflow.schemas.state import StageStatus
from lectureflow.schemas.transcript import TranscriptDocument, TranscriptSegment
from lectureflow.sources.metadata import available_tool_version
from lectureflow.state import (
    JobLock,
    finish_stage_failure,
    finish_stage_success,
    load_state,
    mark_interrupted_running_stages,
    record_transition,
    restore_stage_cache_hit,
    save_state,
    start_stage,
    utc_now,
)
from lectureflow.subtitles.formats import clean_segments, render_srt, render_txt, render_vtt
from lectureflow.subtitles.quality import build_quality_report

MLX_MODEL_BYTES = 1_524_927_044
ASR_STAGE_SCHEMA = "asr-mlx-1.1.0"
VIBEASR_STAGE_SCHEMA = "asr-vibeasr-bitnet-1.0.0"
AUDIO_STAGE_SCHEMA = "asr-audio-1.2.0"
COMMON_ASR_ARTIFACTS = (
    "original.jsonl",
    "cleaned.jsonl",
    "transcript.txt",
    "transcript.srt",
    "transcript.vtt",
    "asr-runtime.json",
    "asr-quality-report.json",
    "technical-term-candidates.json",
)

FORMULA_CUE_TERMS = (
    "公式",
    "等式",
    "损失函数",
    "目标函数",
    "概率",
    "分布",
    "期望",
    "方差",
    "梯度",
    "参数",
    "矩阵",
    "向量",
    "求和",
    "最小化",
    "最大化",
    "定义",
    "定理",
    "推导",
    "如图",
    "看这里",
    "这个式子",
    "上式",
    "下式",
    "左边",
    "右边",
    "分母",
    "分子",
    "等于",
    "定义为",
    "我们可以写成",
)


class LocalEntryNotFoundError(Exception):
    """Dependency-neutral cache-miss signal used by the MLX resolver."""


def snapshot_download(**kwargs: Any) -> str:
    """Load the optional Hugging Face client only when ASR needs model resolution."""
    try:
        from huggingface_hub import snapshot_download as hf_snapshot_download
        from huggingface_hub.errors import LocalEntryNotFoundError as HFLocalEntryNotFoundError
    except ModuleNotFoundError as error:
        raise ASRUnavailableError(
            "MLX ASR dependencies are not installed. Run "
            "`uv sync --dev --extra asr-mlx --locked` only when local ASR is required."
        ) from error
    try:
        return str(hf_snapshot_download(**kwargs))
    except HFLocalEntryNotFoundError as error:
        raise LocalEntryNotFoundError(str(error)) from error


def _package_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


@dataclass(frozen=True, slots=True)
class ASRProcessResult:
    job_id: str
    status: str
    backend: str
    model: str
    audio_cache_hit: bool
    model_cache_hit: bool
    transcript_cache_hit: bool
    segment_count: int
    elapsed_seconds: float
    real_time_factor: float
    transcript_root: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "backend": self.backend,
            "model": self.model,
            "audio_cache_hit": self.audio_cache_hit,
            "model_cache_hit": self.model_cache_hit,
            "transcript_cache_hit": self.transcript_cache_hit,
            "segment_count": self.segment_count,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "real_time_factor": round(self.real_time_factor, 6),
            "transcript_root": self.transcript_root,
        }


@dataclass(frozen=True, slots=True)
class VibeASRRuntime:
    executable: Path
    model_path: Path
    vae_path: Path
    lm_path: Path
    executable_sha256: str
    runtime_revision: str
    vae_sha256: str | None
    lm_sha256: str | None


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ASRUnavailableError(f"Cannot read ASR audit file {path.name}: {error}") from error
    if not isinstance(value, dict):
        raise ASRUnavailableError(f"Expected JSON object in ASR audit file {path.name}.")
    return value


def _jsonl(segments: list[TranscriptSegment]) -> str:
    return "".join(
        json.dumps(
            segment.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        for segment in segments
    )


def _write_immutable_text(path: Path, value: str) -> None:
    encoded = value.encode("utf-8")
    if path.is_file():
        if path.read_bytes() != encoded:
            raise ASRUnavailableError(
                f"Immutable ASR artifact already exists with different content: {path.name}"
            )
        return
    atomic_write_text(path, value)


def resolve_mlx_model(config: AppConfig, *, offline: bool) -> tuple[Path, bool]:
    model = config.asr.mlx_model
    revision = config.asr.mlx_revision
    try:
        cached = Path(
            snapshot_download(
                repo_id=model,
                revision=revision,
                local_files_only=True,
                token=False,
            )
        )
        return cached, True
    except LocalEntryNotFoundError:
        if offline:
            raise ASRUnavailableError(
                f"strict offline model cache miss for {model}@{revision}; no download attempted."
            ) from None
    if model != "mlx-community/whisper-medium-mlx" or revision != (
        "7fc08c4eac4c316526498f147dfdee6f6303f975"
    ):
        raise ASRUnavailableError(
            "Automatic download is allowed only for the audited pinned M3B model. "
            "Verify the replacement repository size before enabling it."
        )
    if MLX_MODEL_BYTES > 2 * 1024**3:
        raise ASRUnavailableError("Pinned model exceeds the 2 GiB approval boundary.")
    path = Path(snapshot_download(repo_id=model, revision=revision, token=False))
    return path, False


def resolve_vibeasr_runtime(config: AppConfig, *, verify_hashes: bool = True) -> VibeASRRuntime:
    if (
        config.asr.vibeasr_model != VIBEASR_MODEL_ID
        or config.asr.vibeasr_revision != VIBEASR_MODEL_REVISION
    ):
        raise ASRUnavailableError(
            "VibeASR BitNet is restricted to the audited official model and pinned revision."
        )
    if config.asr.vibeasr_runtime_revision != VIBEASR_RUNTIME_REVISION:
        raise ASRUnavailableError(
            "VibeASR.cpp runtime is restricted to the audited pinned source revision."
        )
    executable_value = config.asr.vibeasr_executable.strip()
    executable = (
        Path(executable_value).expanduser().resolve()
        if executable_value
        else Path(shutil.which("asr_infer") or "")
    )
    model_value = config.asr.vibeasr_model_path.strip()
    if not str(executable) or not executable.is_file() or not os.access(executable, os.X_OK):
        raise ASRUnavailableError(
            "Official VibeASR.cpp executable is unavailable. Configure "
            "asr.vibeasr_executable; LectureFlow does not clone or build it automatically."
        )
    expected_executable_sha = config.asr.vibeasr_executable_sha256
    if not expected_executable_sha:
        raise ASRUnavailableError(
            "VibeASR executable SHA-256 is not configured. Build the pinned official revision, "
            "run 'shasum -a 256 <asr_infer>', and set asr.vibeasr_executable_sha256."
        )
    executable_sha = hash_file(executable)
    if executable_sha != expected_executable_sha:
        raise ASRUnavailableError("Configured VibeASR executable SHA-256 does not match.")
    if not model_value:
        raise ASRUnavailableError(
            "VibeASR BitNet model directory is not configured. Set asr.vibeasr_model_path "
            "to a local directory containing only the two audited GGUF files. No download "
            "was attempted."
        )
    model_path = Path(model_value).expanduser().resolve()
    vae_path = model_path / VIBEASR_VAE_FILENAME
    lm_path = model_path / VIBEASR_LM_FILENAME
    expected = (
        (vae_path, VIBEASR_VAE_BYTES, VIBEASR_VAE_SHA256),
        (lm_path, VIBEASR_LM_BYTES, VIBEASR_LM_SHA256),
    )
    for path, size, _ in expected:
        if not path.is_file():
            raise ASRUnavailableError(f"Pinned VibeASR model file is missing: {path.name}")
        if path.stat().st_size != size:
            raise ASRUnavailableError(
                f"Pinned VibeASR model size mismatch for {path.name}; expected {size} bytes."
            )
    hashes: list[str | None] = []
    for path, _, digest in expected:
        actual = hash_file(path) if verify_hashes else None
        if actual is not None and actual != digest:
            raise ASRUnavailableError(f"Pinned VibeASR model SHA-256 mismatch: {path.name}")
        hashes.append(actual)
    return VibeASRRuntime(
        executable=executable,
        model_path=model_path,
        vae_path=vae_path,
        lm_path=lm_path,
        executable_sha256=executable_sha,
        runtime_revision=config.asr.vibeasr_runtime_revision,
        vae_sha256=hashes[0],
        lm_sha256=hashes[1],
    )


def asr_doctor(
    config: AppConfig, *, offline: bool = True, backend: str = "mlx-whisper"
) -> dict[str, Any]:
    normalized_backend = backend.replace("_", "-")
    if normalized_backend == "vibeasr-bitnet":
        try:
            runtime = resolve_vibeasr_runtime(config, verify_hashes=True)
        except ASRUnavailableError as error:
            runtime, runtime_error = None, str(error)
        else:
            runtime_error = None
        return {
            "schema_version": "1.0",
            "backend": "vibeasr-bitnet",
            "available": runtime is not None,
            "python": sys.version.split()[0],
            "python_executable": sys.executable,
            "executable": str(runtime.executable) if runtime else None,
            "executable_sha256": runtime.executable_sha256 if runtime else None,
            "runtime_revision": runtime.runtime_revision if runtime else None,
            "model": config.asr.vibeasr_model,
            "revision": config.asr.vibeasr_revision,
            "model_size_bytes": VIBEASR_MODEL_BYTES,
            "model_cached": runtime is not None,
            "model_path": str(runtime.model_path) if runtime else None,
            "threads": config.asr.vibeasr_threads,
            "chunk_seconds": config.asr.vibeasr_chunk_seconds,
            "context_size": config.asr.vibeasr_context_size,
            "max_tokens": config.asr.vibeasr_max_tokens,
            "hotword_count": len(config.asr.vibeasr_hotwords),
            "offline": offline,
            "error": runtime_error,
            "automatic_download_enabled": False,
            "external_model_api_used": False,
            "cloud_asr_used": False,
        }
    if normalized_backend != "mlx-whisper":
        raise ASRUnavailableError(
            "ASR doctor supports mlx-whisper or vibeasr-bitnet in this release."
        )
    package = _package_version("mlx-whisper")
    mlx = _package_version("mlx")
    try:
        model_path, cached = resolve_mlx_model(config, offline=True)
    except ASRUnavailableError as error:
        model_path, cached, model_error = None, False, str(error)
    else:
        model_error = None
    metal_available = False
    if mlx is not None:
        try:
            import mlx.core as mx

            metal_available = bool(mx.metal.is_available())
        except Exception:
            metal_available = False
    available = package is not None and mlx is not None and metal_available and cached
    return {
        "schema_version": "1.0",
        "backend": "mlx-whisper",
        "available": available,
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "mlx_version": mlx,
        "mlx_whisper_version": package,
        "metal_available": metal_available,
        "model": config.asr.mlx_model,
        "revision": config.asr.mlx_revision,
        "model_size_bytes": MLX_MODEL_BYTES,
        "model_cached": cached,
        "model_path": str(model_path) if model_path else None,
        "offline": offline,
        "error": model_error,
        "external_model_api_used": False,
        "cloud_asr_used": False,
    }


def _audio_paths(
    root: Path, end: float = 300.0, *, backend: str = "mlx-whisper"
) -> tuple[Path, Path]:
    suffix = f"000-{round(end)}"
    if backend == "vibeasr-bitnet":
        suffix = f"vibeasr-bitnet-{suffix}"
    return root / f"media/audio/asr-input-{suffix}.wav", root / (
        f"media/audio/asr-input-{suffix}.json"
    )


def _audio_cache_valid(
    audio: Path, manifest_path: Path, input_hash: str, *, sample_rate: int = 16000
) -> bool:
    if not audio.is_file() or not manifest_path.is_file():
        return False
    try:
        manifest = _load_json(manifest_path)
        return (
            manifest["input_hash"] == input_hash
            and manifest["sha256"] == hash_file(audio)
            and manifest["sample_rate"] == sample_rate
            and manifest["channels"] == 1
        )
    except (ASRUnavailableError, KeyError):
        return False


def _probe_audio(path: Path) -> tuple[float, int, int]:
    executable = shutil.which("ffprobe")
    if executable is None:
        raise ASRUnavailableError("FFprobe is required for local ASR audio verification.")
    result = run_command(
        (
            executable,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            path,
        ),
        timeout=120,
    )
    try:
        payload = json.loads(result.stdout)
        stream = next(item for item in payload["streams"] if item["codec_type"] == "audio")
        duration = float(payload["format"]["duration"])
        sample_rate = int(stream["sample_rate"])
        channels = int(stream["channels"])
    except (KeyError, TypeError, ValueError, StopIteration, json.JSONDecodeError) as error:
        raise ASRUnavailableError(
            f"FFprobe returned invalid ASR audio metadata: {error}"
        ) from error
    return duration, sample_rate, channels


def ensure_asr_audio(
    job_id: str, *, config: AppConfig, backend: str = "mlx-whisper"
) -> tuple[Path, dict[str, Any], bool]:
    files, _, state = load_job_context(job_id, config=config)
    media = MediaManifest.model_validate(_load_json(files.media_manifest))
    media_path = resolve_media_path(files, media)
    target_duration = media.duration
    normalized_backend = backend.replace("_", "-")
    if normalized_backend not in {"mlx-whisper", "vibeasr-bitnet"}:
        raise ASRUnavailableError(f"Unsupported local ASR audio backend: {backend}")
    sample_rate_target = 24000 if normalized_backend == "vibeasr-bitnet" else 16000
    audio, audio_manifest = _audio_paths(files.root, target_duration, backend=normalized_backend)
    ffmpeg_version = available_tool_version("ffmpeg")
    audio_input = {
        "schema": AUDIO_STAGE_SCHEMA,
        "source_media_sha256": media.sha256,
        "range": [0.0, target_duration],
        "sample_rate": sample_rate_target,
        "channels": 1,
        "codec": "pcm_s16le",
        "ffmpeg": ffmpeg_version,
    }
    if normalized_backend == "vibeasr-bitnet":
        audio_input["backend"] = normalized_backend
    input_hash = hash_object(audio_input)
    with JobLock(files.lock):
        if _audio_cache_valid(audio, audio_manifest, input_hash, sample_rate=sample_rate_target):
            current = state.current_milestone
            successful = state.last_successful_milestone
            overall = state.overall_status
            restored = restore_stage_cache_hit(
                state,
                "audio_ready",
                input_hash=input_hash,
                output_hash=hash_file(audio_manifest),
            )
            if (
                restored
                and current in MILESTONES
                and MILESTONES.index(current) > MILESTONES.index("audio_ready")
            ):
                state.current_milestone = current
                state.last_successful_milestone = successful
                state.overall_status = overall
            if not restored and state.stages["audio_ready"].status != StageStatus.SUCCEEDED:
                current, successful = state.current_milestone, state.last_successful_milestone
                attempt = start_stage(
                    state,
                    "audio_ready",
                    input_hash=input_hash,
                    config_hash=hash_object({"sample_rate": sample_rate_target, "channels": 1}),
                    tool="ffmpeg-cache-adoption",
                    tool_version=ffmpeg_version,
                )
                finish_stage_success(
                    state, "audio_ready", attempt, output_hash=hash_file(audio_manifest)
                )
                state.current_milestone = current
                state.last_successful_milestone = successful
                restored = True
            if restored:
                save_state(files.state, state)
            record_transition(files.events, state, "stage_cache_hit", stage="audio_ready")
            return audio, _load_json(audio_manifest), True
        executable = shutil.which("ffmpeg")
        if executable is None:
            raise ASRUnavailableError("FFmpeg is required for local ASR audio extraction.")
        prior_current = state.current_milestone
        prior_successful = state.last_successful_milestone
        prior_overall = state.overall_status
        attempt = start_stage(
            state,
            "audio_ready",
            input_hash=input_hash,
            config_hash=hash_object({"sample_rate": sample_rate_target, "channels": 1}),
            tool="ffmpeg",
            tool_version=ffmpeg_version,
        )
        save_state(files.state, state)
        audio.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(suffix=".wav", dir=audio.parent)
        os.close(descriptor)
        temporary = Path(temp_name)
        temporary.unlink()
        try:
            run_command(
                (
                    executable,
                    "-nostdin",
                    "-v",
                    "error",
                    "-i",
                    media_path,
                    "-map",
                    "0:a:0",
                    "-af",
                    "aresample=async=1:first_pts=0",
                    "-ac",
                    "1",
                    "-ar",
                    str(sample_rate_target),
                    "-c:a",
                    "pcm_s16le",
                    temporary,
                ),
                timeout=max(300, int(target_duration * 2)),
            )
            duration, sample_rate, channels = _probe_audio(temporary)
            if (
                abs(duration - target_duration) > 0.25
                or sample_rate != sample_rate_target
                or channels != 1
            ):
                raise ASRUnavailableError(
                    f"Unexpected ASR audio facts: {duration:.3f}s {sample_rate}Hz {channels}ch."
                )
            os.replace(temporary, audio)
            payload = {
                "schema_version": "1.0",
                "input_hash": input_hash,
                "path": str(audio.relative_to(files.root)),
                "source_media_path": media.media_path,
                "source_media_storage": media.storage,
                "source_media_sha256": media.sha256,
                "ffmpeg_version": ffmpeg_version,
                "duration_seconds": duration,
                "sample_rate": sample_rate,
                "channels": channels,
                "codec": "pcm_s16le",
                "asr_backend": normalized_backend,
                "sha256": hash_file(audio),
                "generated_at": utc_now(),
            }
            atomic_write_json(audio_manifest, payload)
            finish_stage_success(
                state, "audio_ready", attempt, output_hash=hash_file(audio_manifest)
            )
            if prior_current in MILESTONES and MILESTONES.index(prior_current) > MILESTONES.index(
                "audio_ready"
            ):
                state.current_milestone = prior_current
                state.last_successful_milestone = prior_successful
                state.overall_status = prior_overall
            save_state(files.state, state)
            record_transition(files.events, state, "stage_succeeded", stage="audio_ready")
            return audio, payload, False
        except Exception as error:
            temporary.unlink(missing_ok=True)
            finish_stage_failure(state, "audio_ready", attempt, error=str(error), recoverable=True)
            save_state(files.state, state)
            record_transition(files.events, state, "stage_failed", stage="audio_ready")
            if isinstance(error, (ASRUnavailableError, CommandError)):
                raise
            raise ASRUnavailableError(f"ASR audio extraction failed: {error}") from error


def _maximum_gap(segments: list[TranscriptSegment], duration: float) -> float:
    cursor = 0.0
    maximum = 0.0
    for segment in sorted(segments, key=lambda item: item.start):
        maximum = max(maximum, segment.start - cursor)
        cursor = max(cursor, segment.end)
    return max(maximum, duration - cursor)


def _gap_intervals(segments: list[TranscriptSegment], duration: float) -> list[list[float]]:
    gaps: list[list[float]] = []
    cursor = 0.0
    for segment in sorted(segments, key=lambda item: item.start):
        if segment.start - cursor >= 2.0:
            gaps.append([round(cursor, 3), round(segment.start, 3)])
        cursor = max(cursor, segment.end)
    if duration - cursor >= 2.0:
        gaps.append([round(cursor, 3), round(duration, 3)])
    return gaps


def _technical_term_candidates(result: ASRTranscriptResult) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    formula_counts = {term: 0 for term in FORMULA_CUE_TERMS}
    mixed_pattern = re.compile(r"[A-Za-z][A-Za-z0-9+_.-]{1,}")
    symbol_pattern = re.compile(r"(?:\d+(?:\.\d+)?|[A-Za-z]+[_^][A-Za-z0-9]+)")
    for segment in result.segments:
        mixed = sorted(set(mixed_pattern.findall(segment.text_raw)))
        symbols = sorted(set(symbol_pattern.findall(segment.text_raw)))
        cues = [term for term in FORMULA_CUE_TERMS if term in segment.text_raw]
        for term in cues:
            formula_counts[term] += segment.text_raw.count(term)
        if mixed or symbols or cues:
            candidates.append(
                {
                    "segment_id": segment.segment_id,
                    "start": segment.start,
                    "end": segment.end,
                    "text_raw": segment.text_raw,
                    "english_or_abbreviations": mixed,
                    "numbers_or_symbols": symbols,
                    "formula_cues": cues,
                    "decision": "pending",
                }
            )
    return {
        "schema_version": "1.0",
        "candidate_count": len(candidates),
        "formula_cue_counts": formula_counts,
        "formula_cue_total": sum(formula_counts.values()),
        "candidates": candidates,
        "semantic_correction_applied": False,
    }


def _quality_report(
    result: ASRTranscriptResult, technical: dict[str, Any] | None = None
) -> dict[str, Any]:
    technical = technical or _technical_term_candidates(result)
    document = TranscriptDocument(
        timed=True,
        source=result.backend.replace("-", "_"),
        language=result.language,
        part_id=result.segments[0].part_id,
        segments=result.segments,
    )
    base = build_quality_report(document, media_duration=result.duration_seconds)
    low_density: list[dict[str, Any]] = []
    for minute in range(ceil(result.duration_seconds / 60)):
        start, end = float(minute * 60), float((minute + 1) * 60)
        chars = sum(
            len(segment.text_raw.replace(" ", ""))
            for segment in result.segments
            if segment.end > start and segment.start < end
        )
        if chars < 20:
            low_density.append({"start": start, "end": end, "character_count": chars})
    report = {
        **base,
        "audio_duration_seconds": result.duration_seconds,
        "total_character_count": base["character_count"],
        "invalid_time_count": len(base["invalid_time_segments"]),
        "overlap_segment_count": len(base["overlaps"]),
        "duplicate_segment_count": len(base["duplicates"]),
        "empty_segment_count": len(base["empty_segments"]),
        "long_segment_count": len(base["very_long_segments"]),
        "suspicious_garbled_count": len(base["suspicious_garbled_segments"]),
        "backend": result.backend,
        "model": result.model,
        "transcription_elapsed_seconds": round(result.elapsed_seconds, 3),
        "real_time_factor": round(result.real_time_factor, 6),
        "segment_count": len(result.segments),
        "max_silent_or_untranscribed_gap_seconds": round(
            _maximum_gap(result.segments, result.duration_seconds), 3
        ),
        "low_text_density_intervals": low_density,
        "possible_background_music_or_silence_intervals": _gap_intervals(
            result.segments, result.duration_seconds
        ),
        "mixed_language_segment_count": sum(
            bool(item["english_or_abbreviations"]) for item in technical["candidates"]
        ),
        "numeric_or_symbol_dense_segment_count": sum(
            len(item["numbers_or_symbols"]) >= 2 for item in technical["candidates"]
        ),
        "technical_term_candidate_count": technical["candidate_count"],
        "formula_cue_counts": technical["formula_cue_counts"],
        "formula_cue_total": technical["formula_cue_total"],
        "accuracy_note": "No reference transcript is available; these metrics are not WER.",
        "manual_spot_checks": [],
    }
    if result.backend == "vibeasr-bitnet":
        report.update(
            {
                "timestamp_basis": "deterministic_audio_chunk_bounds",
                "timestamp_granularity_seconds": max(
                    segment.end - segment.start for segment in result.segments
                ),
                "speaker_diarization_available": False,
                "coverage_note": (
                    "VibeASR BitNet coverage uses coarse chunk bounds and must not be interpreted "
                    "as model-aligned speech coverage."
                ),
            }
        )
    return report


def _asr_input_hash(
    audio_manifest: dict[str, Any],
    config: AppConfig,
    *,
    backend: str = "mlx-whisper",
    runtime: VibeASRRuntime | None = None,
) -> str:
    normalized_backend = backend.replace("_", "-")
    if normalized_backend == "vibeasr-bitnet":
        if runtime is None or runtime.vae_sha256 is None or runtime.lm_sha256 is None:
            raise ASRUnavailableError("Verified VibeASR runtime hashes are required for caching.")
        return hash_object(
            {
                "schema": VIBEASR_STAGE_SCHEMA,
                "audio_sha256": audio_manifest["sha256"],
                "backend": normalized_backend,
                "model": config.asr.vibeasr_model,
                "revision": config.asr.vibeasr_revision,
                "vae_sha256": runtime.vae_sha256,
                "lm_sha256": runtime.lm_sha256,
                "executable_sha256": runtime.executable_sha256,
                "runtime_revision": runtime.runtime_revision,
                "threads": config.asr.vibeasr_threads,
                "chunk_seconds": config.asr.vibeasr_chunk_seconds,
                "context_size": config.asr.vibeasr_context_size,
                "max_tokens": config.asr.vibeasr_max_tokens,
                "context_sha256": hash_object(config.asr.vibeasr_hotwords),
                "language": "zh",
                "task": "transcribe",
                "word_timestamps": False,
            }
        )
    return hash_object(
        {
            "schema": ASR_STAGE_SCHEMA,
            "audio_sha256": audio_manifest["sha256"],
            "backend": "mlx-whisper",
            "model": config.asr.mlx_model,
            "revision": config.asr.mlx_revision,
            "language": "zh",
            "task": "transcribe",
            "word_timestamps": False,
        }
    )


def _cached_transcript(root: Path, input_hash: str) -> dict[str, Any] | None:
    manifest_path = root / "transcript/asr/asr-manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = _load_json(manifest_path)
        if manifest["input_hash"] != input_hash:
            return None
        for relative, digest in manifest["artifacts"].items():
            path = root / relative
            if not path.is_file() or hash_file(path) != digest:
                return None
        original = root / "transcript/asr/original.jsonl"
        if hash_file(original) != manifest["immutable_original_sha256"]:
            return None
        return manifest
    except (ASRUnavailableError, KeyError, TypeError):
        return None


def _write_transcript_artifacts(
    root: Path,
    result: ASRTranscriptResult,
    *,
    input_hash: str,
    audio_manifest: dict[str, Any],
    model_path: Path,
    model_cache_hit: bool,
    raw_output_filename: str = "raw-mlx-whisper.json",
    runtime_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    destination = root / "transcript/asr"
    destination.mkdir(parents=True, exist_ok=True)
    original = result.segments
    cleaned = clean_segments(original)
    document = TranscriptDocument(
        timed=True,
        source=result.backend.replace("-", "_"),
        language=result.language,
        part_id=result.segments[0].part_id,
        segments=cleaned,
        source_ref={"asr_manifest": "transcript/asr/asr-manifest.json"},
    )
    _write_immutable_text(destination / "original.jsonl", _jsonl(original))
    atomic_write_text(destination / "cleaned.jsonl", _jsonl(cleaned))
    atomic_write_text(destination / "transcript.txt", render_txt(document))
    atomic_write_text(destination / "transcript.srt", render_srt(document))
    atomic_write_text(destination / "transcript.vtt", render_vtt(document))
    runtime = {
        "schema_version": "1.0",
        "python_executable": sys.executable,
        "python_version": sys.version.split()[0],
        "mlx_version": _package_version("mlx"),
        "mlx_whisper_version": _package_version("mlx-whisper"),
        "backend": result.backend,
        "model": result.model,
        "revision": result.model_revision,
        "model_path": str(model_path),
        "model_size_bytes": MLX_MODEL_BYTES,
        "model_cache_hit": model_cache_hit,
        "external_model_api_used": False,
        "cloud_asr_used": False,
        **(runtime_details or {}),
    }
    atomic_write_json(destination / "asr-runtime.json", runtime)
    technical = _technical_term_candidates(result)
    atomic_write_json(destination / "technical-term-candidates.json", technical)
    atomic_write_json(destination / "asr-quality-report.json", _quality_report(result, technical))
    artifacts = {
        f"transcript/asr/{name}": hash_file(destination / name)
        for name in (raw_output_filename, *COMMON_ASR_ARTIFACTS)
    }
    model_size = VIBEASR_MODEL_BYTES if result.backend == "vibeasr-bitnet" else MLX_MODEL_BYTES
    manifest = {
        "schema_version": "1.0",
        "input_hash": input_hash,
        "backend": result.backend,
        "model": result.model,
        "model_revision": result.model_revision,
        "model_size_bytes": model_size,
        "model_path": str(model_path),
        "language": result.language,
        "duration_seconds": result.duration_seconds,
        "requested_range": [0.0, result.duration_seconds],
        "segment_count": len(original),
        "elapsed_seconds": result.elapsed_seconds,
        "real_time_factor": result.real_time_factor,
        "audio_path": audio_manifest["path"],
        "audio_sha256": audio_manifest["sha256"],
        "raw_output_path": f"transcript/asr/{raw_output_filename}",
        "immutable_original_sha256": hash_file(destination / "original.jsonl"),
        "artifacts": artifacts,
        "generated_at": utc_now(),
        "external_model_api_used": False,
        "cloud_asr_used": False,
    }
    atomic_write_json(destination / "asr-manifest.json", manifest)
    return manifest


def _normalize_cached_duration(
    root: Path, cached: dict[str, Any], requested_duration: float
) -> dict[str, Any]:
    stored_duration = float(cached["duration_seconds"])
    if abs(stored_duration - requested_duration) <= 0.001:
        return cached
    if abs(stored_duration - requested_duration) > 0.25:
        raise ASRUnavailableError(
            "Cached ASR duration differs from the registered range by more than 0.25 seconds."
        )
    segments = [
        TranscriptSegment.model_validate(json.loads(line))
        for line in (root / "transcript/asr/original.jsonl").read_text().splitlines()
        if line.strip()
    ]
    if any(segment.end > requested_duration + 0.001 for segment in segments):
        raise ASRUnavailableError("Cached ASR contains segments outside the registered range.")
    result = ASRTranscriptResult(
        backend=str(cached.get("backend", "mlx-whisper")),
        model=str(cached["model"]),
        model_revision=str(cached["model_revision"]),
        language=str(cached["language"]),
        duration_seconds=requested_duration,
        segments=segments,
        raw_output_path=str(cached["raw_output_path"]),
        elapsed_seconds=float(cached["elapsed_seconds"]),
        real_time_factor=float(cached["elapsed_seconds"]) / requested_duration,
    )
    technical_path = root / "transcript/asr/technical-term-candidates.json"
    technical = _load_json(technical_path)
    quality_path = root / "transcript/asr/asr-quality-report.json"
    atomic_write_json(quality_path, _quality_report(result, technical))
    cached["duration_seconds"] = requested_duration
    cached["requested_range"] = [0.0, requested_duration]
    cached["real_time_factor"] = result.real_time_factor
    cached["artifacts"]["transcript/asr/asr-quality-report.json"] = hash_file(quality_path)
    atomic_write_json(root / "transcript/asr/asr-manifest.json", cached)
    return cached


def _select_backend(config: AppConfig, requested: str | None, root: Path | None = None) -> str:
    value = requested
    if value is None and root is not None:
        manifest_path = root / "transcript/asr/asr-manifest.json"
        if manifest_path.is_file():
            value = str(_load_json(manifest_path).get("backend") or "")
    if not value:
        value = config.asr.preferred_backend
    normalized = value.replace("_", "-")
    if normalized == "auto":
        normalized = "mlx-whisper"
    if normalized not in {"mlx-whisper", "vibeasr-bitnet"}:
        raise ASRUnavailableError("Runnable local ASR backends are mlx-whisper and vibeasr-bitnet.")
    return normalized


def _locked_transcript_backend(state: Any) -> str | None:
    record = state.stages.get("transcript_ready")
    if record is None:
        return None
    tools = {
        str(attempt.tool).replace("_", "-")
        for attempt in getattr(record, "attempts", [])
        if str(attempt.tool).replace("_", "-") in {"mlx-whisper", "vibeasr-bitnet"}
    }
    if len(tools) > 1:
        raise StateError("Transcript attempt history contains conflicting ASR backends.")
    return next(iter(tools), None)


def _build_backend(
    config: AppConfig, backend_name: str, *, offline: bool
) -> tuple[Any, Path, bool, VibeASRRuntime | None, str, dict[str, Any], str | None]:
    if backend_name == "mlx-whisper":
        model_path, cache_hit = resolve_mlx_model(config, offline=offline)
        backend = MLXWhisperBackend(
            model=config.asr.mlx_model,
            revision=config.asr.mlx_revision,
            model_path=model_path,
        )
        return (
            backend,
            model_path,
            cache_hit,
            None,
            "raw-mlx-whisper.json",
            {},
            _package_version("mlx-whisper"),
        )
    runtime = resolve_vibeasr_runtime(config, verify_hashes=True)
    backend = VibeASRBitNetBackend(
        executable=runtime.executable,
        model=config.asr.vibeasr_model,
        revision=config.asr.vibeasr_revision,
        model_path=runtime.model_path,
        threads=config.asr.vibeasr_threads,
        chunk_seconds=config.asr.vibeasr_chunk_seconds,
        context_size=config.asr.vibeasr_context_size,
        max_tokens=config.asr.vibeasr_max_tokens,
    )
    details = {
        "mlx_version": None,
        "mlx_whisper_version": None,
        "executable": str(runtime.executable),
        "executable_sha256": runtime.executable_sha256,
        "runtime_revision": runtime.runtime_revision,
        "vae_model_path": str(runtime.vae_path),
        "vae_sha256": runtime.vae_sha256,
        "lm_model_path": str(runtime.lm_path),
        "lm_sha256": runtime.lm_sha256,
        "model_size_bytes": VIBEASR_MODEL_BYTES,
        "threads": config.asr.vibeasr_threads,
        "chunk_seconds": config.asr.vibeasr_chunk_seconds,
        "context_size": config.asr.vibeasr_context_size,
        "max_tokens": config.asr.vibeasr_max_tokens,
        "hotwords": config.asr.vibeasr_hotwords,
        "automatic_download_enabled": False,
    }
    return (
        backend,
        runtime.model_path,
        True,
        runtime,
        "raw-vibeasr-bitnet.json",
        details,
        f"sha256:{runtime.executable_sha256[:12]}",
    )


def transcribe_job(
    job_id: str,
    *,
    config: AppConfig,
    backend: str | None = None,
    offline: bool = False,
    force: bool = False,
    start: float | None = None,
    end: float | None = None,
) -> ASRProcessResult:
    files, manifest, state = load_job_context(job_id, config=config)
    locked_backend = _locked_transcript_backend(state)
    backend_name = (
        locked_backend
        if backend is None and locked_backend is not None
        else _select_backend(config, backend, files.root)
    )
    if locked_backend is not None and locked_backend != backend_name:
        raise ASRUnavailableError(
            f"This job's first transcript attempt locked backend={locked_backend}; "
            f"create a separate job to use {backend_name}."
        )
    selected_model = (
        config.asr.vibeasr_model if backend_name == "vibeasr-bitnet" else config.asr.mlx_model
    )
    selected_revision = (
        config.asr.vibeasr_revision if backend_name == "vibeasr-bitnet" else config.asr.mlx_revision
    )
    registered_start = manifest.requested_range.start
    registered_end = manifest.requested_range.end
    requested_start = registered_start if start is None else start
    requested_end = registered_end if end is None else end
    if registered_end is None:
        raise StateError("ASR requires a finite registered task end time.")
    if requested_start != registered_start or requested_end != registered_end:
        raise StateError(
            "ASR range must exactly match the registered task range "
            f"[{registered_start:g}, {registered_end:g}]."
        )
    requested_duration = registered_end - registered_start
    existing_manifest_path = files.root / "transcript/asr/asr-manifest.json"
    if existing_manifest_path.is_file():
        existing_backend = str(_load_json(existing_manifest_path).get("backend", "mlx-whisper"))
        if existing_backend != backend_name:
            raise ASRUnavailableError(
                "This job already contains an immutable transcript from "
                f"{existing_backend}; create a separate job to compare {backend_name}."
            )
        if force:
            raise ASRUnavailableError(
                "Immutable ASR artifacts already exist; --force-stage cannot overwrite raw or "
                "original transcript data. Use a separate workspace for a new ASR run."
            )
    audio, audio_manifest, audio_cache_hit = ensure_asr_audio(
        job_id, config=config, backend=backend_name
    )
    (
        backend_impl,
        model_path,
        model_cache_hit,
        vibe_runtime,
        raw_filename,
        runtime_details,
        tool_version,
    ) = _build_backend(config, backend_name, offline=offline)
    if not backend_impl.is_available():
        raise ASRUnavailableError(f"Pinned {backend_name} backend/model is not runnable.")
    input_hash = _asr_input_hash(audio_manifest, config, backend=backend_name, runtime=vibe_runtime)
    cached = None if force else _cached_transcript(files.root, input_hash)
    if cached is not None:
        with JobLock(files.lock):
            state = load_state(files.state)
            restored = restore_stage_cache_hit(
                state,
                "transcript_ready",
                input_hash=input_hash,
                output_hash=hash_file(files.root / "transcript/asr/asr-manifest.json"),
            )
            if restored:
                save_state(files.state, state)
        previous_duration = float(cached["duration_seconds"])
        cached = _normalize_cached_duration(files.root, cached, requested_duration)
        if previous_duration != float(cached["duration_seconds"]):
            attempts = state.stages["transcript_ready"].attempts
            latest = attempts[-1] if attempts else None
            if latest is not None:
                latest.output_hash = hash_file(files.root / "transcript/asr/asr-manifest.json")
                save_state(files.state, state)
            record_transition(
                files.events,
                state,
                "stage_cache_repaired",
                stage="transcript_ready",
                repair="registered_range_duration_normalized",
            )
        record_transition(files.events, state, "stage_cache_hit", stage="transcript_ready")
        return ASRProcessResult(
            job_id=job_id,
            status="transcript_ready",
            backend=backend_name,
            model=str(cached["model"]),
            audio_cache_hit=audio_cache_hit,
            model_cache_hit=model_cache_hit,
            transcript_cache_hit=True,
            segment_count=int(cached["segment_count"]),
            elapsed_seconds=float(cached["elapsed_seconds"]),
            real_time_factor=float(cached["real_time_factor"]),
            transcript_root="transcript/asr",
        )
    if existing_manifest_path.is_file():
        raise ASRUnavailableError(
            "Existing immutable ASR artifacts do not match the selected runtime/configuration or "
            "failed integrity checks; refusing in-place replacement. Use a separate workspace."
        )
    with JobLock(files.lock):
        state = load_state(files.state)
        if state.stages["transcript_ready"].status == StageStatus.RUNNING:
            interrupted = mark_interrupted_running_stages(state)
            save_state(files.state, state)
            for stage_name in interrupted:
                record_transition(files.events, state, "stage_interrupted", stage=stage_name)
        inner_locked = _locked_transcript_backend(state)
        if inner_locked is not None and inner_locked != backend_name:
            raise ASRUnavailableError(
                f"Concurrent transcript attempt locked backend={inner_locked}; create a separate "
                f"job to use {backend_name}."
            )
        if existing_manifest_path.is_file():
            concurrent_manifest = _load_json(existing_manifest_path)
            if str(concurrent_manifest.get("backend", "mlx-whisper")) != backend_name:
                raise ASRUnavailableError(
                    "Concurrent ASR completion used a different backend; refusing overwrite."
                )
            if str(concurrent_manifest.get("input_hash")) != input_hash:
                raise ASRUnavailableError(
                    "Concurrent ASR completion used different inputs; refusing overwrite."
                )
            late_cached = _cached_transcript(files.root, input_hash)
            if late_cached is None:
                raise ASRUnavailableError(
                    "Concurrent immutable ASR output failed integrity checks; refusing overwrite."
                )
            record_transition(files.events, state, "stage_cache_hit", stage="transcript_ready")
            return ASRProcessResult(
                job_id=job_id,
                status="transcript_ready",
                backend=backend_name,
                model=str(late_cached["model"]),
                audio_cache_hit=audio_cache_hit,
                model_cache_hit=model_cache_hit,
                transcript_cache_hit=True,
                segment_count=int(late_cached["segment_count"]),
                elapsed_seconds=float(late_cached["elapsed_seconds"]),
                real_time_factor=float(late_cached["real_time_factor"]),
                transcript_root="transcript/asr",
            )
        attempt = start_stage(
            state,
            "transcript_ready",
            input_hash=input_hash,
            config_hash=hash_object(
                {
                    "backend": backend_name,
                    "model": selected_model,
                    "revision": selected_revision,
                }
            ),
            tool=backend_name,
            tool_version=tool_version,
        )
        save_state(files.state, state)
        staging = files.root / f"transcript/asr/.raw-{attempt}.json"
        try:
            request = TranscriptionRequest(
                job_id=job_id,
                audio_path=audio,
                raw_output_path=staging,
                start=0.0,
                end=requested_duration,
                language="zh",
                model=selected_model,
                model_path=model_path,
                part_id=f"P{manifest.source.page or 1}",
                context=(
                    ", ".join(config.asr.vibeasr_hotwords)
                    if backend_name == "vibeasr-bitnet" and config.asr.vibeasr_hotwords
                    else None
                ),
            )
            result = backend_impl.transcribe(request)
            effective_raw_filename = raw_filename
            if backend_name == "vibeasr-bitnet":
                effective_raw_filename = f"raw-vibeasr-bitnet-{hash_file(staging)[:16]}.json"
            raw_target = files.root / "transcript/asr" / effective_raw_filename
            raw_target.parent.mkdir(parents=True, exist_ok=True)
            if raw_target.is_file():
                if hash_file(raw_target) != hash_file(staging):
                    raise ASRUnavailableError(
                        "Immutable raw ASR output already exists with different content."
                    )
                staging.unlink()
            else:
                os.replace(staging, raw_target)
            result = result.model_copy(
                update={"raw_output_path": f"transcript/asr/{effective_raw_filename}"}
            )
            stored = _write_transcript_artifacts(
                files.root,
                result,
                input_hash=input_hash,
                audio_manifest=audio_manifest,
                model_path=model_path,
                model_cache_hit=model_cache_hit,
                raw_output_filename=effective_raw_filename,
                runtime_details=runtime_details,
            )
            finish_stage_success(
                state,
                "transcript_ready",
                attempt,
                output_hash=hash_file(files.root / "transcript/asr/asr-manifest.json"),
            )
            state.current_milestone = "frames_ready"
            state.last_successful_milestone = "frames_ready"
            save_state(files.state, state)
            record_transition(files.events, state, "stage_succeeded", stage="transcript_ready")
            return ASRProcessResult(
                job_id=job_id,
                status="transcript_ready",
                backend=backend_name,
                model=selected_model,
                audio_cache_hit=audio_cache_hit,
                model_cache_hit=model_cache_hit,
                transcript_cache_hit=False,
                segment_count=int(stored["segment_count"]),
                elapsed_seconds=float(stored["elapsed_seconds"]),
                real_time_factor=float(stored["real_time_factor"]),
                transcript_root="transcript/asr",
            )
        except Exception as error:
            staging.unlink(missing_ok=True)
            finish_stage_failure(
                state, "transcript_ready", attempt, error=str(error), recoverable=True
            )
            save_state(files.state, state)
            record_transition(files.events, state, "stage_failed", stage="transcript_ready")
            if isinstance(error, (ASRUnavailableError, ValidationError)):
                raise ASRUnavailableError(str(error)) from error
            raise ASRUnavailableError(f"Local transcription failed: {error}") from error


def preflight_job(
    job_id: str,
    *,
    config: AppConfig,
    seconds: float = 30.0,
    backend: str | None = None,
) -> dict[str, Any]:
    files, _, state = load_job_context(job_id, config=config)
    backend_name = _select_backend(config, backend)
    locked_backend = _locked_transcript_backend(state)
    if locked_backend is not None and locked_backend != backend_name:
        raise ASRUnavailableError(
            f"This job is locked to backend={locked_backend}; use a separate workspace to "
            f"preflight {backend_name}."
        )
    manifest_path = files.root / "transcript/asr/asr-manifest.json"
    if (
        manifest_path.is_file()
        and str(_load_json(manifest_path).get("backend", "mlx-whisper")) != backend_name
    ):
        raise ASRUnavailableError(
            "Preflight cannot add a different ASR backend to a job with immutable transcript."
        )
    selected_model = (
        config.asr.vibeasr_model if backend_name == "vibeasr-bitnet" else config.asr.mlx_model
    )
    audio, audio_manifest, audio_cache_hit = ensure_asr_audio(
        job_id, config=config, backend=backend_name
    )
    (
        backend_impl,
        model_path,
        model_cache_hit,
        _,
        raw_filename,
        _,
        _,
    ) = _build_backend(config, backend_name, offline=True)
    raw_path = files.root / f"reports/asr-preflight-{backend_name}-000-030.raw.json"
    result = backend_impl.transcribe(
        TranscriptionRequest(
            job_id=job_id,
            audio_path=audio,
            raw_output_path=raw_path,
            start=0.0,
            end=seconds,
            language="zh",
            model=selected_model,
            model_path=model_path,
            part_id="P1",
            context=(
                ", ".join(config.asr.vibeasr_hotwords)
                if backend_name == "vibeasr-bitnet" and config.asr.vibeasr_hotwords
                else None
            ),
        )
    )
    payload = {
        "schema_version": "1.0",
        "job_id": job_id,
        "range": [0.0, seconds],
        "status": "passed",
        "segment_count": len(result.segments),
        "sample_text": "".join(segment.text_clean for segment in result.segments),
        "elapsed_seconds": result.elapsed_seconds,
        "real_time_factor": result.real_time_factor,
        "audio_sha256": audio_manifest["sha256"],
        "audio_cache_hit": audio_cache_hit,
        "model_cache_hit": model_cache_hit,
        "backend": backend_name,
        "model": result.model,
        "model_revision": result.model_revision,
        "raw_output_filename": raw_filename,
        "raw_output_path": str(raw_path.relative_to(files.root)),
        "external_model_api_used": False,
        "cloud_asr_used": False,
        "completed_at": utc_now(),
    }
    atomic_write_json(files.root / "reports/asr-preflight-000-030.json", payload)
    return payload


def inspect_asr(job_id: str, *, config: AppConfig) -> dict[str, Any]:
    files, _, state = load_job_context(job_id, config=config)
    asr_manifest = files.root / "transcript/asr/asr-manifest.json"
    manifest = _load_json(asr_manifest) if asr_manifest.is_file() else None
    if manifest is not None:
        audio = files.root / str(manifest["audio_path"])
        audio_manifest = audio.with_suffix(".json")
    else:
        backend_name = _locked_transcript_backend(state) or _select_backend(config, None)
        if hasattr(files, "media_manifest") and files.media_manifest.is_file():
            media = MediaManifest.model_validate(_load_json(files.media_manifest))
            audio, audio_manifest = _audio_paths(files.root, media.duration, backend=backend_name)
        else:
            audio, audio_manifest = _audio_paths(files.root, backend=backend_name)
    integrity = False
    if manifest is not None:
        integrity = _cached_transcript(files.root, str(manifest["input_hash"])) is not None
    return {
        "schema_version": "1.0",
        "job_id": job_id,
        "audio_ready": audio.is_file() and audio_manifest.is_file(),
        "audio_sha256_matches": (
            hash_file(audio) == _load_json(audio_manifest)["sha256"]
            if audio.is_file() and audio_manifest.is_file()
            else False
        ),
        "transcript_ready": manifest is not None,
        "transcript_integrity": integrity,
        "stage_status": state.stages["transcript_ready"].status.value,
        "manifest": manifest,
    }
