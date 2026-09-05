from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from lectureflow.errors import SourceError
from lectureflow.schemas.manifest import SourceDescriptor
from lectureflow.sources.bilibili import parse_bilibili_url


def identify_source(value: str) -> SourceDescriptor:
    candidate = value.strip()
    if not candidate:
        raise SourceError("Source cannot be empty.")
    if re.fullmatch(r"(?i)(BV[0-9A-Za-z]{10}|av[0-9]+)", candidate):
        return parse_bilibili_url(candidate)
    parts = urlsplit(candidate)
    if parts.scheme in {"http", "https"}:
        return parse_bilibili_url(candidate)
    if parts.scheme and len(parts.scheme) > 1:
        raise SourceError(f"Unsupported source URL scheme: {parts.scheme}")
    path = Path(candidate).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SourceError(f"Local source does not exist: {path}") from exc
    if not resolved.is_file():
        raise SourceError(f"Local source is not a regular file: {resolved}")
    try:
        with resolved.open("rb"):
            pass
    except OSError as exc:
        raise SourceError(f"Local source is not readable: {resolved}") from exc
    return SourceDescriptor(
        kind="local",
        normalized=str(resolved),
        display_name=resolved.name,
    )
