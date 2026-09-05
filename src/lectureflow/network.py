from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lectureflow.atomic import atomic_write_bytes, atomic_write_json
from lectureflow.errors import NetworkError
from lectureflow.hashing import hash_object
from lectureflow.security import is_sensitive_key, redact_text, sanitize_url

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 LectureFlow-Local/0.2"
)
RETRYABLE_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}


@dataclass(frozen=True, slots=True)
class HttpResult:
    body: bytes
    url: str
    cache_hit: bool
    status: int


class HttpClient:
    def __init__(
        self,
        *,
        cache_dir: Path,
        timeout: float = 20.0,
        retries: int = 2,
        user_agent: str = DEFAULT_USER_AGENT,
        offline: bool = False,
        opener: Callable[..., Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if retries < 0 or retries > 5:
            raise ValueError("HTTP retries must be between 0 and 5")
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.retries = retries
        self.user_agent = user_agent
        self.offline = offline
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleeper

    def _cache_paths(self, url: str, cache_key: str | None) -> tuple[Path, Path]:
        key = cache_key or hash_object({"url": url})
        safe_key = "".join(char for char in key if char.isalnum() or char in "-_")[:120]
        return self.cache_dir / f"{safe_key}.body", self.cache_dir / f"{safe_key}.meta.json"

    def get_bytes(
        self,
        url: str,
        *,
        cache_key: str | None = None,
        headers: dict[str, str] | None = None,
        force: bool = False,
    ) -> HttpResult:
        request_headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json,text/plain,*/*",
        }
        for key, value in (headers or {}).items():
            if is_sensitive_key(key):
                raise NetworkError(f"Sensitive HTTP header is forbidden: {key}")
            request_headers[key] = value
        body_path, metadata_path = self._cache_paths(url, cache_key)
        if not force and body_path.is_file() and metadata_path.is_file():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                return HttpResult(
                    body=body_path.read_bytes(),
                    url=str(metadata["url"]),
                    cache_hit=True,
                    status=int(metadata["status"]),
                )
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                pass
        if self.offline:
            raise NetworkError(
                f"offline_cache_miss: no valid cached response for {sanitize_url(url)}"
            )
        request = urllib.request.Request(url, headers=request_headers, method="GET")
        last_error: BaseException | None = None
        for attempt in range(self.retries + 1):
            try:
                with self._opener(request, timeout=self.timeout) as response:
                    body = response.read()
                    status = int(getattr(response, "status", 200))
                    final_url = str(getattr(response, "url", url))
                atomic_write_bytes(body_path, body)
                atomic_write_json(
                    metadata_path,
                    {
                        "schema_version": "1.0.0",
                        "url": sanitize_url(final_url),
                        "status": status,
                        "request_url": sanitize_url(url),
                    },
                )
                return HttpResult(body=body, url=final_url, cache_hit=False, status=status)
            except urllib.error.HTTPError as exc:
                last_error = exc
                category = "rate_limited" if exc.code == 429 else "http_error"
                retryable = exc.code in RETRYABLE_HTTP_STATUS
                if not retryable or attempt >= self.retries:
                    raise NetworkError(
                        f"{category}: HTTP {exc.code} for {sanitize_url(url)}"
                    ) from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    reason = getattr(exc, "reason", exc)
                    raise NetworkError(
                        f"network_error after {attempt + 1} attempt(s) for "
                        f"{sanitize_url(url)}: {redact_text(str(reason))}"
                    ) from exc
            if attempt < self.retries:
                self._sleep(min(0.25 * (2**attempt), 1.0))
        raise NetworkError(f"network_error for {sanitize_url(url)}: {last_error}")

    def get_json(
        self,
        url: str,
        *,
        cache_key: str | None = None,
        headers: dict[str, str] | None = None,
        force: bool = False,
    ) -> tuple[dict[str, Any], HttpResult]:
        result = self.get_bytes(
            url,
            cache_key=cache_key,
            headers=headers,
            force=force,
        )
        try:
            payload = json.loads(result.body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NetworkError(f"invalid_json_response from {sanitize_url(url)}: {exc}") from exc
        if not isinstance(payload, dict):
            raise NetworkError(f"invalid_json_response from {sanitize_url(url)}: expected object")
        return payload, result
