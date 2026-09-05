from __future__ import annotations

from pathlib import Path

import pytest

from lectureflow.errors import SourceError
from lectureflow.sources.bilibili import parse_bilibili_url
from lectureflow.sources.detect import identify_source


@pytest.mark.parametrize(
    ("value", "bvid", "aid", "page", "normalized"),
    [
        (
            "https://www.bilibili.com/video/BV1xx411c7mD/?p=3&spm_id_from=secret",
            "BV1xx411c7mD",
            None,
            3,
            "https://www.bilibili.com/video/BV1xx411c7mD?p=3",
        ),
        ("BV1xx411c7mD", "BV1xx411c7mD", None, None, "https://www.bilibili.com/video/BV1xx411c7mD"),
        ("av170001", None, 170001, None, "https://www.bilibili.com/video/av170001"),
    ],
)
def test_parse_bilibili_identity(
    value: str, bvid: str | None, aid: int | None, page: int | None, normalized: str
) -> None:
    result = parse_bilibili_url(value)
    assert result.bvid == bvid
    assert result.aid == aid
    assert result.page == page
    assert result.normalized == normalized


def test_parse_short_link_without_network() -> None:
    result = parse_bilibili_url("https://b23.tv/AbCd123?token=do-not-keep")
    assert result.short_url is True
    assert result.normalized == "https://b23.tv/AbCd123"


def test_parse_short_link_preserves_explicit_page() -> None:
    result = parse_bilibili_url("https://b23.tv/AbCd123?p=3&token=do-not-keep")
    assert result.page == 3
    assert result.normalized == "https://b23.tv/AbCd123?p=3"


def test_bilibili_url_without_page_means_all_parts() -> None:
    result = parse_bilibili_url("https://www.bilibili.com/video/BV1xx411c7mD")
    assert result.page is None


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/video/BV1xx411c7mD",
        "https://www.bilibili.com/not-video/BV1xx411c7mD",
        "https://www.bilibili.com/video/BV1xx411c7mD?p=zero",
        "ftp://example.com/video.mp4",
    ],
)
def test_reject_unsupported_or_invalid_url(value: str) -> None:
    with pytest.raises(SourceError):
        identify_source(value)


def test_identify_local_path_with_chinese_and_spaces(tmp_path: Path) -> None:
    source = tmp_path / "中文 课程.mov"
    source.write_bytes(b"fixture")
    result = identify_source(str(source))
    assert result.kind == "local"
    assert result.normalized == str(source.resolve())
    assert result.display_name == "中文 课程.mov"


def test_local_filename_starting_with_bv_is_not_misclassified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "BV lecture.mp4"
    source.write_bytes(b"fixture")
    monkeypatch.chdir(tmp_path)
    result = identify_source(source.name)
    assert result.kind == "local"


def test_reject_directory_as_local_source(tmp_path: Path) -> None:
    with pytest.raises(SourceError, match="regular file"):
        identify_source(str(tmp_path))
