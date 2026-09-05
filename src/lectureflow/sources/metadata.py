from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from lectureflow.errors import CommandError
from lectureflow.process import first_version_line, run_command
from lectureflow.schemas.manifest import SourceDescriptor


def tool_version(executable: str) -> str | None:
    if Path(executable).name == "yt-dlp":
        return first_version_line(executable, "--version")
    return first_version_line(executable, "-version")


def available_tool_version(name: str) -> str | None:
    executable = shutil.which(name)
    return tool_version(executable) if executable is not None else None


def probe_local(source: SourceDescriptor) -> tuple[dict[str, Any], str | None]:
    executable = shutil.which("ffprobe")
    if executable is None:
        raise CommandError(
            "FFprobe is required for local video metadata. Run 'lectureflow doctor'."
        )
    result = run_command(
        (
            executable,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            Path(source.normalized),
        ),
        timeout=120,
    )
    try:
        metadata = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CommandError(f"FFprobe returned invalid JSON: {exc}") from exc
    metadata["lectureflow_source"] = "local"
    return metadata, tool_version(executable)


def probe_bilibili(source: SourceDescriptor) -> tuple[dict[str, Any], str | None]:
    executable = shutil.which("yt-dlp")
    if executable is None:
        raise CommandError(
            "yt-dlp is required for Bilibili metadata. Restore the project environment with "
            "'uv sync --dev', then run 'lectureflow resume <job-id>'."
        )
    argv = [
        executable,
        "--ignore-config",
        "--dump-single-json",
        "--skip-download",
    ]
    argv.append(source.normalized)
    result = run_command(tuple(argv), timeout=180)
    try:
        metadata = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CommandError(f"yt-dlp returned invalid JSON: {exc}") from exc
    metadata["lectureflow_source"] = "bilibili"
    metadata["lectureflow_page"] = source.page
    return metadata, tool_version(executable)
