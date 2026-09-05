from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import lectureflow.media.service as media_service
from lectureflow.config import load_config
from lectureflow.errors import MediaSizeLimitError
from lectureflow.media.service import MEDIA_LIMIT_BYTES, _estimate_download_bytes, acquire_media
from lectureflow.paths import JobFiles, ensure_job_layout
from lectureflow.process import CommandResult
from lectureflow.schemas.frames import MediaManifest
from lectureflow.schemas.manifest import JobManifest, SourceDescriptor, TimeRange
from lectureflow.state import new_state


def test_download_estimate_respects_requested_range() -> None:
    metadata = {
        "duration": 1000,
        "formats": [
            {"height": 1080, "tbr": 1000, "vcodec": "avc1", "acodec": "none"},
            {"tbr": 128, "vcodec": "none", "acodec": "mp4a"},
        ],
    }
    whole = _estimate_download_bytes(metadata, start=0, end=None)
    clip = _estimate_download_bytes(metadata, start=0, end=100)
    assert whole is not None and clip is not None
    assert whole > clip
    assert clip == int(100 * (1000 + 128) * 1000 / 8 * 1.10)


def test_size_limit_is_binary_500_mib() -> None:
    assert MEDIA_LIMIT_BYTES == 500 * 1024 * 1024
    assert load_config().frames.max_final_frames == 30
    assert load_config().frames.contact_sheet_max_frames == 12


def test_media_manifest_records_multipart_policy() -> None:
    value = MediaManifest(
        source_kind="bilibili",
        source_id="BV1fixture",
        part_id="BV1fixture_p1",
        selected_part=1,
        part_selection_reason="default_first_part_for_media_stage",
        source_url="https://www.bilibili.com/video/BV1fixture?p=1",
        requested_start=0,
        requested_end=300,
        media_path="media/video/source.mp4",
        storage="job_local",
        container="mp4",
        duration=300,
        source_duration=1000,
        width=1920,
        height=1080,
        file_size=1,
        sha256="a" * 64,
        video_codec="h264",
        audio_included=True,
        tool_versions={},
        input_hash="b" * 64,
        config_hash="c" * 64,
        generated_at="2026-08-13T00:00:00Z",
    )
    assert value.selected_part == 1
    assert value.part_selection_reason == "default_first_part_for_media_stage"


def test_bilibili_acquire_refuses_estimate_over_limit_before_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(cwd=tmp_path)
    root = ensure_job_layout(config.workspace_root, "bili-size-fixture")
    files = JobFiles(root)
    files.metadata.write_text(
        '{"duration":7200,"formats":[{"height":1080,"tbr":3000,"vcodec":"avc1","acodec":"none"}]}',
        encoding="utf-8",
    )
    source = SourceDescriptor(
        kind="bilibili",
        normalized="https://www.bilibili.com/video/BV1fixture",
        display_name="BV1fixture",
        bvid="BV1fixture",
    )
    manifest = JobManifest(
        job_id="bili-size-fixture",
        created_at="2026-08-13T00:00:00Z",
        updated_at="2026-08-13T00:00:00Z",
        source=source,
        requested_range=TimeRange(),
        config_hash="a" * 64,
        metadata_path="raw/metadata/metadata.json",
    )
    state = new_state(manifest.job_id)
    monkeypatch.setattr(
        media_service,
        "load_job_context",
        lambda *args, **kwargs: (files, manifest, state),
    )
    with pytest.raises(MediaSizeLimitError, match="500 MiB"):
        acquire_media(manifest.job_id, config=config)
    assert not (root / "media/video/source.mp4").exists()


def test_bilibili_download_uses_project_python_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(cwd=tmp_path)
    root = ensure_job_layout(config.workspace_root, "bili-module-fixture")
    files = JobFiles(root)
    files.metadata.write_text(
        '{"duration":60,"webpage_url":"https://www.bilibili.com/video/BV1fixture?p=1",'
        '"id":"BV1fixture_p1","formats":[{"height":720,"tbr":200,'
        '"vcodec":"avc1","acodec":"none"}]}',
        encoding="utf-8",
    )
    source = SourceDescriptor(
        kind="bilibili",
        normalized="https://www.bilibili.com/video/BV1fixture",
        display_name="BV1fixture",
        bvid="BV1fixture",
    )
    manifest = JobManifest(
        job_id="bili-module-fixture",
        created_at="2026-08-13T00:00:00Z",
        updated_at="2026-08-13T00:00:00Z",
        source=source,
        requested_range=TimeRange(start=0, end=30),
        config_hash="a" * 64,
        metadata_path="raw/metadata/metadata.json",
    )
    state = new_state(manifest.job_id)
    monkeypatch.setattr(
        media_service,
        "load_job_context",
        lambda *args, **kwargs: (files, manifest, state),
    )
    monkeypatch.setattr(media_service.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(media_service, "_yt_dlp_version", lambda: "fixture-version")
    monkeypatch.setattr(media_service, "available_tool_version", lambda name: "fixture-ffprobe")
    monkeypatch.setattr(media_service.shutil, "which", lambda name: f"/fixture/{name}")
    calls: list[tuple[str, ...]] = []

    def fake_run(argv, **kwargs):
        normalized = tuple(str(item) for item in argv)
        calls.append(normalized)
        if normalized[:3] == (sys.executable, "-m", "yt_dlp"):
            template = normalized[normalized.index("--output") + 1]
            Path(template.replace("%(ext)s", "mp4")).write_bytes(b"fixture-media")
            return CommandResult(normalized, 0, "", "")
        probe = {
            "format": {"duration": "30.0", "format_name": "mp4"},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1280,
                    "height": 720,
                }
            ],
        }
        return CommandResult(normalized, 0, json.dumps(probe), "")

    monkeypatch.setattr(media_service, "run_command", fake_run)
    result = acquire_media(manifest.job_id, config=config)
    assert result.cache_hit is False
    assert calls[0][:3] == (sys.executable, "-m", "yt_dlp")
    assert not any("cookie" in value.casefold() for value in calls[0])

    stored = json.loads(files.media_manifest.read_text())
    stored["input_hash"] = "legacy-metadata-sha-input"
    files.media_manifest.write_text(json.dumps(stored), encoding="utf-8")
    files.metadata.write_text(
        '{"duration":60,"webpage_url":"https://www.bilibili.com/video/BV1fixture?p=1",'
        '"id":"BV1fixture_p1","volatile_signed_url":"changed",'
        '"formats":[{"height":720,"tbr":200,"vcodec":"avc1","acodec":"none"}]}',
        encoding="utf-8",
    )
    calls.clear()
    cached = acquire_media(manifest.job_id, config=config)
    assert cached.cache_hit is True
    assert calls == []
