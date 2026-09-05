from __future__ import annotations

from pathlib import Path

import pytest

import lectureflow.sources.metadata as metadata_module
from lectureflow.process import CommandResult
from lectureflow.schemas.manifest import SourceDescriptor
from lectureflow.sources.metadata import probe_bilibili, tool_version


def test_absolute_yt_dlp_path_uses_double_dash_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, ...]] = []

    def fake_version(*argv: str) -> str:
        observed.append(argv)
        return "test-version"

    monkeypatch.setattr(metadata_module, "first_version_line", fake_version)
    executable = str(Path("/opt/tools/yt-dlp"))
    assert tool_version(executable) == "test-version"
    assert observed == [(executable, "--version")]


def test_bilibili_all_parts_does_not_disable_playlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, ...]] = []
    monkeypatch.setattr(metadata_module.shutil, "which", lambda name: "/fake/yt-dlp")
    monkeypatch.setattr(metadata_module, "tool_version", lambda executable: "test")

    def fake_run(argv: tuple[str, ...], **kwargs: object) -> CommandResult:
        observed.append(argv)
        return CommandResult(argv, 0, '{"_type":"playlist","entries":[]}', "")

    monkeypatch.setattr(metadata_module, "run_command", fake_run)
    source = SourceDescriptor(
        kind="bilibili",
        normalized="https://www.bilibili.com/video/BV1xx411c7mD",
        display_name="BV1xx411c7mD",
        bvid="BV1xx411c7mD",
        page=None,
    )
    probe_bilibili(source)
    assert "--no-playlist" not in observed[0]
    assert "--yes-playlist" not in observed[0]
    assert "--playlist-items" not in observed[0]


def test_bilibili_explicit_part_uses_page_url_and_disables_playlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, ...]] = []
    monkeypatch.setattr(metadata_module.shutil, "which", lambda name: "/fake/yt-dlp")
    monkeypatch.setattr(metadata_module, "tool_version", lambda executable: "test")

    def fake_run(argv: tuple[str, ...], **kwargs: object) -> CommandResult:
        observed.append(argv)
        return CommandResult(argv, 0, '{"id":"part"}', "")

    monkeypatch.setattr(metadata_module, "run_command", fake_run)
    source = SourceDescriptor(
        kind="bilibili",
        normalized="https://www.bilibili.com/video/BV1xx411c7mD?p=3",
        display_name="BV1xx411c7mD",
        bvid="BV1xx411c7mD",
        page=3,
    )
    probe_bilibili(source)
    assert "--no-playlist" not in observed[0]
    assert "--playlist-items" not in observed[0]
    assert observed[0][-1].endswith("?p=3")
