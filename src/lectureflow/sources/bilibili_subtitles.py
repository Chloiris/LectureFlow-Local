from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

from lectureflow.errors import NetworkError, SubtitleError
from lectureflow.hashing import hash_object
from lectureflow.network import HttpClient
from lectureflow.schemas.manifest import SourceDescriptor
from lectureflow.schemas.transcript import SubtitleCandidate, SubtitleSelection
from lectureflow.security import redact, redact_text, sanitize_url

BILIBILI_API = "https://api.bilibili.com"
_BVID = re.compile(r"BV[0-9A-Za-z]{10}", re.IGNORECASE)
_SUBTITLE_HOST_SUFFIXES = (".hdslb.com", ".bilibili.com", ".bilivideo.com")


@dataclass(frozen=True, slots=True)
class BilibiliPart:
    part_id: str
    page: int
    cid: int
    title: str
    duration: float | None
    bvid: str
    aid: int | None
    published_at: int | None


def _api_url(path: str, query: dict[str, Any]) -> str:
    return f"{BILIBILI_API}{path}?{urlencode(query)}"


def _require_success(payload: dict[str, Any], *, operation: str) -> dict[str, Any]:
    code = payload.get("code", 0)
    if code != 0:
        message = payload.get("message") or payload.get("msg") or "unknown error"
        raise NetworkError(
            f"bilibili_api_error during {operation}: code={code}, "
            f"message={redact_text(str(message))}"
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise NetworkError(f"bilibili_invalid_response during {operation}: missing data object")
    return data


def _bvid_from_metadata(metadata: dict[str, Any]) -> str | None:
    candidates = [metadata.get("bvid"), metadata.get("id"), metadata.get("display_id")]
    entries = metadata.get("entries")
    if isinstance(entries, list) and entries:
        first = entries[0]
        if isinstance(first, dict):
            candidates.extend((first.get("bvid"), first.get("id"), first.get("display_id")))
    for candidate in candidates:
        if isinstance(candidate, str):
            match = _BVID.search(candidate)
            if match:
                return match.group(0)
    return None


def _require_subtitle_url(value: str) -> str:
    parts = urlsplit(value)
    hostname = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or not any(
        hostname == suffix.removeprefix(".") or hostname.endswith(suffix)
        for suffix in _SUBTITLE_HOST_SUFFIXES
    ):
        raise NetworkError("bilibili_invalid_response: untrusted subtitle download URL")
    return value


def _is_ai_subtitle(item: dict[str, Any]) -> bool:
    language = str(item.get("lan") or "").lower()
    language_name = str(item.get("lan_doc") or "").lower()
    ai_type = item.get("ai_type")
    ai_status = item.get("ai_status")
    subtitle_type = str(item.get("type") or "").lower()
    return (
        language.startswith("ai-")
        or "ai" in language_name
        or "自动" in language_name
        or "机翻" in language_name
        or ai_type not in (None, 0, "0", False)
        or ai_status not in (None, 0, "0", False)
        or subtitle_type in {"ai", "automatic", "auto"}
    )


class BilibiliSubtitleSource:
    def __init__(self, client: HttpClient) -> None:
        self.client = client

    def enumerate_parts(
        self,
        source: SourceDescriptor,
        *,
        metadata: dict[str, Any],
        force: bool = False,
    ) -> list[BilibiliPart]:
        bvid = source.bvid or _bvid_from_metadata(metadata)
        query: dict[str, Any]
        if bvid:
            query = {"bvid": bvid}
        elif source.aid is not None:
            query = {"aid": source.aid}
        else:
            raise SubtitleError(
                "Cannot resolve a BV/av identity from the short URL metadata. "
                "Run prepare again with a current yt-dlp, or use the canonical Bilibili URL."
            )
        url = _api_url("/x/web-interface/view", query)
        payload, _ = self.client.get_json(
            url,
            cache_key=f"view-{bvid or source.aid}",
            headers={"Referer": source.normalized},
            force=force,
        )
        data = _require_success(payload, operation="video parts")
        resolved_bvid = str(data.get("bvid") or bvid or "")
        if _BVID.fullmatch(resolved_bvid) is None:
            raise NetworkError("bilibili_invalid_response during video parts: invalid bvid")
        aid = int(data["aid"]) if data.get("aid") is not None else source.aid
        published_at = int(data["pubdate"]) if data.get("pubdate") is not None else None
        pages = data.get("pages")
        if not isinstance(pages, list):
            raise NetworkError("bilibili_invalid_response during video parts: missing pages")
        selected_pages = pages
        if source.page is not None:
            selected_pages = [item for item in pages if item.get("page") == source.page]
            if not selected_pages:
                raise SubtitleError(f"Bilibili part p={source.page} does not exist.")
        selected_pages = sorted(selected_pages, key=lambda item: int(item["page"]))
        parts: list[BilibiliPart] = []
        for item in selected_pages:
            page = int(item["page"])
            cid = int(item["cid"])
            duration = float(item["duration"]) if item.get("duration") is not None else None
            parts.append(
                BilibiliPart(
                    part_id=f"{resolved_bvid}-p{page}",
                    page=page,
                    cid=cid,
                    title=str(item.get("part") or f"P{page}"),
                    duration=duration,
                    bvid=resolved_bvid,
                    aid=aid,
                    published_at=published_at,
                )
            )
        return parts

    def enumerate_candidates(
        self,
        part: BilibiliPart,
        *,
        force: bool = False,
    ) -> tuple[list[SubtitleCandidate], bool, bool]:
        query = {"bvid": part.bvid, "cid": part.cid}
        if part.aid is not None:
            query["aid"] = part.aid
        url = _api_url("/x/player/wbi/v2", query)
        payload, response = self.client.get_json(
            url,
            cache_key=f"player-{part.bvid}-{part.cid}",
            headers={"Referer": f"https://www.bilibili.com/video/{part.bvid}?p={part.page}"},
            force=force,
        )
        data = _require_success(payload, operation=f"subtitle candidates for {part.part_id}")
        subtitle_data = data.get("subtitle")
        items = subtitle_data.get("subtitles", []) if isinstance(subtitle_data, dict) else []
        if not isinstance(items, list):
            items = []
        candidates: list[SubtitleCandidate] = []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict) or not item.get("subtitle_url"):
                continue
            source = "bilibili_ai" if _is_ai_subtitle(item) else "uploader"
            url_value = str(item["subtitle_url"])
            if url_value.startswith("//"):
                url_value = f"https:{url_value}"
            else:
                url_value = urljoin("https://www.bilibili.com", url_value)
            url_value = _require_subtitle_url(url_value)
            language = str(item.get("lan") or "und")
            upstream_id = str(item.get("id_str") or item.get("id") or index)
            candidate_id = f"s{hash_object({'part': part.part_id, 'id': upstream_id})[:16]}"
            candidates.append(
                SubtitleCandidate(
                    candidate_id=f"{part.part_id}-{candidate_id}",
                    source=source,
                    language=language,
                    language_name=str(item.get("lan_doc")) if item.get("lan_doc") else None,
                    part_id=part.part_id,
                    part_number=part.page,
                    cid=part.cid,
                    subtitle_url=url_value,
                    published_at=part.published_at,
                    metadata={
                        key: value for key, value in item.items() if key not in {"subtitle_url"}
                    },
                )
            )
        need_login = bool(data.get("need_login_subtitle"))
        return candidates, need_login, response.cache_hit

    def download_candidate(
        self,
        candidate: SubtitleCandidate,
        *,
        force: bool = False,
    ) -> tuple[bytes, bool]:
        if candidate.subtitle_url is None:
            raise SubtitleError(f"Subtitle candidate has no URL: {candidate.candidate_id}")
        result = self.client.get_bytes(
            candidate.subtitle_url,
            cache_key=f"subtitle-{candidate.candidate_id}",
            headers={"Referer": "https://www.bilibili.com/"},
            force=force,
        )
        return result.body, result.cache_hit


def select_candidates(
    candidates: list[SubtitleCandidate],
    *,
    preferred_languages: list[str],
) -> SubtitleSelection:
    if not candidates:
        return SubtitleSelection(
            part_id="unknown",
            selected_candidate_id=None,
            selected_source=None,
            reason="no_subtitles_available",
            candidates=[],
        )
    part_id = candidates[0].part_id
    source_rank = {"uploader": 0, "bilibili_ai": 1, "local_file": 2}
    language_rank = {language.lower(): index for index, language in enumerate(preferred_languages)}

    def rank(candidate: SubtitleCandidate) -> tuple[int, int, str]:
        return (
            source_rank[candidate.source],
            language_rank.get(candidate.language.lower(), len(language_rank)),
            candidate.candidate_id,
        )

    selected = min(candidates, key=rank)
    return SubtitleSelection(
        part_id=part_id,
        selected_candidate_id=selected.candidate_id,
        selected_source=selected.source,
        reason=(
            "uploader_subtitle_preferred"
            if selected.source == "uploader"
            else "bilibili_ai_used_no_uploader_subtitle"
            if selected.source == "bilibili_ai"
            else "local_subtitle_used_no_remote_subtitle"
        ),
        candidates=candidates,
    )


def public_candidate_metadata(candidate: SubtitleCandidate) -> dict[str, Any]:
    payload = candidate.model_dump(mode="json")
    if payload.get("subtitle_url"):
        payload["subtitle_url"] = sanitize_url(str(payload["subtitle_url"]))
    return redact(payload)
