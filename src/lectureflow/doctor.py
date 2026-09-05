from __future__ import annotations

import importlib.util
import platform
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

from lectureflow.process import first_version_line


def _check(
    name: str,
    status: str,
    *,
    required: bool,
    detail: str,
    remediation: str | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "required": required,
        "detail": detail,
        "remediation": remediation,
    }


def _executable_check(
    name: str, *, required: bool, version_args: tuple[str, ...]
) -> dict[str, Any]:
    executable = shutil.which(name)
    if executable is None:
        return _check(
            name,
            "fail" if required else "warn",
            required=required,
            detail="not found on PATH",
            remediation=f"Install {name} and rerun 'lectureflow doctor'.",
        )
    version = first_version_line(executable, *version_args)
    if version is None:
        return _check(
            name,
            "fail" if required else "warn",
            required=required,
            detail=f"{executable} was found, but its version command failed",
            remediation=f"Repair {name} or adjust PATH, then rerun 'lectureflow doctor'.",
        )
    return _check(
        name,
        "pass",
        required=required,
        detail=f"{executable} ({version or 'version unavailable'})",
    )


def _sqlite_check() -> dict[str, Any]:
    try:
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE VIRTUAL TABLE lectureflow_fts USING fts5(content)")
        connection.execute("INSERT INTO lectureflow_fts VALUES ('时间轴测试')")
        row = connection.execute(
            "SELECT content FROM lectureflow_fts WHERE lectureflow_fts MATCH '时间轴测试'"
        ).fetchone()
        connection.close()
        if row is None:
            raise RuntimeError("FTS5 query returned no result")
    except (sqlite3.Error, RuntimeError) as exc:
        return _check(
            "sqlite_fts5",
            "fail",
            required=True,
            detail=f"SQLite {sqlite3.sqlite_version}; FTS5 unavailable: {exc}",
            remediation="Use a Python 3.11+ build whose SQLite library enables FTS5.",
        )
    return _check(
        "sqlite_fts5",
        "pass",
        required=True,
        detail=f"SQLite {sqlite3.sqlite_version}; FTS5 query passed",
    )


def _package_check(module: str, display: str) -> dict[str, Any]:
    available = importlib.util.find_spec(module) is not None
    return _check(
        display,
        "pass" if available else "warn",
        required=False,
        detail="available" if available else "not installed (optional backend)",
    )


def run_doctor(*, workspace_root: Path | None = None) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    python_ok = sys.version_info >= (3, 11)
    checks.append(
        _check(
            "python",
            "pass" if python_ok else "fail",
            required=True,
            detail=f"{sys.executable} ({platform.python_version()})",
            remediation=None if python_ok else "Run LectureFlow through uv with Python 3.11+.",
        )
    )
    machine_ok = sys.platform == "darwin" and platform.machine() == "arm64"
    checks.append(
        _check(
            "platform",
            "pass" if machine_ok else "warn",
            required=False,
            detail=f"{platform.system()} {platform.release()} / {platform.machine()}",
            remediation=None if machine_ok else "Primary support target is Apple Silicon macOS.",
        )
    )
    checks.extend(
        (
            _executable_check("ffmpeg", required=True, version_args=("-version",)),
            _executable_check("ffprobe", required=True, version_args=("-version",)),
            _executable_check("yt-dlp", required=False, version_args=("--version",)),
            _executable_check("git", required=False, version_args=("--version",)),
            _executable_check("codex", required=False, version_args=("--version",)),
            _sqlite_check(),
            _package_check("mlx_whisper", "mlx-whisper"),
            _package_check("faster_whisper", "faster-whisper"),
        )
    )
    if workspace_root is not None:
        parent = workspace_root.expanduser().resolve().parent
        writable = parent.exists() and os_access_writable(parent)
        checks.append(
            _check(
                "workspace_parent",
                "pass" if writable else "fail",
                required=True,
                detail=str(parent),
                remediation=None if writable else "Choose a workspace under a writable directory.",
            )
        )
    required_ok = all(item["status"] == "pass" for item in checks if item["required"])
    strict_ok = all(item["status"] == "pass" for item in checks)
    return {
        "schema_version": "1.0.0",
        "ok": required_ok,
        "strict_ok": strict_ok,
        "checks": checks,
        "privacy": {
            "network_used": False,
            "cookies_inspected": False,
            "external_model_api_used": False,
        },
    }


def os_access_writable(path: Path) -> bool:
    import os

    return os.access(path, os.W_OK | os.X_OK)
