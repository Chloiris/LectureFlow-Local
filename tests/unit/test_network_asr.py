from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

import lectureflow.asr.base as asr_module
from lectureflow.asr.base import detect_asr_capabilities, require_asr_backend
from lectureflow.config import load_config
from lectureflow.errors import ASRUnavailableError, NetworkError
from lectureflow.network import HttpClient


class Response:
    status = 200

    def __init__(self, body: bytes, url: str) -> None:
        self.body = body
        self.url = url

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_http_client_retries_then_caches_raw_response(tmp_path: Path) -> None:
    calls: list[Any] = []

    def opener(request: object, *, timeout: float) -> Response:
        calls.append((request, timeout))
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                "https://example.invalid/data",
                503,
                "unavailable",
                hdrs=None,
                fp=None,
            )
        return Response(b'{"ok": true}', "https://example.invalid/data")

    client = HttpClient(
        cache_dir=tmp_path / "响应 缓存",
        retries=1,
        opener=opener,
        sleeper=lambda _: None,
    )
    payload, first = client.get_json(
        "https://example.invalid/data?token=top-secret",
        cache_key="fixture",
    )
    payload_again, second = client.get_json(
        "https://example.invalid/data?token=top-secret",
        cache_key="fixture",
    )
    assert payload == payload_again == {"ok": True}
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert len(calls) == 2
    metadata = (tmp_path / "响应 缓存/fixture.meta.json").read_text()
    assert "top-secret" not in metadata
    assert (tmp_path / "响应 缓存/fixture.body").read_bytes() == b'{"ok": true}'


def test_http_errors_are_classified_and_urls_are_redacted(tmp_path: Path) -> None:
    def opener(request: object, *, timeout: float) -> Response:
        raise urllib.error.HTTPError(
            "https://example.invalid/data",
            429,
            "limited",
            hdrs=None,
            fp=None,
        )

    client = HttpClient(cache_dir=tmp_path, retries=0, opener=opener)
    with pytest.raises(NetworkError) as captured:
        client.get_bytes("https://example.invalid/data?token=must-not-leak&w_rid=signature")
    message = str(captured.value)
    assert "rate_limited" in message
    assert "must-not-leak" not in message
    assert "signature" not in message

    with pytest.raises(NetworkError, match="Sensitive HTTP header"):
        client.get_bytes("https://example.invalid", headers={"Cookie": "SESSDATA=leak"})


def test_asr_capability_check_never_downloads_a_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config(cwd=tmp_path)
    monkeypatch.setattr(asr_module.importlib.util, "find_spec", lambda module: None)
    capabilities = detect_asr_capabilities(config.asr)
    assert [item.backend for item in capabilities] == [
        "mlx_whisper",
        "faster_whisper",
        "vibeasr_bitnet",
    ]
    assert not any(item.available for item in capabilities)
    assert all("No model was downloaded" in item.reason for item in capabilities)
    with pytest.raises(ASRUnavailableError, match="No model download was attempted"):
        require_asr_backend(config.asr)

    monkeypatch.setattr(asr_module.importlib.util, "find_spec", lambda module: object())
    installed_without_model = detect_asr_capabilities(config.asr)
    assert not any(item.available for item in installed_without_model)
    assert all(
        "automatic model download is disabled" in item.reason.lower()
        for item in installed_without_model
    )


def test_network_cache_metadata_is_valid_json(tmp_path: Path) -> None:
    client = HttpClient(
        cache_dir=tmp_path,
        retries=0,
        opener=lambda request, timeout: Response(b"{}", "https://example.invalid"),
    )
    client.get_bytes("https://example.invalid", cache_key="one")
    assert json.loads((tmp_path / "one.meta.json").read_text())["status"] == 200


def test_offline_http_client_never_uses_network_and_requires_cache(tmp_path: Path) -> None:
    calls = 0

    def opener(request: object, *, timeout: float) -> Response:
        nonlocal calls
        calls += 1
        return Response(b'{"cached": true}', "https://example.invalid/data")

    online = HttpClient(cache_dir=tmp_path, retries=0, opener=opener)
    online.get_bytes("https://example.invalid/data", cache_key="cached")
    offline = HttpClient(cache_dir=tmp_path, retries=0, offline=True, opener=opener)
    hit = offline.get_bytes("https://example.invalid/data", cache_key="cached")
    assert hit.cache_hit is True
    assert calls == 1
    with pytest.raises(NetworkError, match="offline_cache_miss"):
        offline.get_bytes("https://example.invalid/missing", cache_key="missing")
    assert calls == 1
