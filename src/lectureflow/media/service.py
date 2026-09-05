from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from lectureflow.atomic import atomic_write_json
from lectureflow.config import AppConfig
from lectureflow.errors import CommandError, MediaError, MediaSizeLimitError, SourceError
from lectureflow.hashing import fingerprint_local_file, hash_file, hash_object
from lectureflow.paths import JobFiles
from lectureflow.pipeline import load_job_context
from lectureflow.process import run_command
from lectureflow.schemas.frames import MediaManifest
from lectureflow.security import redact_text
from lectureflow.sources.metadata import available_tool_version
from lectureflow.state import JobLock, record_transition, utc_now

MEDIA_LIMIT_BYTES = 500 * 1024 * 1024
FORMAT_SELECTOR = "bv*[height<=1080]+ba[abr<=160]/b[height<=1080]/best[height<=1080]"
MEDIA_STAGE_SCHEMA = "media-1.0.1"


def _yt_dlp_version() -> str | None:
    try:
        return version("yt-dlp")
    except PackageNotFoundError:
        return None


@dataclass(frozen=True, slots=True)
class MediaAcquireResult:
    job_id: str
    cache_hit: bool
    media_manifest_path: str
    media_path: str | None
    estimated_bytes: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": "media_ready",
            "cache_hit": self.cache_hit,
            "media_manifest_path": self.media_manifest_path,
            "media_path": self.media_path,
            "estimated_bytes": self.estimated_bytes,
        }


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MediaError(f"Cannot read media audit file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MediaError(f"Expected a JSON object in {path}.")
    return value


def _selected_metadata(metadata: dict[str, Any], page: int | None) -> dict[str, Any]:
    entries = metadata.get("entries")
    if not isinstance(entries, list) or not entries:
        return metadata
    index = (page or 1) - 1
    if index >= len(entries) or not isinstance(entries[index], dict):
        raise MediaError(f"Requested Bilibili part p={page or 1} is absent from metadata.")
    return entries[index]


def _duration(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _estimate_download_bytes(
    metadata: dict[str, Any], *, start: float, end: float | None
) -> int | None:
    source_duration = _duration(metadata.get("duration"))
    selected_duration = (end - start) if end is not None else source_duration
    if selected_duration is None or selected_duration <= 0:
        return None
    formats = metadata.get("formats") if isinstance(metadata.get("formats"), list) else []
    video_rates: list[float] = []
    audio_rates: list[float] = []
    for item in formats:
        if not isinstance(item, dict):
            continue
        height = item.get("height")
        if isinstance(height, (int, float)) and height > 1080:
            continue
        rate = _duration(item.get("tbr"))
        if rate is None:
            continue
        if item.get("vcodec") not in {None, "none"}:
            video_rates.append(rate)
        elif item.get("acodec") not in {None, "none"}:
            audio_rates.append(rate)
    if video_rates:
        # Conservative within the configured cap; include container overhead.
        kbps = max(video_rates) + (max(audio_rates) if audio_rates else 160.0)
        return int(selected_duration * kbps * 1000 / 8 * 1.10)
    total = metadata.get("filesize") or metadata.get("filesize_approx")
    if isinstance(total, (int, float)) and source_duration:
        return int(float(total) * min(1.0, selected_duration / source_duration) * 1.10)
    rate = _duration(metadata.get("tbr"))
    return int(selected_duration * rate * 1000 / 8 * 1.10) if rate else None


def _ffprobe(path: Path) -> tuple[dict[str, Any], str | None]:
    executable = shutil.which("ffprobe")
    if executable is None:
        raise CommandError("FFprobe is required. Run 'lectureflow doctor'.")
    result = run_command(
        (
            executable,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            path,
        ),
        timeout=180,
    )
    try:
        return json.loads(result.stdout), available_tool_version("ffprobe")
    except json.JSONDecodeError as exc:
        raise MediaError(f"FFprobe returned invalid JSON: {exc}") from exc


def _video_facts(payload: dict[str, Any]) -> tuple[float, int, int, str, str | None, bool]:
    streams = payload.get("streams") if isinstance(payload.get("streams"), list) else []
    video = next(
        (item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"),
        None,
    )
    if video is None:
        raise MediaError("Acquired media has no video stream.")
    raw_duration = payload.get("format", {}).get("duration")
    if raw_duration in {None, "N/A"}:
        raw_duration = video.get("duration")
    duration = _duration(raw_duration)
    if duration is None or duration <= 0:
        raise MediaError("Acquired media has no finite positive duration.")
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if width <= 0 or height <= 0:
        raise MediaError("Acquired media has invalid video dimensions.")
    container = str(payload.get("format", {}).get("format_name") or "unknown")
    audio = any(isinstance(item, dict) and item.get("codec_type") == "audio" for item in streams)
    return duration, width, height, container, video.get("codec_name"), audio


def _media_input_hash(files: JobFiles, manifest: Any, selected: dict[str, Any]) -> str:
    if manifest.source.kind == "local":
        source_identity: Any = fingerprint_local_file(Path(manifest.source.normalized))
    else:
        source_identity = {
            "kind": manifest.source.kind,
            "normalized": manifest.source.normalized,
            "bvid": manifest.source.bvid,
            "part": manifest.source.page or 1,
            "entry_id": selected.get("id"),
            "duration": selected.get("duration"),
            "upload_date": selected.get("upload_date"),
        }
    return hash_object(
        {
            "schema": MEDIA_STAGE_SCHEMA,
            "source": source_identity,
            "range": manifest.requested_range.model_dump(mode="json"),
            "selector": FORMAT_SELECTOR if manifest.source.kind == "bilibili" else None,
            "ffprobe": available_tool_version("ffprobe"),
            "yt_dlp": _yt_dlp_version() if manifest.source.kind == "bilibili" else None,
        }
    )


def _load_valid_cached(files: JobFiles, input_hash: str, manifest: Any) -> MediaManifest | None:
    if not files.media_manifest.is_file():
        return None
    try:
        value = MediaManifest.model_validate(_load_json(files.media_manifest))
    except (ValidationError, ValueError):
        return None
    if value.storage == "job_local":
        target = files.root / str(value.media_path)
    else:
        job_manifest = json.loads(files.manifest.read_text(encoding="utf-8"))
        target = Path(job_manifest["source"]["normalized"])
    if not target.is_file() or hash_file(target) != value.sha256:
        return None
    if value.input_hash == input_hash:
        return value
    # One-time compatibility for media acquired with the legacy metadata-file hash.
    # Bilibili refreshes signed URLs inside otherwise identical metadata; identity,
    # range, selector, tool versions, and the cached content hash remain authoritative.
    expected_source = manifest.source.bvid or manifest.source.display_name
    expected_part = manifest.source.page or 1
    compatible = (
        value.source_kind == manifest.source.kind
        and value.source_id == expected_source
        and value.selected_part == expected_part
        and value.requested_start == manifest.requested_range.start
        and value.requested_end == manifest.requested_range.end
        and value.format_selector == FORMAT_SELECTOR
        and value.tool_versions.get("yt-dlp") == _yt_dlp_version()
        and value.tool_versions.get("ffprobe") == available_tool_version("ffprobe")
    )
    return value if compatible else None


def resolve_media_path(files: JobFiles, media: MediaManifest) -> Path:
    if media.storage == "job_local":
        path = (files.root / str(media.media_path)).resolve()
        if files.root.resolve() not in path.parents:
            raise MediaError("Media path escapes the job workspace.")
        return path
    payload = _load_json(files.manifest)
    path = Path(str(payload["source"]["normalized"]))
    if not path.is_file():
        raise MediaError("Original local media moved or was removed; run prepare again.")
    return path


def acquire_media(job_id: str, *, config: AppConfig) -> MediaAcquireResult:
    files, manifest, state = load_job_context(job_id, config=config)
    if manifest.source.kind == "subtitle":
        raise SourceError("A subtitle-only job has no video to acquire.")
    metadata = _load_json(files.metadata)
    selected = _selected_metadata(metadata, manifest.source.page)
    input_hash = _media_input_hash(files, manifest, selected)
    estimated = (
        _estimate_download_bytes(
            selected,
            start=manifest.requested_range.start,
            end=manifest.requested_range.end,
        )
        if manifest.source.kind == "bilibili"
        else None
    )
    with JobLock(files.lock):
        cached = _load_valid_cached(files, input_hash, manifest)
        if cached is not None:
            if cached.source_kind == "bilibili" and cached.selected_part is None:
                cached.selected_part = manifest.source.page or 1
                cached.part_selection_reason = (
                    "explicit_bilibili_page"
                    if manifest.source.page is not None
                    else "default_first_part_for_media_stage"
                )
                atomic_write_json(files.media_manifest, cached.model_dump(mode="json"))
            record_transition(files.events, state, "stage_cache_hit", stage="media_ready")
            return MediaAcquireResult(
                job_id,
                True,
                str(files.media_manifest.relative_to(files.root)),
                cached.media_path,
                estimated,
            )
        if estimated is not None and estimated > MEDIA_LIMIT_BYTES:
            raise MediaSizeLimitError(
                f"Estimated media download is {estimated / 1024 / 1024:.1f} MiB, above the "
                "500 MiB approval boundary. Narrow --start/--end or explicitly approve a larger "
                "download in a new request."
            )
        media_path: Path
        storage: str
        selector: str | None = None
        yt_version: str | None = None
        if manifest.source.kind == "local":
            media_path = Path(manifest.source.normalized)
            storage = "external_local"
        else:
            if importlib.util.find_spec("yt_dlp") is None:
                raise MediaError(
                    "yt-dlp is required. Restore the project environment with 'uv sync'."
                )
            source_url = str(selected.get("webpage_url") or manifest.source.normalized)
            with tempfile.TemporaryDirectory(
                prefix=".lectureflow-media-", dir=files.root / "media"
            ) as name:
                temporary = Path(name)
                argv = [
                    sys.executable,
                    "-m",
                    "yt_dlp",
                    "--ignore-config",
                    "--no-playlist",
                    "--no-write-comments",
                    "--no-write-info-json",
                    "--format",
                    FORMAT_SELECTOR,
                    "--merge-output-format",
                    "mp4",
                    "--output",
                    str(temporary / "source.%(ext)s"),
                ]
                if manifest.requested_range.end is not None:
                    argv.extend(
                        [
                            "--download-sections",
                            f"*{manifest.requested_range.start:g}-{manifest.requested_range.end:g}",
                            "--force-keyframes-at-cuts",
                        ]
                    )
                argv.append(source_url)
                try:
                    run_command(argv, timeout=1800, cwd=temporary)
                except CommandError as exc:
                    record_transition(
                        files.events,
                        state,
                        "stage_failed",
                        stage="media_ready",
                        category="media_download_failed",
                        error=redact_text(str(exc)),
                        recoverable=True,
                    )
                    raise MediaError(
                        f"Bilibili media download failed: {redact_text(str(exc))}"
                    ) from exc
                candidates = [item for item in temporary.iterdir() if item.is_file()]
                if len(candidates) != 1:
                    raise MediaError("yt-dlp did not produce exactly one auditable media file.")
                media_path = files.root / "media/video/source.mp4"
                os.replace(candidates[0], media_path)
            storage = "job_local"
            selector = FORMAT_SELECTOR
            yt_version = _yt_dlp_version()
        probe, ffprobe_version = _ffprobe(media_path)
        duration, width, height, container, codec, audio = _video_facts(probe)
        source_duration = _duration(selected.get("duration"))
        relative = str(media_path.relative_to(files.root)) if storage == "job_local" else None
        media = MediaManifest(
            source_kind=manifest.source.kind,
            source_id=manifest.source.bvid or manifest.source.display_name,
            part_id=str(selected.get("id")) if selected.get("id") else None,
            selected_part=(manifest.source.page or 1)
            if manifest.source.kind == "bilibili"
            else None,
            part_selection_reason=(
                "explicit_bilibili_page"
                if manifest.source.kind == "bilibili" and manifest.source.page is not None
                else "default_first_part_for_media_stage"
                if manifest.source.kind == "bilibili"
                else None
            ),
            source_url=str(selected.get("webpage_url")) if selected.get("webpage_url") else None,
            requested_start=manifest.requested_range.start,
            requested_end=manifest.requested_range.end,
            media_path=relative,
            storage=storage,
            container=container,
            duration=duration,
            source_duration=source_duration,
            width=width,
            height=height,
            file_size=media_path.stat().st_size,
            sha256=hash_file(media_path),
            video_codec=str(codec) if codec else None,
            audio_included=audio,
            format_selector=selector,
            tool_versions={"ffprobe": ffprobe_version, "yt-dlp": yt_version},
            input_hash=input_hash,
            config_hash=hash_object({"format_selector": selector, "limit": MEDIA_LIMIT_BYTES}),
            generated_at=utc_now(),
        )
        atomic_write_json(files.media_manifest, media.model_dump(mode="json"))
        record_transition(
            files.events,
            state,
            "stage_succeeded",
            stage="media_ready",
            output_hash=hash_file(files.media_manifest),
        )
        return MediaAcquireResult(
            job_id, False, str(files.media_manifest.relative_to(files.root)), relative, estimated
        )


def inspect_media(job_id: str, *, config: AppConfig) -> dict[str, Any]:
    files, _, _ = load_job_context(job_id, config=config)
    media = MediaManifest.model_validate(_load_json(files.media_manifest))
    path = resolve_media_path(files, media)
    return {
        "job_id": job_id,
        "manifest": media.model_dump(mode="json"),
        "integrity": {
            "exists": path.is_file(),
            "sha256_matches": path.is_file() and hash_file(path) == media.sha256,
        },
    }
