from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit, urlunsplit

from lectureflow.errors import SourceError
from lectureflow.schemas.manifest import SourceDescriptor

_BV_PATTERN = re.compile(r"(?i)^BV[0-9A-Za-z]{10}$")
_AV_PATTERN = re.compile(r"(?i)^av(?P<aid>[0-9]+)$")


def _is_bilibili_host(hostname: str) -> bool:
    host = hostname.lower().rstrip(".")
    return host == "bilibili.com" or host.endswith(".bilibili.com") or host == "b23.tv"


def parse_bilibili_url(value: str) -> SourceDescriptor:
    candidate = value.strip()
    if _BV_PATTERN.fullmatch(candidate):
        return SourceDescriptor(
            kind="bilibili",
            normalized=f"https://www.bilibili.com/video/{candidate}",
            display_name=candidate,
            bvid=candidate,
            page=None,
        )
    av_match = _AV_PATTERN.fullmatch(candidate)
    if av_match:
        aid = int(av_match.group("aid"))
        return SourceDescriptor(
            kind="bilibili",
            normalized=f"https://www.bilibili.com/video/av{aid}",
            display_name=f"av{aid}",
            aid=aid,
            page=None,
        )

    parts = urlsplit(candidate)
    if parts.scheme not in {"http", "https"} or not _is_bilibili_host(parts.hostname or ""):
        raise SourceError("Only bilibili.com and b23.tv video URLs are supported.")
    query = parse_qs(parts.query)
    page: int | None = None
    if "p" in query:
        try:
            page = int(query["p"][0])
        except ValueError as exc:
            raise SourceError("Bilibili query parameter 'p' must be a positive integer.") from exc
        if page < 1:
            raise SourceError("Bilibili query parameter 'p' must be a positive integer.")

    path_parts = [item for item in parts.path.split("/") if item]
    identifier: str | None = None
    if len(path_parts) >= 2 and path_parts[0].lower() == "video":
        identifier = path_parts[1]
    bvid: str | None = None
    aid: int | None = None
    if identifier and _BV_PATTERN.fullmatch(identifier):
        bvid = identifier
    elif identifier:
        match = _AV_PATTERN.fullmatch(identifier)
        if match:
            aid = int(match.group("aid"))

    short_url = (parts.hostname or "").lower() == "b23.tv"
    if not short_url and bvid is None and aid is None:
        raise SourceError("The Bilibili URL does not contain a valid BV or av video ID.")
    if short_url:
        selected_query = f"p={page}" if page is not None else ""
        normalized = urlunsplit(("https", "b23.tv", parts.path, selected_query, ""))
        display = parts.path.strip("/") or "b23-link"
    else:
        video_id = bvid or f"av{aid}"
        normalized = f"https://www.bilibili.com/video/{video_id}"
        if page is not None:
            normalized = f"{normalized}?p={page}"
        display = str(video_id)
    return SourceDescriptor(
        kind="bilibili",
        normalized=normalized,
        display_name=display,
        bvid=bvid,
        aid=aid,
        page=page,
        short_url=short_url,
    )
