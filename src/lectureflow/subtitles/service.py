from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from lectureflow.asr.base import require_asr_backend
from lectureflow.atomic import atomic_write_bytes, atomic_write_json
from lectureflow.config import AppConfig
from lectureflow.constants import PIPELINE_VERSION
from lectureflow.errors import ASRUnavailableError, StateError, SubtitleError, SubtitleParseError
from lectureflow.hashing import fingerprint_local_file, hash_bytes, hash_file, hash_object
from lectureflow.network import HttpClient
from lectureflow.paths import JobFiles
from lectureflow.pipeline import (
    load_job_context,
    prepare_job,
    register_subtitle_job,
    resume_job,
)
from lectureflow.schemas.state import PipelineState, StageStatus
from lectureflow.schemas.transcript import (
    SubtitleCandidate,
    SubtitleSelection,
    TranscriptDocument,
)
from lectureflow.security import redact_text, sanitize_url
from lectureflow.sources.bilibili_subtitles import (
    BilibiliSubtitleSource,
    public_candidate_metadata,
    select_candidates,
)
from lectureflow.state import (
    JobLock,
    finish_stage_failure,
    finish_stage_success,
    invalidate_from,
    load_state,
    record_transition,
    save_state,
    start_stage,
    utc_now,
)
from lectureflow.subtitles.export import (
    export_document,
    load_document,
    validate_artifact_manifest,
)
from lectureflow.subtitles.formats import (
    assign_stable_segment_ids,
    parse_bilibili_json,
    parse_srt,
    parse_txt,
    parse_vtt,
)

SUPPORTED_SUBTITLE_SUFFIXES = {
    ".json": "bilibili_json",
    ".srt": "srt",
    ".vtt": "vtt",
    ".txt": "txt",
}


@dataclass(frozen=True, slots=True)
class SubtitleProcessResult:
    job_id: str
    status: str
    cache_hit: bool
    selected_sources: dict[str, str | None]
    part_ids: list[str]
    transcript_root: Path
    classifications: dict[str, str] | None = None
    raw_download_cache_hits: int = 0
    raw_download_count: int = 0
    recoverable: bool = True
    message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "cache_hit": self.cache_hit,
            "selected_sources": self.selected_sources,
            "part_ids": self.part_ids,
            "classifications": self.classifications or {},
            "transcript_root": str(self.transcript_root),
            "raw_download_cache_hits": self.raw_download_cache_hits,
            "raw_download_count": self.raw_download_count,
            "recoverable": self.recoverable,
            "message": self.message,
        }


def detect_subtitle_format(path: Path) -> str:
    subtitle_format = SUPPORTED_SUBTITLE_SUFFIXES.get(path.suffix.lower())
    if subtitle_format is None:
        supported = ", ".join(sorted(SUPPORTED_SUBTITLE_SUFFIXES))
        raise SubtitleError(f"Unsupported subtitle format {path.suffix!r}; expected {supported}.")
    return subtitle_format


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise StateError(f"Expected a JSON object in {path}")
    return value


def _latest_success(state: PipelineState, stage: str) -> tuple[str, str, str] | None:
    record = state.stages[stage]
    if record.status != StageStatus.SUCCEEDED:
        return None
    for attempt in reversed(record.attempts):
        if attempt.finished_at and attempt.output_hash and attempt.error is None:
            return attempt.input_hash, attempt.config_hash, attempt.output_hash
    return None


def _stage_file_cache_valid(
    state: PipelineState,
    stage: str,
    path: Path,
    *,
    input_hash: str,
    config_hash: str,
) -> bool:
    previous = _latest_success(state, stage)
    if previous is None or not path.is_file():
        return False
    return previous == (input_hash, config_hash, hash_file(path))


def _result_path(files: JobFiles) -> Path:
    return files.root / "transcript/subtitle-result.json"


def _safe_job_relative(files: JobFiles, value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise StateError("Subtitle artifact path must be a non-empty relative string.")
    relative = Path(value)
    if relative.is_absolute():
        raise StateError("Subtitle artifact path must be relative to its job workspace.")
    root = files.root.resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise StateError("Subtitle artifact path escapes its job workspace.")
    return target


def _part_artifacts_valid(files: JobFiles, relative: object) -> bool:
    try:
        part_dir = _safe_job_relative(files, relative)
        if not validate_artifact_manifest(part_dir):
            return False
        source_report = _load_json(part_dir / "subtitle-source.json")
        raw_path = _safe_job_relative(files, source_report.get("raw_file"))
        expected_hash = source_report.get("source_sha256")
        return (
            raw_path.is_file()
            and isinstance(expected_hash, str)
            and hash_file(raw_path) == expected_hash
        )
    except (OSError, StateError):
        return False


def _result_cache_valid(
    files: JobFiles,
    state: PipelineState,
    *,
    input_hash: str,
    config_hash: str,
) -> bool:
    previous = _latest_success(state, "transcript_ready")
    result_path = _result_path(files)
    if previous is None or not result_path.is_file():
        return False
    old_input, old_config, old_output = previous
    if old_input != input_hash or old_config != config_hash or hash_file(result_path) != old_output:
        return False
    result = _load_json(result_path)
    part_paths = result.get("part_paths")
    if not isinstance(part_paths, dict):
        return False
    return all(_part_artifacts_valid(files, relative) for relative in part_paths.values())


def _no_subtitle_cache_valid(
    files: JobFiles,
    state: PipelineState,
    *,
    input_hash: str,
    config_hash: str,
) -> bool:
    previous = _latest_success(state, "subtitle_ready")
    result_path = _result_path(files)
    selection_path = files.root / "raw/subtitles/subtitle-selection-report.json"
    if previous is None or not result_path.is_file() or not selection_path.is_file():
        return False
    old_input, old_config, old_output = previous
    try:
        result = _load_json(result_path)
    except StateError:
        return False
    output_hash = hash_object(
        {
            "selection": hash_file(selection_path),
            "result": hash_file(result_path),
        }
    )
    return (
        result.get("status") == "no_public_subtitles"
        and old_input == input_hash
        and old_config == config_hash
        and old_output == output_hash
    )


def _read_cached_result(files: JobFiles) -> SubtitleProcessResult:
    result = _load_json(_result_path(files))
    return SubtitleProcessResult(
        job_id=str(result["job_id"]),
        status=str(result["status"]),
        cache_hit=True,
        selected_sources={str(key): value for key, value in result["selected_sources"].items()},
        part_ids=[str(item) for item in result["part_ids"]],
        transcript_root=files.root / "transcript",
        classifications={
            str(key): str(value) for key, value in result.get("classifications", {}).items()
        },
        raw_download_cache_hits=0,
        raw_download_count=0,
        recoverable=bool(result.get("recoverable", True)),
        message=result.get("message"),
    )


def _require_result_integrity(files: JobFiles, state: PipelineState) -> dict[str, Any]:
    result_path = _result_path(files)
    previous = _latest_success(state, "transcript_ready")
    if previous is None or not result_path.is_file() or hash_file(result_path) != previous[2]:
        raise StateError(
            "Transcript result does not match its successful stage audit hash; "
            "run resume to rebuild it."
        )
    result = _load_json(result_path)
    part_paths = result.get("part_paths")
    if not isinstance(part_paths, dict) or not all(
        _part_artifacts_valid(files, relative) for relative in part_paths.values()
    ):
        raise StateError(
            "One or more transcript artifacts or immutable raw subtitles failed integrity "
            "validation; run resume to rebuild derived outputs."
        )
    return result


def _start_or_restart_stage(
    files: JobFiles,
    state: PipelineState,
    stage: str,
    *,
    input_hash: str,
    config_hash: str,
    tool: str,
) -> str:
    if state.stages[stage].status in {
        StageStatus.SUCCEEDED,
        StageStatus.FAILED,
        StageStatus.SKIPPED,
    }:
        changed = invalidate_from(state, stage)
        save_state(files.state, state)
        record_transition(
            files.events,
            state,
            "stages_invalidated",
            stage=stage,
            stages=changed,
            reason="stage_restart",
        )
    attempt_id = start_stage(
        state,
        stage,
        input_hash=input_hash,
        config_hash=config_hash,
        tool=tool,
        tool_version=PIPELINE_VERSION,
    )
    save_state(files.state, state)
    record_transition(files.events, state, "stage_started", stage=stage, tool=tool)
    return attempt_id


def _finish_success(
    files: JobFiles,
    state: PipelineState,
    stage: str,
    attempt_id: str,
    *,
    output_hash: str,
) -> None:
    finish_stage_success(state, stage, attempt_id, output_hash=output_hash)
    save_state(files.state, state)
    record_transition(
        files.events,
        state,
        "stage_succeeded",
        stage=stage,
        output_hash=output_hash,
    )


def _finish_failure(
    files: JobFiles,
    state: PipelineState,
    stage: str,
    attempt_id: str,
    *,
    error: str,
) -> None:
    finish_stage_failure(state, stage, attempt_id, error=error, recoverable=True)
    save_state(files.state, state)
    record_transition(
        files.events,
        state,
        "stage_failed",
        stage=stage,
        error=redact_text(error),
        recoverable=True,
    )


def _mark_audio_not_required(files: JobFiles, state: PipelineState) -> None:
    record = state.stages["audio_ready"]
    if record.status in {StageStatus.PENDING, StageStatus.INVALIDATED, StageStatus.FAILED}:
        record.status = StageStatus.SKIPPED
        save_state(files.state, state)
        record_transition(
            files.events,
            state,
            "stage_skipped",
            stage="audio_ready",
            reason="reliable_subtitle_selected_no_asr_needed",
        )


def _parse_local_document(
    path: Path,
    *,
    subtitle_format: str,
    language: str,
    part_id: str = "local",
    source_ref: dict[str, Any] | None = None,
) -> TranscriptDocument:
    raw_bytes = path.read_bytes()
    reference = source_ref or {
        "raw_file": str(path),
        "format": subtitle_format,
        "sha256": hash_file(path),
    }
    if subtitle_format == "bilibili_json":
        return parse_bilibili_json(
            raw_bytes,
            source="local_file",
            language=language,
            part_id=part_id,
            source_ref=reference,
        )
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SubtitleParseError("Local subtitle must be UTF-8 encoded.") from exc
    if subtitle_format == "srt":
        return parse_srt(
            text,
            language=language,
            part_id=part_id,
            source_ref=reference,
        )
    if subtitle_format == "vtt":
        return parse_vtt(
            text,
            language=language,
            part_id=part_id,
            source_ref=reference,
        )
    return parse_txt(
        text,
        language=language,
        part_id=part_id,
        source_ref=reference,
    )


def _filter_document_range(
    document: TranscriptDocument,
    *,
    start: float,
    end: float | None,
) -> TranscriptDocument:
    if not document.timed or (start == 0 and end is None):
        return document
    segments = [
        segment
        for segment in document.segments
        if segment.end >= start and (end is None or segment.start <= end)
    ]
    return document.model_copy(
        update={
            "segments": segments,
            "source_ref": {
                **document.source_ref,
                "requested_range": {"start": start, "end": end},
            },
        }
    )


def _range_duration(
    *,
    media_duration: float | None,
    start: float,
    end: float | None,
) -> float | None:
    upper = end if end is not None else media_duration
    if upper is None:
        return None
    return (
        max(0.0, min(upper, media_duration) - start) if media_duration else max(0.0, upper - start)
    )


def import_local_subtitle(
    path: Path,
    *,
    config: AppConfig,
    language: str = "und",
    force_stage: bool = False,
) -> SubtitleProcessResult:
    subtitle_format = detect_subtitle_format(path)
    registration = register_subtitle_job(path, subtitle_format=subtitle_format, config=config)
    files, manifest, state = load_job_context(registration.job_id, config=config)
    source_path = Path(manifest.source.normalized)
    fingerprint = fingerprint_local_file(source_path)
    config_hash = hash_object(config.model_dump(mode="json"))
    input_hash = hash_object(
        {
            "source": manifest.source.model_dump(mode="json"),
            "fingerprint": fingerprint,
            "language": language,
            "cleaning_schema": "deterministic-cleaning-1.0.0",
            "export_schema": "subtitle-export-1.0.0",
        }
    )
    with JobLock(files.lock):
        state = load_state(files.state)
        if force_stage and state.stages["subtitle_ready"].status != StageStatus.PENDING:
            invalidate_from(state, "subtitle_ready")
            save_state(files.state, state)
        if not force_stage and _result_cache_valid(
            files,
            state,
            input_hash=input_hash,
            config_hash=config_hash,
        ):
            return _read_cached_result(files)
        raw_dir = files.root / "raw/subtitles/local"
        raw_dir.mkdir(parents=True, exist_ok=True)
        source_digest = hash_file(source_path)
        raw_path = raw_dir / f"{source_digest}.original{source_path.suffix.lower()}"
        source_marker = files.root / "raw/subtitles/subtitle-source.json"
        subtitle_cache_hit = not force_stage and _stage_file_cache_valid(
            state,
            "subtitle_ready",
            source_marker,
            input_hash=input_hash,
            config_hash=config_hash,
        )
        subtitle_attempt = None
        if not subtitle_cache_hit:
            subtitle_attempt = _start_or_restart_stage(
                files,
                state,
                "subtitle_ready",
                input_hash=input_hash,
                config_hash=config_hash,
                tool="lectureflow-subtitle-import",
            )
        try:
            if not raw_path.is_file():
                atomic_write_bytes(raw_path, source_path.read_bytes())
            document = _parse_local_document(
                raw_path,
                subtitle_format=subtitle_format,
                language=language,
            )
            source_report = (
                _load_json(source_marker)
                if subtitle_cache_hit
                else {
                    "schema_version": "1.0.0",
                    "source": "local_file",
                    "language": language,
                    "part_id": "local",
                    "generated_at": utc_now(),
                    "model": None,
                    "raw_file": str(raw_path.relative_to(files.root)),
                    "format": subtitle_format,
                    "source_sha256": hash_file(raw_path),
                    "timed": document.timed,
                }
            )
            candidate = SubtitleCandidate(
                candidate_id=f"local-{hash_file(raw_path)[:16]}",
                source="local_file",
                language=language,
                part_id="local",
                metadata={"format": subtitle_format},
            )
            selection = SubtitleSelection(
                part_id="local",
                selected_candidate_id=candidate.candidate_id,
                selected_source="local_file",
                reason="local_subtitle_explicitly_imported",
                candidates=[candidate],
            )
            selection_report = {
                "schema_version": "1.0.0",
                **selection.model_dump(mode="json"),
                "priority_order": config.subtitles.sources,
            }
            if subtitle_attempt is not None:
                atomic_write_json(source_marker, source_report)
                _finish_success(
                    files,
                    state,
                    "subtitle_ready",
                    subtitle_attempt,
                    output_hash=hash_file(source_marker),
                )
        except Exception as exc:
            if (
                subtitle_attempt is not None
                and state.stages["subtitle_ready"].status == StageStatus.RUNNING
            ):
                _finish_failure(
                    files,
                    state,
                    "subtitle_ready",
                    subtitle_attempt,
                    error=str(exc),
                )
            if isinstance(exc, SubtitleError):
                raise SubtitleError(redact_text(str(exc))) from exc
            if isinstance(exc, (OSError, ValidationError, ValueError)):
                raise SubtitleParseError(
                    f"Cannot import local subtitle: {redact_text(str(exc))}"
                ) from exc
            raise
        _mark_audio_not_required(files, state)
        transcript_attempt = _start_or_restart_stage(
            files,
            state,
            "transcript_ready",
            input_hash=input_hash,
            config_hash=config_hash,
            tool="lectureflow-subtitle-export",
        )
        try:
            export_document(
                document,
                files.root / "transcript",
                subtitle_source=source_report,
                selection_report=selection_report,
            )
            result_payload = {
                "schema_version": "1.0.0",
                "job_id": manifest.job_id,
                "status": "transcript_ready",
                "selected_sources": {"local": "local_file"},
                "part_ids": ["local"],
                "part_paths": {"local": "transcript"},
                "raw_download_cache_hits": 0,
                "raw_download_count": 0,
                "recoverable": True,
                "message": (
                    "Untimed TXT preserved without fabricated cue timestamps."
                    if not document.timed
                    else None
                ),
            }
            atomic_write_json(_result_path(files), result_payload)
            _finish_success(
                files,
                state,
                "transcript_ready",
                transcript_attempt,
                output_hash=hash_file(_result_path(files)),
            )
        except Exception as exc:
            if state.stages["transcript_ready"].status == StageStatus.RUNNING:
                _finish_failure(
                    files,
                    state,
                    "transcript_ready",
                    transcript_attempt,
                    error=str(exc),
                )
            raise SubtitleError(f"Subtitle export failed: {redact_text(str(exc))}") from exc
    return SubtitleProcessResult(
        job_id=manifest.job_id,
        status="transcript_ready",
        cache_hit=False,
        selected_sources={"local": "local_file"},
        part_ids=["local"],
        transcript_root=files.root / "transcript",
        raw_download_cache_hits=0,
        raw_download_count=0,
        message=result_payload["message"],
    )


def _copy_single_part_exports_to_root(files: JobFiles, part_dir: Path) -> None:
    for source in part_dir.iterdir():
        if source.is_file():
            atomic_write_bytes(files.root / "transcript" / source.name, source.read_bytes())


def fetch_bilibili_subtitles(
    source_value: str,
    *,
    config: AppConfig,
    start: float = 0.0,
    end: float | None = None,
    force_stage: bool = False,
    offline: bool = False,
    local_subtitle: Path | None = None,
    local_language: str = "und",
    client: HttpClient | None = None,
) -> SubtitleProcessResult:
    if offline and force_stage:
        raise SubtitleError("--offline cannot be combined with --force-stage subtitle_ready.")
    registration = prepare_job(
        source_value,
        config=config,
        start=start,
        end=end,
        offline=offline,
    )
    files, manifest, state = load_job_context(registration.job_id, config=config)
    if manifest.source.kind != "bilibili":
        raise SubtitleError("subtitles fetch requires a Bilibili source")
    metadata = _load_json(files.metadata)
    fallback_marker = files.root / "raw/subtitles/local-fallback.json"
    if local_subtitle is None and fallback_marker.is_file():
        marker = _load_json(fallback_marker)
        local_subtitle = Path(str(marker["path"]))
        local_language = str(marker.get("language") or "und")
    fallback_path: Path | None = None
    fallback_format: str | None = None
    fallback_fingerprint: dict[str, Any] | None = None
    if local_subtitle is not None:
        try:
            fallback_path = local_subtitle.expanduser().resolve(strict=True)
        except OSError as exc:
            raise SubtitleError(
                f"Local fallback subtitle does not exist: {local_subtitle}"
            ) from exc
        if not fallback_path.is_file():
            raise SubtitleError(f"Local fallback subtitle is not a regular file: {fallback_path}")
        fallback_format = detect_subtitle_format(fallback_path)
        fallback_fingerprint = fingerprint_local_file(fallback_path)
    config_hash = hash_object(config.model_dump(mode="json"))
    input_hash = hash_object(
        {
            "source": manifest.source.model_dump(mode="json"),
            "metadata_sha256": hash_file(files.metadata),
            "preferred_languages": config.subtitles.preferred_languages,
            "priority": config.subtitles.sources,
            "local_fallback": fallback_fingerprint,
            "local_fallback_language": local_language if fallback_path else None,
            "subtitle_schema": "bilibili-subtitle-fetch-1.0.0",
        }
    )
    http_client = client or HttpClient(
        cache_dir=files.root / "raw/subtitles/http-cache",
        timeout=config.subtitles.http_timeout_seconds,
        retries=config.subtitles.http_retries,
        user_agent=config.subtitles.user_agent,
        offline=offline,
    )
    source_backend = BilibiliSubtitleSource(http_client)
    with JobLock(files.lock):
        state = load_state(files.state)
        if force_stage and state.stages["subtitle_ready"].status != StageStatus.PENDING:
            changed = invalidate_from(state, "subtitle_ready")
            save_state(files.state, state)
            record_transition(
                files.events,
                state,
                "stages_invalidated",
                stage="subtitle_ready",
                stages=changed,
                reason="forced",
            )
        if not force_stage and _result_cache_valid(
            files,
            state,
            input_hash=input_hash,
            config_hash=config_hash,
        ):
            return _read_cached_result(files)
        if not force_stage and _no_subtitle_cache_valid(
            files,
            state,
            input_hash=input_hash,
            config_hash=config_hash,
        ):
            return _read_cached_result(files)
        subtitle_attempt = _start_or_restart_stage(
            files,
            state,
            "subtitle_ready",
            input_hash=input_hash,
            config_hash=config_hash,
            tool="lectureflow-bilibili-subtitles",
        )
        try:
            parts = source_backend.enumerate_parts(
                manifest.source,
                metadata=metadata,
                force=force_stage,
            )
            if fallback_path is not None and len(parts) != 1:
                raise SubtitleError(
                    "A single --local-subtitle is ambiguous for a multi-part course. "
                    "Select one part with ?p=N, then retry."
                )
            if fallback_path is not None:
                atomic_write_json(
                    fallback_marker,
                    {
                        "schema_version": "1.0.0",
                        "path": str(fallback_path),
                        "language": local_language,
                        "format": fallback_format,
                        "fingerprint": fallback_fingerprint,
                    },
                )
            selections: list[SubtitleSelection] = []
            selected_payloads: list[tuple[Any, SubtitleCandidate, bytes, bool, Path, str]] = []
            login_required_parts: list[str] = []
            part_classifications: dict[str, str] = {}
            candidate_cache_hits = 0
            download_cache_hits = 0
            for part in parts:
                candidates, need_login, candidate_cache_hit = source_backend.enumerate_candidates(
                    part,
                    force=force_stage,
                )
                candidate_cache_hits += int(candidate_cache_hit)
                if need_login:
                    login_required_parts.append(part.part_id)
                if fallback_path is not None:
                    candidates.append(
                        SubtitleCandidate(
                            candidate_id=f"{part.part_id}-local-{hash_file(fallback_path)[:16]}",
                            source="local_file",
                            language=local_language,
                            part_id=part.part_id,
                            part_number=part.page,
                            cid=part.cid,
                            published_at=part.published_at,
                            metadata={
                                "format": fallback_format,
                                "sha256": hash_file(fallback_path),
                            },
                        )
                    )
                selection = select_candidates(
                    candidates,
                    preferred_languages=config.subtitles.preferred_languages,
                )
                if not candidates:
                    selection = selection.model_copy(update={"part_id": part.part_id})
                if selection.selected_source == "uploader":
                    part_classifications[part.part_id] = "A"
                elif selection.selected_source == "bilibili_ai":
                    part_classifications[part.part_id] = "B"
                elif selection.selected_source == "local_file":
                    part_classifications[part.part_id] = "local_file"
                elif need_login:
                    part_classifications[part.part_id] = "E"
                else:
                    part_classifications[part.part_id] = "D"
                selections.append(selection)
                if selection.selected_candidate_id is None:
                    continue
                selected = next(
                    candidate
                    for candidate in candidates
                    if candidate.candidate_id == selection.selected_candidate_id
                )
                if selected.source == "local_file":
                    assert fallback_path is not None
                    assert fallback_format is not None
                    raw = fallback_path.read_bytes()
                    cache_hit = False
                    payload_format = fallback_format
                else:
                    raw, cache_hit = source_backend.download_candidate(
                        selected,
                        force=force_stage,
                    )
                    download_cache_hits += int(cache_hit)
                    payload_format = "bilibili_json"
                raw_dir = files.root / "raw/subtitles/parts" / part.part_id
                raw_dir.mkdir(parents=True, exist_ok=True)
                raw_digest = hash_bytes(raw)
                raw_suffix = {
                    "bilibili_json": ".json",
                    "srt": ".srt",
                    "vtt": ".vtt",
                    "txt": ".txt",
                }[payload_format]
                raw_path = (
                    raw_dir / f"{selected.candidate_id}.{raw_digest[:16]}.original{raw_suffix}"
                )
                if not raw_path.exists() or hash_file(raw_path) != hash_bytes(raw):
                    atomic_write_bytes(raw_path, raw)
                selected_payloads.append((part, selected, raw, cache_hit, raw_path, payload_format))
            aggregate_selection = {
                "schema_version": "1.0.0",
                "job_id": manifest.job_id,
                "priority_order": config.subtitles.sources,
                "parts": [
                    {
                        "part_id": part.part_id,
                        "page": part.page,
                        "cid": part.cid,
                        "title": part.title,
                        "duration": part.duration,
                        "bvid": part.bvid,
                        "aid": part.aid,
                        "published_at": part.published_at,
                    }
                    for part in parts
                ],
                "selections": [
                    {
                        **selection.model_dump(mode="json", exclude={"candidates"}),
                        "classification": part_classifications[selection.part_id],
                        "candidates": [
                            public_candidate_metadata(candidate)
                            for candidate in selection.candidates
                        ],
                    }
                    for selection in selections
                ],
                "login_required_parts": login_required_parts,
                "cookies_used": False,
                "generated_at": utc_now(),
            }
            raw_report = files.root / "raw/subtitles/subtitle-selection-report.json"
            atomic_write_json(raw_report, aggregate_selection)
            if not selected_payloads:
                code_counts = {
                    code: sum(item == code for item in part_classifications.values())
                    for code in sorted(set(part_classifications.values()))
                }
                no_subtitle_report = {
                    "schema_version": "1.0.0",
                    "job_id": manifest.job_id,
                    "status": "no_public_subtitles",
                    "selected_sources": {selection.part_id: None for selection in selections},
                    "classifications": part_classifications,
                    "classification_counts": code_counts,
                    "part_ids": [part.part_id for part in parts],
                    "part_paths": {},
                    "raw_download_cache_hits": 0,
                    "raw_download_count": 0,
                    "recoverable": True,
                    "message": (
                        "; ".join(f"{code}: {count} part(s)" for code, count in code_counts.items())
                        + ". No public subtitle was downloaded and no ASR model download "
                        "was attempted."
                    ),
                }
                atomic_write_json(_result_path(files), no_subtitle_report)
                _finish_success(
                    files,
                    state,
                    "subtitle_ready",
                    subtitle_attempt,
                    output_hash=hash_object(
                        {
                            "selection": hash_file(raw_report),
                            "result": hash_file(_result_path(files)),
                        }
                    ),
                )
                return SubtitleProcessResult(
                    job_id=manifest.job_id,
                    status="no_public_subtitles",
                    cache_hit=False,
                    selected_sources=no_subtitle_report["selected_sources"],
                    part_ids=no_subtitle_report["part_ids"],
                    transcript_root=files.root / "transcript",
                    classifications=part_classifications,
                    recoverable=True,
                    message=no_subtitle_report["message"],
                )
            _finish_success(
                files,
                state,
                "subtitle_ready",
                subtitle_attempt,
                output_hash=hash_file(raw_report),
            )
        except Exception as exc:
            if state.stages["subtitle_ready"].status == StageStatus.RUNNING:
                _finish_failure(
                    files,
                    state,
                    "subtitle_ready",
                    subtitle_attempt,
                    error=str(exc),
                )
            if isinstance(exc, SubtitleError):
                raise SubtitleError(redact_text(str(exc))) from exc
            raise SubtitleError(f"Bilibili subtitle fetch failed: {redact_text(str(exc))}") from exc
        _mark_audio_not_required(files, state)
        transcript_attempt = _start_or_restart_stage(
            files,
            state,
            "transcript_ready",
            input_hash=input_hash,
            config_hash=config_hash,
            tool="lectureflow-subtitle-export",
        )
        part_paths: dict[str, str] = {}
        selected_sources: dict[str, str | None] = {
            selection.part_id: selection.selected_source for selection in selections
        }
        try:
            next_segment_index = 1
            for part, candidate, raw, _, raw_path, payload_format in selected_payloads:
                source_ref = {
                    "candidate_id": candidate.candidate_id,
                    "cid": part.cid,
                    "page": part.page,
                    "bvid": part.bvid,
                    "raw_file": str(raw_path.relative_to(files.root)),
                    "subtitle_url": sanitize_url(candidate.subtitle_url or ""),
                    "published_at": part.published_at,
                }
                if candidate.source == "local_file":
                    document = _parse_local_document(
                        raw_path,
                        subtitle_format=payload_format,
                        language=candidate.language,
                        part_id=part.part_id,
                        source_ref=source_ref,
                    )
                else:
                    document = parse_bilibili_json(
                        raw,
                        source=candidate.source,
                        language=candidate.language,
                        part_id=part.part_id,
                        source_ref=source_ref,
                    )
                document = _filter_document_range(
                    document,
                    start=manifest.requested_range.start,
                    end=manifest.requested_range.end,
                )
                document, next_segment_index = assign_stable_segment_ids(
                    document,
                    first_index=next_segment_index,
                )
                source_report = {
                    "schema_version": "1.0.0",
                    "source": candidate.source,
                    "language": candidate.language,
                    "part_id": part.part_id,
                    "part_number": part.page,
                    "cid": part.cid,
                    "published_at": part.published_at,
                    "generated_at": utc_now(),
                    "model": None,
                    "format": payload_format,
                    "raw_file": str(raw_path.relative_to(files.root)),
                    "source_sha256": hash_file(raw_path),
                    "source_ref": source_ref,
                }
                selection = next(item for item in selections if item.part_id == part.part_id)
                part_dir = files.root / "transcript/parts" / part.part_id
                export_document(
                    document,
                    part_dir,
                    subtitle_source=source_report,
                    selection_report={
                        "schema_version": "1.0.0",
                        **selection.model_dump(mode="json", exclude={"candidates"}),
                        "candidates": [
                            public_candidate_metadata(item) for item in selection.candidates
                        ],
                        "priority_order": config.subtitles.sources,
                    },
                    media_duration=_range_duration(
                        media_duration=part.duration,
                        start=manifest.requested_range.start,
                        end=manifest.requested_range.end,
                    ),
                    media_start=manifest.requested_range.start,
                )
                part_paths[part.part_id] = str(part_dir.relative_to(files.root))
            if len(part_paths) == 1:
                only_part_dir = files.root / next(iter(part_paths.values()))
                _copy_single_part_exports_to_root(files, only_part_dir)
            result_payload = {
                "schema_version": "1.0.0",
                "job_id": manifest.job_id,
                "status": "transcript_ready",
                "selected_sources": selected_sources,
                "classifications": part_classifications,
                "part_ids": [part.part_id for part in parts],
                "part_paths": part_paths,
                "raw_download_cache_hits": download_cache_hits,
                "raw_download_count": sum(
                    item[1].source != "local_file" and not item[3] for item in selected_payloads
                ),
                "candidate_response_cache_hits": candidate_cache_hits,
                "recoverable": True,
                "message": None,
            }
            atomic_write_json(_result_path(files), result_payload)
            _finish_success(
                files,
                state,
                "transcript_ready",
                transcript_attempt,
                output_hash=hash_file(_result_path(files)),
            )
        except Exception as exc:
            if state.stages["transcript_ready"].status == StageStatus.RUNNING:
                _finish_failure(
                    files,
                    state,
                    "transcript_ready",
                    transcript_attempt,
                    error=str(exc),
                )
            if isinstance(exc, SubtitleError):
                raise SubtitleError(redact_text(str(exc))) from exc
            raise SubtitleError(
                f"Bilibili transcript export failed: {redact_text(str(exc))}"
            ) from exc
    return SubtitleProcessResult(
        job_id=manifest.job_id,
        status="transcript_ready",
        cache_hit=False,
        selected_sources=selected_sources,
        part_ids=[part.part_id for part in parts],
        transcript_root=files.root / "transcript",
        classifications=part_classifications,
        raw_download_cache_hits=download_cache_hits,
        raw_download_count=sum(
            item[1].source != "local_file" and not item[3] for item in selected_payloads
        ),
    )


def inspect_subtitles(job_id: str, *, config: AppConfig) -> dict[str, Any]:
    files, manifest, state = load_job_context(job_id, config=config)
    if state.stages["transcript_ready"].status == StageStatus.SUCCEEDED:
        result = _require_result_integrity(files, state)
    else:
        result = _load_json(_result_path(files)) if _result_path(files).is_file() else None
    return {
        "schema_version": "1.0.0",
        "job_id": job_id,
        "source": manifest.source.model_dump(mode="json"),
        "subtitle_stage": state.stages["subtitle_ready"].status.value,
        "transcript_stage": state.stages["transcript_ready"].status.value,
        "result": result,
    }


def export_job_subtitles(job_id: str, *, config: AppConfig) -> SubtitleProcessResult:
    files, _, state = load_job_context(job_id, config=config)
    result = _require_result_integrity(files, state)
    if result.get("status") != "transcript_ready":
        raise SubtitleError(f"Job {job_id} has no transcript to export.")
    for relative in result["part_paths"].values():
        part_dir = _safe_job_relative(files, relative)
        document = load_document(part_dir / "original.json")
        source_report = _load_json(part_dir / "subtitle-source.json")
        selection_report = _load_json(part_dir / "subtitle-selection-report.json")
        quality = _load_json(part_dir / "subtitle-quality-report.json")
        export_document(
            document,
            part_dir,
            subtitle_source=source_report,
            selection_report=selection_report,
            media_duration=quality.get("total_duration"),
            media_start=float(quality.get("range_start", 0.0)),
        )
    if len(result["part_paths"]) == 1 and next(iter(result["part_paths"].values())) != "transcript":
        _copy_single_part_exports_to_root(
            files,
            _safe_job_relative(files, next(iter(result["part_paths"].values()))),
        )
    return SubtitleProcessResult(
        job_id=job_id,
        status="transcript_ready",
        cache_hit=False,
        selected_sources=result["selected_sources"],
        part_ids=result["part_ids"],
        transcript_root=files.root / "transcript",
        recoverable=True,
        message="Exports regenerated from immutable original.json.",
    )


def resume_to_transcript(job_id: str, *, config: AppConfig) -> SubtitleProcessResult:
    files, manifest, _ = load_job_context(job_id, config=config)
    if manifest.source.kind == "subtitle":
        language = "und"
        source_report_path = files.root / "raw/subtitles/subtitle-source.json"
        if source_report_path.is_file():
            language = str(_load_json(source_report_path).get("language") or "und")
        return import_local_subtitle(
            Path(manifest.source.normalized),
            config=config,
            language=language,
        )
    if manifest.source.kind == "bilibili":
        return fetch_bilibili_subtitles(manifest.source.normalized, config=config)
    resume_job(job_id, config=config)
    require_asr_backend(config.asr)
    raise ASRUnavailableError(
        "A local media job without an imported subtitle requires ASR. "
        "The backend interface is ready, but transcription execution is scheduled after M1."
    )
