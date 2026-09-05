from __future__ import annotations

import importlib.util
import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from lectureflow.config import ASRConfig
from lectureflow.errors import ASRUnavailableError
from lectureflow.hashing import hash_file
from lectureflow.schemas.asr import ASRBackendInfo, ASRTranscriptResult, TranscriptionRequest


@dataclass(frozen=True, slots=True)
class ASRCapability:
    backend: str
    available: bool
    module: str
    configured_model: str
    configured_model_path: str | None
    reason: str


class ASRBackend(ABC):
    name: str

    @abstractmethod
    def is_available(self) -> bool:
        """Return whether this local backend can execute now."""

    @abstractmethod
    def describe(self) -> ASRBackendInfo:
        """Describe the installed package and verified local model."""

    @abstractmethod
    def transcribe(self, request: TranscriptionRequest) -> ASRTranscriptResult:
        """Transcribe local audio without using an external service."""


def detect_asr_capabilities(config: ASRConfig) -> list[ASRCapability]:
    definitions = (
        ("mlx_whisper", "mlx_whisper"),
        ("faster_whisper", "faster_whisper"),
    )
    configured_path = config.model_path or None
    capabilities = []
    for backend, module in definitions:
        installed = importlib.util.find_spec(module) is not None
        if not installed:
            reason = (
                "Python package is not installed. No model was downloaded. "
                "Install the selected backend later, then configure an existing local model."
            )
        elif not configured_path:
            reason = (
                "Backend package is installed, but no verified local model_path is configured. "
                "Automatic model download is disabled."
            )
        elif not Path(configured_path).expanduser().exists():
            reason = "Configured local model path does not exist."
        else:
            reason = "Backend package and configured local model path are available."
        capabilities.append(
            ASRCapability(
                backend=backend,
                available=installed
                and bool(configured_path)
                and Path(configured_path).expanduser().exists(),
                module=module,
                configured_model=config.production_model,
                configured_model_path=configured_path,
                reason=reason,
            )
        )
    from lectureflow.asr.vibeasr_backend import (
        VIBEASR_LM_BYTES,
        VIBEASR_LM_FILENAME,
        VIBEASR_LM_SHA256,
        VIBEASR_MODEL_ID,
        VIBEASR_MODEL_REVISION,
        VIBEASR_RUNTIME_REVISION,
        VIBEASR_VAE_BYTES,
        VIBEASR_VAE_FILENAME,
        VIBEASR_VAE_SHA256,
    )

    executable = (
        Path(config.vibeasr_executable).expanduser()
        if config.vibeasr_executable
        else Path(shutil.which("asr_infer") or "")
    )
    vibe_model_path = (
        Path(config.vibeasr_model_path).expanduser() if config.vibeasr_model_path else None
    )
    vibe_files = (
        [
            (vibe_model_path / VIBEASR_VAE_FILENAME, VIBEASR_VAE_BYTES, VIBEASR_VAE_SHA256),
            (vibe_model_path / VIBEASR_LM_FILENAME, VIBEASR_LM_BYTES, VIBEASR_LM_SHA256),
        ]
        if vibe_model_path is not None
        else []
    )
    executable_hash_matches = (
        executable.is_file()
        and bool(config.vibeasr_executable_sha256)
        and hash_file(executable) == config.vibeasr_executable_sha256
    )
    vibe_available = (
        bool(str(executable))
        and executable.is_file()
        and os.access(executable, os.X_OK)
        and executable_hash_matches
        and config.vibeasr_runtime_revision == VIBEASR_RUNTIME_REVISION
        and config.vibeasr_model == VIBEASR_MODEL_ID
        and config.vibeasr_revision == VIBEASR_MODEL_REVISION
        and len(vibe_files) == 2
        and all(
            path.is_file() and path.stat().st_size == size and hash_file(path) == digest
            for path, size, digest in vibe_files
        )
    )
    capabilities.append(
        ASRCapability(
            backend="vibeasr_bitnet",
            available=vibe_available,
            module="asr_infer",
            configured_model=config.vibeasr_model,
            configured_model_path=str(vibe_model_path) if vibe_model_path else None,
            reason=(
                "Pinned VibeASR.cpp executable and configured local model files are verified."
                if vibe_available
                else "VibeASR.cpp or its configured local GGUF files are unavailable. "
                "No model was downloaded and automatic model download is disabled."
            ),
        )
    )
    return capabilities


def require_asr_backend(config: ASRConfig) -> ASRCapability:
    capabilities = detect_asr_capabilities(config)
    preferred = config.preferred_backend
    if preferred == "auto":
        selected = next(
            (item for item in capabilities if item.available and item.backend != "vibeasr_bitnet"),
            None,
        )
    else:
        normalized = preferred.replace("-", "_")
        selected = next(
            (item for item in capabilities if item.backend == normalized and item.available),
            None,
        )
    if selected is None:
        summary = "; ".join(f"{item.backend}: {item.reason}" for item in capabilities)
        raise ASRUnavailableError(
            "No configured local ASR backend/model is available. "
            f"No model download was attempted. {summary}"
        )
    return selected
