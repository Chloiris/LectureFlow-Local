from __future__ import annotations

import json
import sys

import pytest

from lectureflow.process import run_command
from lectureflow.security import redact, redact_text, sanitize_url


def test_redaction_covers_nested_secrets() -> None:
    value = {
        "Cookie": "SESSDATA=secret",
        "nested": {"access_token": "abc", "safe": "visible"},
    }
    assert redact(value) == {
        "Cookie": "<redacted>",
        "nested": {"access_token": "<redacted>", "safe": "visible"},
    }
    assert "secret" not in redact_text("Authorization: secret")
    assert "bearer-secret" not in redact_text("Authorization: Bearer bearer-secret")


@pytest.mark.parametrize(
    "value",
    [
        '{"Cookie": "plain-secret"}',
        '{"bili_jct": "csrfsecret"}',
        '{"DedeUserID": "123456"}',
        "{'mid': '98765'}",
    ],
)
def test_redact_text_hides_quoted_json_or_repr_secrets(value: str) -> None:
    cleaned = redact_text(value)
    assert all(
        secret not in cleaned for secret in ("plain-secret", "csrfsecret", "123456", "98765")
    )
    assert "<redacted>" in cleaned


def test_sanitize_url_removes_identity_and_sensitive_query() -> None:
    cleaned = sanitize_url(
        "https://name:password@example.com/video?p=2&token=secret&safe=yes",
        keep_keys={"p"},
    )
    assert cleaned == "https://example.com/video?p=2"
    assert "password" not in cleaned
    assert "secret" not in cleaned


def test_redact_text_hides_signed_url_values() -> None:
    value = "download failed: https://cdn.example/video?upsig=secret-signature&deadline=99"
    cleaned = redact_text(value)
    assert "secret-signature" not in cleaned
    assert "deadline=99" in cleaned


@pytest.mark.parametrize(
    "key",
    ["DedeUserID", "mid", "buvid3", "csrf", "w_rid", "bili_ticket"],
)
def test_redact_text_hides_bilibili_identity_fields(key: str) -> None:
    cleaned = redact_text(f"request failed: https://example.test/v?{key}=private-value&p=2")
    assert "private-value" not in cleaned
    assert "p=2" in cleaned


def test_subprocess_treats_metacharacters_as_plain_argument() -> None:
    hostile = "中文 空格;$(touch should-not-exist)`uname`"
    script = "import json,sys; print(json.dumps(sys.argv[1:], ensure_ascii=False))"
    result = run_command((sys.executable, "-c", script, hostile))
    assert json.loads(result.stdout) == [hostile]
