from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    ).encode()


def hash_object(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def hash_file(path: Path, *, block_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(block_size):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint_local_file(path: Path, *, sample_size: int = 1024 * 1024) -> dict[str, Any]:
    """Hash file identity cheaply without mutating or reading the whole course video."""
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        head = handle.read(sample_size)
        digest.update(head)
        if stat.st_size > sample_size:
            handle.seek(max(0, stat.st_size - sample_size))
            digest.update(handle.read(sample_size))
    return {
        "path": str(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sample_sha256": digest.hexdigest(),
        "sample_bytes": min(stat.st_size, sample_size * 2),
    }
