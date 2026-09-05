from __future__ import annotations

import math
import tomllib
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lectureflow.errors import ConfigurationError


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PipelineConfig(StrictModel):
    packet_seconds: int = Field(default=420, ge=60, le=3600)
    packet_overlap_seconds: int = Field(default=30, ge=0, le=300)
    codex_cli_enabled: bool = False
    codex_cli_concurrency: int = Field(default=1, ge=1, le=1)
    codex_cli_max_retries: int = Field(default=1, ge=0, le=3)


class SubtitleConfig(StrictModel):
    preferred_languages: list[str]
    sources: list[str]
    http_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    http_retries: int = Field(default=2, ge=0, le=5)
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 LectureFlow-Local/0.3"
    )

    @field_validator("sources")
    @classmethod
    def require_source_priority(cls, value: list[str]) -> list[str]:
        required = [
            "uploader",
            "bilibili_ai",
            "local_file",
            "mlx_whisper",
            "faster_whisper",
        ]
        if value != required:
            raise ValueError(f"sources must preserve the required priority order: {required}")
        return value

    @field_validator("preferred_languages")
    @classmethod
    def require_languages(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        if not normalized:
            raise ValueError("preferred_languages cannot be empty")
        if len(set(item.lower() for item in normalized)) != len(normalized):
            raise ValueError("preferred_languages cannot contain duplicates")
        return normalized

    @field_validator("user_agent")
    @classmethod
    def require_safe_user_agent(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\n" in normalized or "\r" in normalized:
            raise ValueError("user_agent must be a non-empty single line")
        return normalized


class ASRConfig(StrictModel):
    preferred_backend: str = "auto"
    smoke_model: str = "small"
    production_model: str = "medium"
    model_path: str = ""
    mlx_model: str = "mlx-community/whisper-medium-mlx"
    mlx_revision: str = "7fc08c4eac4c316526498f147dfdee6f6303f975"
    vibeasr_model: str = "microsoft/VibeVoice-ASR-BitNet"
    vibeasr_revision: str = "66e78021ab8f5f06133d1ab421ba4d348bda97c9"
    vibeasr_runtime_revision: str = "5cbce71c65911a7e10639ac13b6ab6929e4c8f9e"
    vibeasr_executable: str = ""
    vibeasr_executable_sha256: str = ""
    vibeasr_model_path: str = ""
    vibeasr_threads: int = Field(default=4, ge=1, le=16)
    vibeasr_chunk_seconds: float = Field(default=30.0, ge=5.0, le=40.0)
    vibeasr_context_size: int = Field(default=16384, ge=4096, le=65536)
    vibeasr_max_tokens: int = Field(default=4096, ge=256, le=32768)
    vibeasr_hotwords: list[str] = Field(default_factory=list)

    @field_validator("preferred_backend")
    @classmethod
    def valid_backend(cls, value: str) -> str:
        normalized = value.replace("-", "_")
        if normalized not in {
            "auto",
            "mlx_whisper",
            "faster_whisper",
            "vibeasr_bitnet",
        }:
            raise ValueError(
                "preferred_backend must be auto, mlx_whisper, faster_whisper, or vibeasr_bitnet"
            )
        return normalized

    @field_validator("vibeasr_hotwords")
    @classmethod
    def valid_vibeasr_hotwords(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(
            not item or len(item) > 200 or "\n" in item or "\r" in item or "\0" in item
            for item in normalized
        ):
            raise ValueError(
                "vibeasr_hotwords must contain non-empty single-line terms up to 200 characters"
            )
        if len(set(item.casefold() for item in normalized)) != len(normalized):
            raise ValueError("vibeasr_hotwords cannot contain case-insensitive duplicates")
        if len(", ".join(normalized)) > 4096:
            raise ValueError("vibeasr_hotwords exceed the 4096-character local context limit")
        return normalized

    @field_validator("vibeasr_executable_sha256")
    @classmethod
    def valid_vibeasr_executable_sha256(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized and (
            len(normalized) != 64
            or any(character not in "0123456789abcdef" for character in normalized)
        ):
            raise ValueError("vibeasr_executable_sha256 must be an empty value or 64 hex digits")
        return normalized


class FrameConfig(StrictModel):
    scene_threshold: float = Field(default=0.32, ge=0, le=1)
    periodic_interval_seconds: float = Field(default=90, ge=1)
    cue_offsets_seconds: list[float] = Field(default_factory=lambda: [-1.0, 0.0, 1.0])
    duplicate_threshold: int = Field(default=6, ge=0, le=64)
    progressive_slide_protection_seconds: float = Field(default=20.0, ge=0, le=600)
    local_change_ratio_threshold: float = Field(default=0.002, ge=0, le=1)
    edge_change_ratio_threshold: float = Field(default=0.001, ge=0, le=1)
    high_contrast_change_ratio_threshold: float = Field(default=0.0005, ge=0, le=1)
    black_frame_threshold: float = Field(default=0.97, ge=0, le=1)
    dark_luma_threshold: float = Field(default=24.0, ge=0, le=255)
    low_information_stddev: float = Field(default=6.0, ge=0, le=128)
    max_candidate_frames: int = Field(default=240, ge=1, le=5000)
    max_final_frames: int = Field(default=30, ge=1, le=1000)
    contact_sheet_columns: int = Field(default=4, ge=1, le=10)
    contact_sheet_width: int = Field(default=1600, ge=400, le=8000)
    contact_sheet_max_frames: int = Field(default=12, ge=1, le=100)
    subtitle_cues: list[str]
    protected_cue_terms: list[str] = Field(
        default_factory=lambda: [
            "公式",
            "式子",
            "损失函数",
            "目标函数",
            "梯度",
            "概率",
            "期望",
            "矩阵",
            "向量",
            "求和",
            "定义",
            "推导",
            "代码",
        ]
    )

    @field_validator("cue_offsets_seconds")
    @classmethod
    def require_finite_cue_offsets(cls, value: list[float]) -> list[float]:
        if not value or any(not math.isfinite(item) for item in value):
            raise ValueError("cue_offsets_seconds must contain finite values")
        return sorted(set(value))


class WebConfig(StrictModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)

    @field_validator("host")
    @classmethod
    def only_loopback(cls, value: str) -> str:
        if value != "127.0.0.1":
            raise ValueError("LectureFlow web service must bind exactly to 127.0.0.1")
        return value


class PrivacyConfig(StrictModel):
    allow_browser_cookie_access: bool = False
    allow_external_model_api: bool = False

    @field_validator("allow_browser_cookie_access", "allow_external_model_api")
    @classmethod
    def forbidden_capabilities_stay_off(cls, value: bool) -> bool:
        if value:
            raise ValueError("this capability is forbidden by the LectureFlow privacy boundary")
        return value


class AppConfig(StrictModel):
    schema_version: str
    workspace_root: Path
    pipeline: PipelineConfig
    subtitles: SubtitleConfig
    asr: ASRConfig
    frames: FrameConfig
    web: WebConfig
    privacy: PrivacyConfig


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"Cannot read configuration {path}: {exc}") from exc


def load_config(path: Path | None = None, *, cwd: Path | None = None) -> AppConfig:
    try:
        default_resource = resources.files("lectureflow.resources").joinpath("default.toml")
        with default_resource.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"Bundled default configuration is invalid: {exc}") from exc

    base_dir = (cwd or Path.cwd()).resolve()
    if path is not None:
        try:
            config_path = path.expanduser().resolve(strict=True)
        except OSError as exc:
            raise ConfigurationError(f"Configuration file does not exist: {path}") from exc
        data = _deep_merge(data, _read_toml(config_path))
        base_dir = config_path.parent
    workspace_value = Path(data["workspace_root"]).expanduser()
    if not workspace_value.is_absolute():
        workspace_value = base_dir / workspace_value
    data["workspace_root"] = workspace_value.resolve()
    try:
        return AppConfig.model_validate(data)
    except ValueError as exc:
        raise ConfigurationError(f"Invalid configuration: {exc}") from exc
