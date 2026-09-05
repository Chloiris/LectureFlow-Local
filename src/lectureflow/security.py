from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "cookies",
    "password",
    "passwd",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "key",
    "signature",
    "sign",
    "sig",
    "upsig",
    "auth_key",
    "wssecret",
    "ws_secret",
    "sessdata",
    "bili_jct",
    "csrf",
    "csrf_token",
    "dedeuserid",
    "dedeuserid__ckmd5",
    "buvid3",
    "buvid4",
    "b_nut",
    "b_lsid",
    "sid",
    "mid",
    "uid",
    "user_id",
    "w_rid",
    "wts",
    "bili_ticket",
    "bili_ticket_expires",
    "ac_time_value",
}

_INLINE_SECRET = re.compile(
    r"(?i)([\"']?)(authorization|cookie|password|token|api[_-]?key|sessdata|bili_jct|csrf|"
    r"dedeuserid(?:__ckmd5)?|buvid[34]|b_nut|b_lsid|sid|mid|uid|user_id|w_rid|"
    r"wts|bili_ticket(?:_expires)?|ac_time_value)([\"']?)"
    r"(\s*[:=]\s*)([\"']?)([^\s,;&}\]\"']+)([\"']?)"
)
_AUTHORIZATION_LINE = re.compile(r"(?im)^(\s*authorization\s*:\s*)[^\r\n]+")
_COOKIE_LINE = re.compile(r"(?im)^(\s*(?:set-)?cookie\s*:\s*)[^\r\n]+")
_URL = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)


def is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in SENSITIVE_KEYS or normalized.endswith(("_token", "_secret", "_key"))


def redact_text(value: str) -> str:
    redacted = _AUTHORIZATION_LINE.sub(r"\1<redacted>", value)
    redacted = _COOKIE_LINE.sub(r"\1<redacted>", redacted)
    redacted = _URL.sub(lambda match: _sanitize_url(match.group(0), keep_keys=None), redacted)
    redacted = _INLINE_SECRET.sub(
        lambda match: (
            f"{match.group(1)}{match.group(2)}{match.group(3)}"
            f"{match.group(4)}{match.group(5)}<redacted>{match.group(7)}"
        ),
        redacted,
    )
    return redacted


def sanitize_url(value: str, *, keep_keys: set[str] | None = None) -> str:
    return _sanitize_url(value, keep_keys=keep_keys)


def _sanitize_url(value: str, *, keep_keys: set[str] | None) -> str:
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        return _INLINE_SECRET.sub(
            lambda match: (
                f"{match.group(1)}{match.group(2)}{match.group(3)}"
                f"{match.group(4)}{match.group(5)}<redacted>{match.group(7)}"
            ),
            value,
        )
    hostname = parts.hostname or ""
    netloc = hostname
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    allowed = keep_keys
    query: list[tuple[str, str]] = []
    for key, item in parse_qsl(parts.query, keep_blank_values=True):
        if is_sensitive_key(key):
            if allowed is None or key in allowed:
                query.append((key, "<redacted>"))
        elif allowed is None or key in allowed:
            query.append((key, item))
    return urlunsplit((parts.scheme, netloc, parts.path, urlencode(query), ""))


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            str(key): "<redacted>" if is_sensitive_key(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [redact(item) for item in value]
    return value
