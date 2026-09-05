from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Atomically replace *path* with bytes and fsync both file and directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        separators=(",", ": "),
        allow_nan=False,
    )
    atomic_write_bytes(path, f"{payload}\n".encode())


def atomic_write_text(path: Path, value: str) -> None:
    atomic_write_bytes(path, value.encode("utf-8"))


def append_jsonl(path: Path, value: Any) -> None:
    """Append one compact JSON object and make it durable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    encoded = f"{payload}\n".encode()
    created = not path.exists()
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    fd = os.open(path, flags, 0o600)
    try:
        offset = 0
        while offset < len(encoded):
            written = os.write(fd, encoded[offset:])
            if written == 0:
                raise OSError("short write while appending JSONL event")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)
    if created:
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
