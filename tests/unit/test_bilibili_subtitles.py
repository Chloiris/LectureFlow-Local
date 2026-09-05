from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import lectureflow.pipeline as pipeline_module
from lectureflow.config import AppConfig, load_config
from lectureflow.errors import NetworkError, SubtitleError
from lectureflow.network import HttpResult
from lectureflow.pipeline import job_status
from lectureflow.schemas.transcript import SubtitleCandidate
from lectureflow.sources.bilibili_subtitles import (
    public_candidate_metadata,
    select_candidates,
)
from lectureflow.subtitles.service import fetch_bilibili_subtitles

FIXTURES = Path(__file__).parents[1] / "fixtures/bilibili"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    base = load_config(cwd=tmp_path)
    return base.model_copy(update={"workspace_root": tmp_path / "工作 空间"})


@pytest.fixture(autouse=True)
def fake_bilibili_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pipeline_module,
        "probe_bilibili",
        lambda source: ({"bvid": source.bvid or "BV1xx411c7mD"}, "yt-dlp fixture"),
    )


class FixtureClient:
    def __init__(self, players: dict[int, dict[str, Any]]) -> None:
        self.players = players
        self.json_calls: list[str] = []
        self.byte_calls: list[str] = []

    def get_json(
        self,
        url: str,
        *,
        cache_key: str | None = None,
        headers: dict[str, str] | None = None,
        force: bool = False,
    ) -> tuple[dict[str, Any], HttpResult]:
        self.json_calls.append(url)
        if "/x/web-interface/view" in url:
            payload = _fixture("view_multi.json")
        elif "cid=1001" in url:
            payload = self.players[1001]
        elif "cid=1002" in url:
            payload = self.players[1002]
        else:
            raise AssertionError(url)
        return payload, HttpResult(body=b"fixture", url=url, cache_hit=False, status=200)

    def get_bytes(
        self,
        url: str,
        *,
        cache_key: str | None = None,
        headers: dict[str, str] | None = None,
        force: bool = False,
    ) -> HttpResult:
        self.byte_calls.append(url)
        name = "subtitle_uploader.json" if "uploader" in url else "subtitle_ai.json"
        return HttpResult(
            body=(FIXTURES / name).read_bytes(),
            url=url,
            cache_hit=False,
            status=200,
        )


def _candidate(source: str, language: str, candidate_id: str) -> SubtitleCandidate:
    return SubtitleCandidate(
        candidate_id=candidate_id,
        source=source,
        language=language,
        part_id="p1",
    )


def test_priority_is_uploader_then_ai_then_local() -> None:
    candidates = [
        _candidate("local_file", "zh-CN", "local"),
        _candidate("bilibili_ai", "zh-CN", "ai"),
        _candidate("uploader", "en", "human"),
    ]
    selection = select_candidates(candidates, preferred_languages=["zh-CN", "en"])
    assert selection.selected_candidate_id == "human"
    assert selection.selected_source == "uploader"
    without_human = select_candidates(candidates[:2], preferred_languages=["zh-CN"])
    assert without_human.selected_source == "bilibili_ai"
    local_only = select_candidates(candidates[:1], preferred_languages=["zh-CN"])
    assert local_only.selected_source == "local_file"


def test_mixed_subtitles_choose_uploader_and_multi_parts_stay_separate(
    config: AppConfig,
) -> None:
    mixed = _fixture("player_mixed.json")
    client = FixtureClient({1001: mixed, 1002: mixed})
    result = fetch_bilibili_subtitles(
        "BV1xx411c7mD",
        config=config,
        client=client,  # type: ignore[arg-type]
    )
    assert result.status == "transcript_ready"
    assert set(result.selected_sources.values()) == {"uploader"}
    assert result.part_ids == ["BV1xx411c7mD-p1", "BV1xx411c7mD-p2"]
    first_dir = result.transcript_root / "parts/BV1xx411c7mD-p1"
    second_dir = result.transcript_root / "parts/BV1xx411c7mD-p2"
    first = json.loads((first_dir / "original.json").read_text())
    second = json.loads((second_dir / "original.json").read_text())
    assert first["part_id"] != second["part_id"]
    first_ids = {item["segment_id"] for item in first["segments"]}
    second_ids = {item["segment_id"] for item in second["segments"]}
    assert first_ids.isdisjoint(second_ids)
    selection_text = (first_dir / "subtitle-selection-report.json").read_text()
    assert "secret" not in selection_text
    assert "redacted" in selection_text
    source_report = json.loads((first_dir / "subtitle-source.json").read_text())
    archived_raw = result.transcript_root.parent / source_report["raw_file"]
    assert archived_raw.read_bytes() == (FIXTURES / "subtitle_uploader.json").read_bytes()

    calls = (len(client.json_calls), len(client.byte_calls))
    cached = fetch_bilibili_subtitles(
        "BV1xx411c7mD",
        config=config,
        client=client,  # type: ignore[arg-type]
    )
    assert cached.cache_hit is True
    assert (len(client.json_calls), len(client.byte_calls)) == calls

    archived_raw.unlink()
    repaired = fetch_bilibili_subtitles(
        "BV1xx411c7mD",
        config=config,
        client=client,  # type: ignore[arg-type]
    )
    assert repaired.cache_hit is False
    assert archived_raw.is_file()
    assert len(client.byte_calls) == calls[1] + 2


def test_ai_is_used_when_no_uploader_exists(config: AppConfig) -> None:
    ai = _fixture("player_ai.json")
    client = FixtureClient({1001: ai, 1002: ai})
    result = fetch_bilibili_subtitles(
        "https://www.bilibili.com/video/BV1xx411c7mD?p=2",
        config=config,
        client=client,  # type: ignore[arg-type]
    )
    assert result.selected_sources == {"BV1xx411c7mD-p2": "bilibili_ai"}
    source = json.loads((result.transcript_root / "subtitle-source.json").read_text())
    assert source["source"] == "bilibili_ai"
    assert source["part_number"] == 2


@pytest.mark.parametrize(
    ("source", "page"),
    [
        ("https://www.bilibili.com/video/av170001?p=1", 1),
        ("https://b23.tv/fixture?p=2", 2),
    ],
)
def test_av_and_b23_sources_resolve_to_requested_part(
    tmp_path: Path,
    config: AppConfig,
    source: str,
    page: int,
) -> None:
    isolated = config.model_copy(
        update={"workspace_root": tmp_path / f"source-{page}-{len(source)}"}
    )
    mixed = _fixture("player_mixed.json")
    client = FixtureClient({1001: mixed, 1002: mixed})
    result = fetch_bilibili_subtitles(
        source,
        config=isolated,
        client=client,  # type: ignore[arg-type]
    )
    assert result.part_ids == [f"BV1xx411c7mD-p{page}"]


def test_requested_range_filters_cues_without_shifting_timestamps(config: AppConfig) -> None:
    mixed = _fixture("player_mixed.json")
    client = FixtureClient({1001: mixed, 1002: mixed})
    result = fetch_bilibili_subtitles(
        "https://www.bilibili.com/video/BV1xx411c7mD?p=1",
        config=config,
        start=3,
        end=5,
        client=client,  # type: ignore[arg-type]
    )
    document = json.loads((result.transcript_root / "original.json").read_text())
    assert len(document["segments"]) == 1
    assert document["segments"][0]["start"] == 3.5
    assert document["segments"][0]["end"] == 6.0
    assert document["source_ref"]["requested_range"] == {"start": 3.0, "end": 5.0}
    quality = json.loads((result.transcript_root / "subtitle-quality-report.json").read_text())
    assert quality["coverage_ratio"] == 0.75


def test_no_subtitles_is_structured_recoverable_result(config: AppConfig) -> None:
    none = _fixture("player_none.json")
    client = FixtureClient({1001: none, 1002: none})
    result = fetch_bilibili_subtitles(
        "BV1xx411c7mD",
        config=config,
        client=client,  # type: ignore[arg-type]
    )
    assert result.status == "no_public_subtitles"
    assert result.recoverable is True
    assert set(result.selected_sources.values()) == {None}
    assert set(result.classifications.values()) == {"D"}
    report = job_status(result.job_id, config=config)
    assert report["stages"]["subtitle_ready"]["status"] == "succeeded"
    assert report["subtitle_result"]["status"] == "no_public_subtitles"
    calls = (len(client.json_calls), len(client.byte_calls))
    cached = fetch_bilibili_subtitles(
        "BV1xx411c7mD",
        config=config,
        offline=True,
        client=client,  # type: ignore[arg-type]
    )
    assert cached.cache_hit is True
    assert (len(client.json_calls), len(client.byte_calls)) == calls


def test_no_subtitle_result_distinguishes_login_required_from_absent(
    config: AppConfig,
) -> None:
    login = _fixture("player_none.json")
    login["data"]["need_login_subtitle"] = True
    client = FixtureClient({1001: login, 1002: _fixture("player_none.json")})
    result = fetch_bilibili_subtitles(
        "BV1xx411c7mD",
        config=config,
        client=client,  # type: ignore[arg-type]
    )
    assert result.classifications == {
        "BV1xx411c7mD-p1": "E",
        "BV1xx411c7mD-p2": "D",
    }
    payload = json.loads((result.transcript_root / "subtitle-result.json").read_text())
    assert payload["classification_counts"] == {"D": 1, "E": 1}


def test_untrusted_subtitle_download_host_is_rejected(config: AppConfig) -> None:
    mixed = _fixture("player_mixed.json")
    mixed["data"]["subtitle"]["subtitles"][0]["subtitle_url"] = (
        "https://127.0.0.1/private?token=secret"
    )
    client = FixtureClient({1001: mixed, 1002: mixed})
    with pytest.raises(SubtitleError, match="untrusted subtitle download URL"):
        fetch_bilibili_subtitles(
            "https://www.bilibili.com/video/BV1xx411c7mD?p=1",
            config=config,
            client=client,  # type: ignore[arg-type]
        )
    assert client.byte_calls == []


def test_local_subtitle_is_fallback_but_never_displaces_remote_human(
    tmp_path: Path,
    config: AppConfig,
) -> None:
    local = tmp_path / "本地 后备.srt"
    local.write_text("1\n00:00:00,000 --> 00:00:01,000\n本地后备\n", encoding="utf-8")
    none_client = FixtureClient({1001: _fixture("player_none.json"), 1002: {}})
    fallback = fetch_bilibili_subtitles(
        "https://www.bilibili.com/video/BV1xx411c7mD?p=1",
        config=config,
        local_subtitle=local,
        local_language="zh-CN",
        client=none_client,  # type: ignore[arg-type]
    )
    assert fallback.selected_sources == {"BV1xx411c7mD-p1": "local_file"}
    assert none_client.byte_calls == []

    other_config = config.model_copy(update={"workspace_root": tmp_path / "second"})
    human_client = FixtureClient({1001: _fixture("player_mixed.json"), 1002: {}})
    remote = fetch_bilibili_subtitles(
        "https://www.bilibili.com/video/BV1xx411c7mD?p=1",
        config=other_config,
        local_subtitle=local,
        client=human_client,  # type: ignore[arg-type]
    )
    assert remote.selected_sources == {"BV1xx411c7mD-p1": "uploader"}
    assert len(human_client.byte_calls) == 1


def test_candidate_public_metadata_redacts_signed_url() -> None:
    candidate = SubtitleCandidate(
        candidate_id="one",
        source="uploader",
        language="zh-CN",
        part_id="p1",
        subtitle_url="https://example.invalid/sub.json?token=leak&w_rid=signed&lang=zh",
    )
    text = json.dumps(public_candidate_metadata(candidate))
    assert "leak" not in text
    assert "signed" not in text
    assert "redacted" in text
    assert "lang=zh" in text


def test_network_failure_does_not_leak_credentials_to_event_log(config: AppConfig) -> None:
    class LeakyFailureClient(FixtureClient):
        def get_json(
            self,
            url: str,
            *,
            cache_key: str | None = None,
            headers: dict[str, str] | None = None,
            force: bool = False,
        ) -> tuple[dict[str, Any], HttpResult]:
            raise NetworkError(
                "Cookie: SESSDATA=cookie-secret\n"
                "https://example.invalid/api?token=token-secret&w_rid=signature-secret"
            )

    client = LeakyFailureClient({})
    with pytest.raises(SubtitleError) as captured:
        fetch_bilibili_subtitles(
            "BV1xx411c7mD",
            config=config,
            client=client,  # type: ignore[arg-type]
        )
    job_dir = next(item for item in config.workspace_root.iterdir() if item.is_dir())
    persisted = (job_dir / "logs/events.jsonl").read_text() + (
        job_dir / "pipeline-state.json"
    ).read_text()
    all_text = persisted + str(captured.value)
    assert "cookie-secret" not in all_text
    assert "token-secret" not in all_text
    assert "signature-secret" not in all_text
    assert "redacted" in all_text
