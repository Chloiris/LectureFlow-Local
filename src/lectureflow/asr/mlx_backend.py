from __future__ import annotations

import importlib.util
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from lectureflow.asr.base import ASRBackend
from lectureflow.atomic import atomic_write_json
from lectureflow.errors import ASRUnavailableError
from lectureflow.schemas.asr import ASRBackendInfo, ASRTranscriptResult, TranscriptionRequest
from lectureflow.schemas.transcript import TranscriptSegment
from lectureflow.subtitles.formats import clean_text


def _package_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "item"):
        return value.item()
    return str(value)


class MLXWhisperBackend(ASRBackend):
    name = "mlx-whisper"

    def __init__(self, *, model: str, revision: str, model_path: Path) -> None:
        self.model = model
        self.revision = revision
        self.model_path = model_path

    def is_available(self) -> bool:
        return (
            importlib.util.find_spec("mlx") is not None
            and importlib.util.find_spec("mlx_whisper") is not None
            and (self.model_path / "config.json").is_file()
            and any(
                (self.model_path / name).is_file()
                for name in ("weights.npz", "weights.safetensors")
            )
        )

    def describe(self) -> ASRBackendInfo:
        available = self.is_available()
        return ASRBackendInfo(
            backend="mlx-whisper",
            available=available,
            package_version=_package_version("mlx-whisper"),
            model=self.model,
            revision=self.revision,
            model_path=str(self.model_path),
            model_cached=available,
            reason=(
                "MLX package and pinned local model snapshot are available."
                if available
                else "MLX package or pinned local model snapshot is unavailable."
            ),
        )

    def transcribe(self, request: TranscriptionRequest) -> ASRTranscriptResult:
        if not self.is_available():
            raise ASRUnavailableError(
                "MLX-Whisper is unavailable or the pinned model is not cached. "
                "Run 'lectureflow asr doctor'; strict offline mode never downloads a model."
            )
        if request.model_path.resolve() != self.model_path.resolve():
            raise ASRUnavailableError(
                "Transcription request model path differs from backend model."
            )
        import mlx_whisper

        started = time.perf_counter()
        try:
            raw = mlx_whisper.transcribe(
                str(request.audio_path),
                path_or_hf_repo=str(self.model_path),
                language=request.language,
                task=request.task,
                word_timestamps=request.word_timestamps,
                clip_timestamps=f"{request.start},{request.end}",
                verbose=False,
            )
        except Exception as error:
            raise ASRUnavailableError(f"Local MLX-Whisper transcription failed: {error}") from error
        elapsed = time.perf_counter() - started
        safe_raw = _json_safe(raw)
        atomic_write_json(request.raw_output_path, safe_raw)
        raw_segments = safe_raw.get("segments", []) if isinstance(safe_raw, dict) else []
        segments: list[TranscriptSegment] = []
        for index, item in enumerate(raw_segments, 1):
            if not isinstance(item, dict):
                raise ASRUnavailableError(f"MLX-Whisper segment {index} is not an object.")
            try:
                raw_start = float(item["start"])
                raw_end = float(item["end"])
                text = str(item["text"])
            except (KeyError, TypeError, ValueError) as error:
                raise ASRUnavailableError(
                    f"Invalid MLX-Whisper segment {index}: {error}"
                ) from error
            if raw_start < request.start - 0.001 or raw_end < raw_start:
                raise ASRUnavailableError(
                    f"Invalid MLX-Whisper segment timing {index}: {raw_start:.3f}-{raw_end:.3f}."
                )
            if raw_start >= request.end:
                continue
            start = max(raw_start, request.start)
            end = min(raw_end, request.end)
            segments.append(
                TranscriptSegment(
                    segment_id=f"T{index:06d}",
                    start=start,
                    end=end,
                    text_raw=text,
                    text_clean=clean_text(text),
                    source="mlx_whisper",
                    language=request.language,
                    confidence=None,
                    speaker=None,
                    chapter_id=None,
                    part_id=request.part_id,
                    source_ref={
                        "raw_segment_index": index - 1,
                        "raw_start": raw_start,
                        "raw_end": raw_end,
                        "range_boundary_clipped": end != raw_end or start != raw_start,
                    },
                )
            )
        return ASRTranscriptResult(
            backend="mlx-whisper",
            model=self.model,
            model_revision=self.revision,
            language=request.language,
            duration_seconds=request.end,
            segments=segments,
            raw_output_path=str(request.raw_output_path),
            elapsed_seconds=elapsed,
            real_time_factor=elapsed / (request.end - request.start),
        )
