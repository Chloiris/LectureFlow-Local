"""Local-only ASR backend contracts, execution, and capability detection."""

from lectureflow.asr.base import ASRBackend, ASRCapability, detect_asr_capabilities
from lectureflow.asr.mlx_backend import MLXWhisperBackend
from lectureflow.asr.service import (
    ASRProcessResult,
    asr_doctor,
    inspect_asr,
    preflight_job,
    transcribe_job,
)
from lectureflow.asr.vibeasr_backend import VibeASRBitNetBackend

__all__ = [
    "ASRBackend",
    "ASRCapability",
    "ASRProcessResult",
    "MLXWhisperBackend",
    "VibeASRBitNetBackend",
    "asr_doctor",
    "detect_asr_capabilities",
    "inspect_asr",
    "preflight_job",
    "transcribe_job",
]
