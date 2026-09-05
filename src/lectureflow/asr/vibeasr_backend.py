from __future__ import annotations

import os
import re
import tempfile
import time
import wave
from math import ceil
from pathlib import Path

from lectureflow.asr.base import ASRBackend
from lectureflow.atomic import atomic_write_json
from lectureflow.errors import ASRUnavailableError, CommandError
from lectureflow.hashing import hash_file, hash_object
from lectureflow.process import run_command
from lectureflow.schemas.asr import ASRBackendInfo, ASRTranscriptResult, TranscriptionRequest
from lectureflow.schemas.transcript import TranscriptSegment
from lectureflow.subtitles.formats import clean_text

VIBEASR_VAE_FILENAME = "vibeasr-vae-encoder-i8_s.gguf"
VIBEASR_LM_FILENAME = "vibeasr-lm-i2_s-embed-q6_k.gguf"
VIBEASR_MODEL_ID = "microsoft/VibeVoice-ASR-BitNet"
VIBEASR_MODEL_REVISION = "66e78021ab8f5f06133d1ab421ba4d348bda97c9"
VIBEASR_RUNTIME_REVISION = "5cbce71c65911a7e10639ac13b6ab6929e4c8f9e"
VIBEASR_VAE_BYTES = 703_080_064
VIBEASR_LM_BYTES = 992_877_600
VIBEASR_MODEL_BYTES = VIBEASR_VAE_BYTES + VIBEASR_LM_BYTES
VIBEASR_VAE_SHA256 = "4941c82608c253ec066b5cc74d3dd11a5c8fef96cccbc5b87359ef0fe4338df6"
VIBEASR_LM_SHA256 = "fbe273d8dc2f2433bb25f849e19d77ea65aaa2188d12c20cee987ab6f321e002"

_RTF = re.compile(r"\bRTF:\s*([0-9]+(?:\.[0-9]+)?)")
_TOKENS = re.compile(r"\bTokens:\s*(\d+)")
_RUNTIME_FAILURE = re.compile(
    r"(?:llama_decode|decode|prefill)[^\n]*(?:failed|error|returned\s+-)"
    r"|^\s*(?:\[[^\]]+\]\s*)?Error:",
    re.IGNORECASE | re.MULTILINE,
)


def _wave_facts(path: Path) -> tuple[float, int, int, int]:
    try:
        with wave.open(str(path), "rb") as source:
            frames = source.getnframes()
            rate = source.getframerate()
            return frames / rate, rate, source.getnchannels(), source.getsampwidth()
    except (OSError, wave.Error, ZeroDivisionError) as error:
        raise ASRUnavailableError(f"Cannot read VibeASR PCM input: {error}") from error


def _slice_pcm_wave(source_path: Path, start: float, end: float, destination: Path) -> None:
    try:
        with wave.open(str(source_path), "rb") as source:
            params = source.getparams()
            first_frame = round(start * params.framerate)
            frame_count = round((end - start) * params.framerate)
            source.setpos(first_frame)
            frames = source.readframes(frame_count)
        with wave.open(str(destination), "wb") as output:
            output.setparams(params)
            output.writeframes(frames)
    except (OSError, wave.Error) as error:
        raise ASRUnavailableError(f"Cannot create bounded VibeASR PCM input: {error}") from error


class VibeASRBitNetBackend(ASRBackend):
    """Local-only adapter for Microsoft's official VibeASR.cpp BitNet runtime."""

    name = "vibeasr-bitnet"

    def __init__(
        self,
        *,
        executable: Path,
        model: str,
        revision: str,
        model_path: Path,
        threads: int = 4,
        chunk_seconds: float = 30.0,
        context_size: int = 16384,
        max_tokens: int = 4096,
    ) -> None:
        self.executable = executable
        self.model = model
        self.revision = revision
        self.model_path = model_path
        self.threads = threads
        self.chunk_seconds = chunk_seconds
        self.context_size = context_size
        self.max_tokens = max_tokens

    @property
    def vae_path(self) -> Path:
        return self.model_path / VIBEASR_VAE_FILENAME

    @property
    def lm_path(self) -> Path:
        return self.model_path / VIBEASR_LM_FILENAME

    def is_available(self) -> bool:
        return (
            self.executable.is_file()
            and os.access(self.executable, os.X_OK)
            and self.vae_path.is_file()
            and self.vae_path.stat().st_size == VIBEASR_VAE_BYTES
            and self.lm_path.is_file()
            and self.lm_path.stat().st_size == VIBEASR_LM_BYTES
        )

    def describe(self) -> ASRBackendInfo:
        available = self.is_available()
        binary_version = (
            f"sha256:{hash_file(self.executable)[:12]}" if self.executable.is_file() else None
        )
        return ASRBackendInfo(
            backend="vibeasr-bitnet",
            available=available,
            package_version=binary_version,
            model=self.model,
            revision=self.revision,
            model_path=str(self.model_path),
            model_cached=available,
            reason=(
                "Official VibeASR.cpp executable and both pinned GGUF files are available."
                if available
                else "Configure an executable official VibeASR.cpp binary and the two pinned "
                "BitNet GGUF files; LectureFlow does not download or build them automatically."
            ),
        )

    def transcribe(self, request: TranscriptionRequest) -> ASRTranscriptResult:
        if not self.is_available():
            raise ASRUnavailableError(self.describe().reason)
        if request.model != self.model:
            raise ASRUnavailableError("VibeASR request model differs from the configured model.")
        if request.model_path.resolve() != self.model_path.resolve():
            raise ASRUnavailableError(
                "VibeASR request model path differs from the configured model directory."
            )
        duration, sample_rate, channels, sample_width = _wave_facts(request.audio_path)
        if sample_rate != 24000 or channels != 1 or sample_width != 2:
            raise ASRUnavailableError(
                "VibeASR requires 24 kHz mono 16-bit PCM WAV input; run through the "
                "LectureFlow VibeASR audio preparation stage."
            )
        if request.start > duration + 0.25 or request.end > duration + 0.25:
            raise ASRUnavailableError(
                f"VibeASR request range {request.start:.3f}-{request.end:.3f}s exceeds "
                f"the {duration:.3f}s local audio."
            )
        request.raw_output_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        segments: list[TranscriptSegment] = []
        raw_chunks: list[dict[str, object]] = []
        chunk_start = request.start
        chunk_index = 0
        while chunk_start < request.end - 0.001:
            chunk_index += 1
            chunk_end = min(request.end, chunk_start + self.chunk_seconds)
            chunk_duration = chunk_end - chunk_start
            speech_tokens = ceil(chunk_duration * 24000 / 3200)
            required_context = speech_tokens + self.max_tokens + len(request.context or "") + 2048
            if required_context > self.context_size:
                raise ASRUnavailableError(
                    "VibeASR chunk exceeds the audited context budget "
                    f"({required_context}>{self.context_size} tokens including reserves)."
                )
            inference_audio = request.audio_path
            temporary: Path | None = None
            if chunk_start > 0.001 or abs(chunk_end - duration) > 0.001:
                descriptor, name = tempfile.mkstemp(
                    prefix=f"vibeasr-chunk-{chunk_index:04d}-",
                    suffix=".wav",
                    dir=request.raw_output_path.parent,
                )
                os.close(descriptor)
                temporary = Path(name)
                _slice_pcm_wave(request.audio_path, chunk_start, chunk_end, temporary)
                inference_audio = temporary
            argv: list[str | Path] = [
                self.executable,
                "--vae-model",
                self.vae_path,
                "--lm-model",
                self.lm_path,
                "--audio",
                inference_audio,
                "-t",
                str(self.threads),
                "-c",
                str(self.context_size),
                "--max-tokens",
                str(self.max_tokens),
                "--greedy",
                "--prompt-format",
                "text",
            ]
            if request.context:
                argv.extend(("--context", request.context))
            try:
                process = run_command(argv, timeout=max(600, int(chunk_duration * 3)))
            except CommandError as error:
                raise ASRUnavailableError(
                    f"Local VibeASR.cpp chunk {chunk_index} failed: {error}"
                ) from error
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            if _RUNTIME_FAILURE.search(process.stderr):
                raise ASRUnavailableError(
                    f"VibeASR.cpp chunk {chunk_index} reported a fatal runtime failure."
                )
            token_match = _TOKENS.search(process.stderr)
            reported_tokens = int(token_match.group(1)) if token_match else None
            if reported_tokens is not None and reported_tokens >= self.max_tokens:
                raise ASRUnavailableError(
                    f"VibeASR.cpp chunk {chunk_index} reached the output token limit; refusing "
                    "a possibly truncated transcript."
                )
            text = process.stdout.strip()
            rtf_match = _RTF.search(process.stderr)
            raw_chunks.append(
                {
                    "chunk_index": chunk_index,
                    "start": chunk_start,
                    "end": chunk_end,
                    "stdout": process.stdout,
                    "reported_rtf": float(rtf_match.group(1)) if rtf_match else None,
                    "reported_tokens": reported_tokens,
                    "empty_text": not bool(text),
                }
            )
            if text:
                segments.append(
                    TranscriptSegment(
                        segment_id=f"T{chunk_index:06d}",
                        start=chunk_start,
                        end=chunk_end,
                        text_raw=text,
                        text_clean=clean_text(text),
                        source="vibeasr_bitnet",
                        language=request.language,
                        confidence=None,
                        speaker=None,
                        chapter_id=None,
                        part_id=request.part_id,
                        source_ref={
                            "raw_chunk_index": chunk_index - 1,
                            "timestamp_basis": "deterministic_audio_chunk_bounds",
                            "model_timestamps_available": False,
                            "speaker_diarization_available": False,
                        },
                    )
                )
            chunk_start = chunk_end
        elapsed = time.perf_counter() - started
        if not segments:
            raise ASRUnavailableError(
                "VibeASR.cpp returned no transcript text in any deterministic audio chunk."
            )
        atomic_write_json(
            request.raw_output_path,
            {
                "schema_version": "1.0",
                "backend": self.name,
                "model": self.model,
                "revision": self.revision,
                "prompt_format": "text",
                "chunks": raw_chunks,
                "chunk_seconds": self.chunk_seconds,
                "context_size": self.context_size,
                "max_tokens": self.max_tokens,
                "context_sha256": hash_object(request.context) if request.context else None,
                "model_timestamps_available": False,
                "speaker_diarization_available": False,
                "external_model_api_used": False,
            },
        )
        return ASRTranscriptResult(
            backend=self.name,
            model=self.model,
            model_revision=self.revision,
            language=request.language,
            duration_seconds=request.end,
            segments=segments,
            raw_output_path=str(request.raw_output_path),
            elapsed_seconds=elapsed,
            real_time_factor=elapsed / (request.end - request.start),
        )
