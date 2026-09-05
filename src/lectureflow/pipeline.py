from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from lectureflow.atomic import atomic_write_json
from lectureflow.config import AppConfig
from lectureflow.constants import PIPELINE_VERSION
from lectureflow.errors import CommandError, JobNotFoundError, PrepareError, SourceError, StateError
from lectureflow.hashing import fingerprint_local_file, hash_file, hash_object
from lectureflow.paths import JobFiles, ensure_job_layout, job_path
from lectureflow.schemas.manifest import JobManifest, SourceDescriptor, TimeRange
from lectureflow.schemas.state import PipelineState, StageStatus
from lectureflow.security import redact, redact_text
from lectureflow.sources import identify_source
from lectureflow.sources.metadata import available_tool_version, probe_bilibili, probe_local
from lectureflow.state import (
    JobLock,
    finish_stage_failure,
    finish_stage_success,
    invalidate_from,
    load_state,
    mark_interrupted_running_stages,
    new_state,
    record_transition,
    save_state,
    start_stage,
    utc_now,
)


@dataclass(frozen=True, slots=True)
class PrepareResult:
    job_id: str
    job_dir: Path
    metadata_path: Path | None
    cache_hit: bool
    current_milestone: str | None


def _range_identity(time_range: TimeRange) -> dict[str, float | None]:
    return {"start": time_range.start, "end": time_range.end}


def _job_id(source: SourceDescriptor, time_range: TimeRange) -> str:
    range_hash = hash_object(_range_identity(time_range))[:8]
    if source.kind == "bilibili":
        identity = source.bvid or (f"av{source.aid}" if source.aid is not None else None)
        if identity is None:
            identity = f"short-{hash_object(source.normalized)[:12]}"
        selection = f"p{source.page}" if source.page is not None else "all"
        base = f"bili-{identity.lower()}-{selection}"
        return f"{base}-{range_hash}"[:96]
    if source.kind == "subtitle":
        path_hash = hash_object({"path": source.normalized})[:16]
        return f"subtitle-{path_hash}-{range_hash}"
    path_hash = hash_object({"path": source.normalized})[:16]
    return f"local-{path_hash}-{range_hash}"


def _manifest_identity(manifest: JobManifest) -> str:
    return hash_object(_identity_payload(manifest.source, manifest.requested_range))


def _identity_payload(source: SourceDescriptor, time_range: TimeRange) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "source": source.model_dump(mode="json"),
        "requested_range": time_range.model_dump(mode="json"),
    }


def _load_manifest(path: Path) -> JobManifest:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return JobManifest.model_validate(json.load(handle))
    except FileNotFoundError as exc:
        raise JobNotFoundError(f"Job manifest does not exist: {path.parent.name}") from exc
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise StateError(f"Cannot load job manifest {path}: {exc}") from exc


def _recover_missing_bootstrap_state(
    files: JobFiles,
    manifest: JobManifest,
    *,
    config_hash: str,
) -> PipelineState:
    """Recover the narrow crash window after manifest but before initial state."""
    identity = _identity_payload(manifest.source, manifest.requested_range)
    if files.identity.exists():
        try:
            with files.identity.open("r", encoding="utf-8") as handle:
                stored_identity = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(
                "Job bootstrap is incomplete and job-identity.json is unreadable; "
                "refusing to replace audit data."
            ) from exc
        if hash_object(stored_identity) != hash_object(identity):
            raise StateError(
                "Job bootstrap identity conflicts with manifest; refusing automatic recovery."
            )
    else:
        atomic_write_json(files.identity, identity)

    state = new_state(manifest.job_id)
    attempt_id = start_stage(
        state,
        "created",
        input_hash=_manifest_identity(manifest),
        config_hash=config_hash,
        tool="lectureflow-bootstrap-recovery",
        tool_version=PIPELINE_VERSION,
    )
    finish_stage_success(
        state,
        "created",
        attempt_id,
        output_hash=hash_file(files.identity),
    )
    save_state(files.state, state)
    record_transition(
        files.events,
        state,
        "bootstrap_recovered",
        stage="created",
        reason="manifest_present_state_missing",
    )
    return state


def _validate_job_audit_files(
    files: JobFiles,
    manifest: JobManifest,
    state: PipelineState,
    *,
    expected_job_id: str,
) -> None:
    _validate_manifest_identity(files, manifest, expected_job_id=expected_job_id)
    if state.job_id != expected_job_id:
        raise StateError(
            "Job identity mismatch between directory, manifest, and pipeline state; "
            "refusing to use possibly cross-contaminated cache data."
        )
    _validate_created_audit_hash(files, state)


def _validate_manifest_identity(
    files: JobFiles,
    manifest: JobManifest,
    *,
    expected_job_id: str,
) -> None:
    if manifest.job_id != expected_job_id:
        raise StateError(
            "Job identity mismatch between directory and manifest; refusing to use possibly "
            "cross-contaminated cache data."
        )
    derived_job_id = _job_id(manifest.source, manifest.requested_range)
    if derived_job_id != expected_job_id:
        raise StateError(
            "Job directory does not match the deterministic source/range identity; refusing "
            "automatic recovery."
        )
    if not files.identity.is_file():
        raise StateError("Immutable job-identity.json is missing; refusing to claim a cache hit.")
    try:
        with files.identity.open("r", encoding="utf-8") as handle:
            stored_identity = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"Cannot read immutable job identity: {exc}") from exc
    expected_identity = _identity_payload(manifest.source, manifest.requested_range)
    if hash_object(stored_identity) != hash_object(expected_identity):
        raise StateError("Immutable job identity conflicts with manifest; refusing cache reuse.")


def _validate_created_audit_hash(files: JobFiles, state: PipelineState) -> None:
    created = _latest_success_input(state, "created")
    if created is None or created[2] != hash_file(files.identity):
        raise StateError(
            "Created-stage audit hash does not match job-identity.json; refusing cache reuse."
        )


def _latest_success_input(state: PipelineState, stage_name: str) -> tuple[str, str, str] | None:
    record = state.stages[stage_name]
    if record.status != StageStatus.SUCCEEDED:
        return None
    for attempt in reversed(record.attempts):
        if attempt.finished_at and attempt.output_hash and attempt.error is None:
            return attempt.input_hash, attempt.config_hash, attempt.output_hash
    return None


def _metadata_input(
    source: SourceDescriptor,
    time_range: TimeRange,
    fingerprint: dict[str, Any] | None,
    tool_name: str,
    tool_version: str | None,
) -> str:
    return hash_object(
        {
            "source": source.model_dump(mode="json"),
            "requested_range": time_range.model_dump(mode="json"),
            "source_fingerprint": fingerprint,
            "pipeline_version": PIPELINE_VERSION,
            "stage_schema": "metadata-1.0.0",
            "tool": tool_name,
            "tool_version": tool_version,
        }
    )


def _metadata_cache_valid(
    files: JobFiles,
    state: PipelineState,
    *,
    input_hash: str,
) -> bool:
    previous = _latest_success_input(state, "metadata_ready")
    if previous is None or not files.metadata.is_file():
        return False
    old_input, _old_config, old_output = previous
    # The metadata probe has no dependency on ASR, frame, render, or export settings.
    # Retaining the full AppConfig here made unrelated downstream changes re-probe a
    # signed Bilibili response and cascade into a needless media download.
    return old_input == input_hash and hash_file(files.metadata) == old_output


def _validate_requested_range(metadata: dict[str, Any], time_range: TimeRange) -> None:
    raw_duration = metadata.get("duration")
    if raw_duration is None and isinstance(metadata.get("format"), dict):
        raw_duration = metadata["format"].get("duration")
    if raw_duration in {None, "", "N/A"}:
        return
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError):
        return
    tolerance = 0.5
    if time_range.start >= duration + tolerance:
        raise CommandError(
            f"Requested start {time_range.start:g}s is beyond media duration {duration:g}s."
        )
    if time_range.end is not None and time_range.end > duration + tolerance:
        raise CommandError(
            f"Requested end {time_range.end:g}s is beyond media duration {duration:g}s."
        )


def prepare_job(
    source_value: str,
    *,
    config: AppConfig,
    start: float = 0.0,
    end: float | None = None,
    force_stage: str | None = None,
    offline: bool = False,
) -> PrepareResult:
    if force_stage not in {None, "metadata_ready"}:
        raise StateError("prepare can only force the 'metadata_ready' stage.")
    try:
        time_range = TimeRange(start=start, end=end)
    except ValidationError as exc:
        raise SourceError(f"Invalid requested time range: {exc.errors()[0]['msg']}") from exc
    source = identify_source(source_value)
    job_id = _job_id(source, time_range)
    if offline and source.kind == "bilibili":
        try:
            root = job_path(config.workspace_root, job_id, must_exist=True)
        except JobNotFoundError as exc:
            raise StateError(
                f"Offline metadata cache miss for job {job_id}; run once without --offline."
            ) from exc
    else:
        root = ensure_job_layout(config.workspace_root, job_id)
    files = JobFiles(root)
    config_hash = hash_object(config.model_dump(mode="json"))
    fingerprint = (
        fingerprint_local_file(Path(source.normalized)) if source.kind == "local" else None
    )
    now = utc_now()
    proposed = JobManifest(
        job_id=job_id,
        created_at=now,
        updated_at=now,
        source=source,
        requested_range=time_range,
        config_hash=config_hash,
        source_fingerprint=fingerprint,
    )

    with JobLock(files.lock):
        if files.manifest.exists():
            manifest = _load_manifest(files.manifest)
            if _manifest_identity(manifest) != _manifest_identity(proposed):
                raise StateError(
                    f"Job ID collision for {job_id}; refusing to overwrite a different source."
                )
            if files.state.exists():
                state = load_state(files.state)
            else:
                state = _recover_missing_bootstrap_state(
                    files,
                    manifest,
                    config_hash=config_hash,
                )
            _validate_job_audit_files(
                files,
                manifest,
                state,
                expected_job_id=job_id,
            )
            interrupted = mark_interrupted_running_stages(state)
            if interrupted:
                save_state(files.state, state)
                for stage in interrupted:
                    record_transition(files.events, state, "stage_interrupted", stage=stage)
            manifest.updated_at = now
            manifest.pipeline_version = PIPELINE_VERSION
            manifest.config_hash = config_hash
            manifest.source_fingerprint = fingerprint
        else:
            manifest = proposed
            atomic_write_json(files.identity, _identity_payload(source, time_range))
            atomic_write_json(files.manifest, manifest.model_dump(mode="json"))
            state = new_state(job_id)
            attempt_id = start_stage(
                state,
                "created",
                input_hash=_manifest_identity(manifest),
                config_hash=config_hash,
                tool="lectureflow",
                tool_version=PIPELINE_VERSION,
            )
            finish_stage_success(
                state,
                "created",
                attempt_id,
                output_hash=hash_file(files.identity),
            )
            save_state(files.state, state)
            record_transition(files.events, state, "stage_succeeded", stage="created")

        tool = "ffprobe" if source.kind == "local" else "yt-dlp"
        detected_tool_version = available_tool_version(tool)
        input_hash = _metadata_input(
            source,
            time_range,
            fingerprint,
            tool,
            detected_tool_version,
        )
        valid_cache = _metadata_cache_valid(
            files,
            state,
            input_hash=input_hash,
        )
        if offline and source.kind == "bilibili" and not valid_cache:
            previous = _latest_success_input(state, "metadata_ready")
            valid_cache = (
                previous is not None
                and files.metadata.is_file()
                and hash_file(files.metadata) == previous[2]
            )
            if not valid_cache:
                raise StateError(
                    "Offline metadata cache is missing, invalid, or incomplete; "
                    "run once without --offline."
                )
        if valid_cache and force_stage is None:
            manifest.updated_at = utc_now()
            atomic_write_json(files.manifest, manifest.model_dump(mode="json"))
            record_transition(files.events, state, "stage_cache_hit", stage="metadata_ready")
            return PrepareResult(
                job_id=job_id,
                job_dir=root,
                metadata_path=files.metadata,
                cache_hit=True,
                current_milestone=state.last_successful_milestone,
            )

        if state.stages["metadata_ready"].status in {
            StageStatus.SUCCEEDED,
            StageStatus.FAILED,
            StageStatus.SKIPPED,
        }:
            changed = invalidate_from(state, "metadata_ready")
            save_state(files.state, state)
            record_transition(
                files.events,
                state,
                "stages_invalidated",
                stage="metadata_ready",
                stages=changed,
                reason="forced" if force_stage else "input_or_output_changed",
            )

        attempt_id = start_stage(
            state,
            "metadata_ready",
            input_hash=input_hash,
            config_hash=config_hash,
            tool=tool,
            tool_version=detected_tool_version,
        )
        save_state(files.state, state)
        record_transition(files.events, state, "stage_started", stage="metadata_ready", tool=tool)
        try:
            if source.kind == "local":
                metadata, version = probe_local(source)
            else:
                metadata, version = probe_bilibili(source)
            _validate_requested_range(metadata, time_range)
            state.stages["metadata_ready"].attempts[-1].tool_version = version
            atomic_write_json(files.metadata, redact(metadata))
            output_hash = hash_file(files.metadata)
            manifest.metadata_path = str(files.metadata.relative_to(root))
            manifest.updated_at = utc_now()
            atomic_write_json(files.manifest, manifest.model_dump(mode="json"))
        except Exception as exc:
            finish_stage_failure(
                state,
                "metadata_ready",
                attempt_id,
                error=str(exc),
                recoverable=True,
            )
            manifest.updated_at = utc_now()
            atomic_write_json(files.manifest, manifest.model_dump(mode="json"))
            save_state(files.state, state)
            record_transition(
                files.events,
                state,
                "stage_failed",
                stage="metadata_ready",
                error=redact(str(exc)),
                recoverable=True,
            )
            safe_error = redact_text(str(exc))
            raise PrepareError(
                f"Job {job_id} was created, but metadata failed: {safe_error} "
                f"Resume with 'lectureflow resume {job_id}'."
            ) from exc

        finish_stage_success(state, "metadata_ready", attempt_id, output_hash=output_hash)
        save_state(files.state, state)
        try:
            record_transition(
                files.events,
                state,
                "stage_succeeded",
                stage="metadata_ready",
                output_hash=output_hash,
            )
        except Exception as exc:
            raise StateError(
                "Metadata stage succeeded and its state is durable, but appending the audit event "
                f"failed: {redact_text(str(exc))}. Rerun status before continuing."
            ) from exc

        return PrepareResult(
            job_id=job_id,
            job_dir=root,
            metadata_path=files.metadata,
            cache_hit=False,
            current_milestone=state.last_successful_milestone,
        )


def register_subtitle_job(
    subtitle_path: Path,
    *,
    subtitle_format: str,
    config: AppConfig,
) -> PrepareResult:
    try:
        resolved = subtitle_path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise SourceError(f"Local subtitle does not exist: {subtitle_path}") from exc
    if not resolved.is_file():
        raise SourceError(f"Local subtitle is not a regular file: {resolved}")
    source = SourceDescriptor(
        kind="subtitle",
        normalized=str(resolved),
        display_name=resolved.name,
        subtitle_format=subtitle_format,
    )
    time_range = TimeRange()
    job_id = _job_id(source, time_range)
    root = ensure_job_layout(config.workspace_root, job_id)
    files = JobFiles(root)
    fingerprint = fingerprint_local_file(resolved)
    config_hash = hash_object(config.model_dump(mode="json"))
    now = utc_now()
    proposed = JobManifest(
        job_id=job_id,
        created_at=now,
        updated_at=now,
        source=source,
        requested_range=time_range,
        config_hash=config_hash,
        source_fingerprint=fingerprint,
        metadata_path=str(files.metadata.relative_to(root)),
    )
    metadata = {
        "schema_version": "1.0.0",
        "lectureflow_source": "local_subtitle",
        "subtitle_format": subtitle_format,
        "display_name": resolved.name,
        "fingerprint": fingerprint,
    }
    input_hash = hash_object(
        {
            "source": source.model_dump(mode="json"),
            "fingerprint": fingerprint,
            "stage_schema": "subtitle-metadata-1.0.0",
        }
    )
    with JobLock(files.lock):
        if files.manifest.exists():
            manifest = _load_manifest(files.manifest)
            if _manifest_identity(manifest) != _manifest_identity(proposed):
                raise StateError(
                    f"Job ID collision for {job_id}; refusing to overwrite a different source."
                )
            if files.state.exists():
                state = load_state(files.state)
            else:
                state = _recover_missing_bootstrap_state(
                    files,
                    manifest,
                    config_hash=config_hash,
                )
            _validate_job_audit_files(files, manifest, state, expected_job_id=job_id)
            manifest.updated_at = now
            manifest.pipeline_version = PIPELINE_VERSION
            manifest.config_hash = config_hash
            manifest.source_fingerprint = fingerprint
            interrupted = mark_interrupted_running_stages(state)
            if interrupted:
                save_state(files.state, state)
                for stage in interrupted:
                    record_transition(files.events, state, "stage_interrupted", stage=stage)
            if _metadata_cache_valid(
                files,
                state,
                input_hash=input_hash,
            ):
                atomic_write_json(files.manifest, manifest.model_dump(mode="json"))
                record_transition(files.events, state, "stage_cache_hit", stage="metadata_ready")
                return PrepareResult(
                    job_id=job_id,
                    job_dir=root,
                    metadata_path=files.metadata,
                    cache_hit=True,
                    current_milestone=state.last_successful_milestone,
                )
            if state.stages["metadata_ready"].status in {
                StageStatus.SUCCEEDED,
                StageStatus.FAILED,
                StageStatus.SKIPPED,
            }:
                invalidate_from(state, "metadata_ready")
                save_state(files.state, state)
        else:
            manifest = proposed
            atomic_write_json(files.identity, _identity_payload(source, time_range))
            atomic_write_json(files.manifest, manifest.model_dump(mode="json"))
            state = new_state(job_id)
            created_attempt = start_stage(
                state,
                "created",
                input_hash=_manifest_identity(manifest),
                config_hash=config_hash,
                tool="lectureflow",
                tool_version=PIPELINE_VERSION,
            )
            finish_stage_success(
                state,
                "created",
                created_attempt,
                output_hash=hash_file(files.identity),
            )
            save_state(files.state, state)
            record_transition(files.events, state, "stage_succeeded", stage="created")
        attempt_id = start_stage(
            state,
            "metadata_ready",
            input_hash=input_hash,
            config_hash=config_hash,
            tool="lectureflow-local-subtitle",
            tool_version=PIPELINE_VERSION,
        )
        save_state(files.state, state)
        atomic_write_json(files.metadata, metadata)
        manifest.source_fingerprint = fingerprint
        manifest.updated_at = utc_now()
        atomic_write_json(files.manifest, manifest.model_dump(mode="json"))
        finish_stage_success(
            state,
            "metadata_ready",
            attempt_id,
            output_hash=hash_file(files.metadata),
        )
        save_state(files.state, state)
        record_transition(files.events, state, "stage_succeeded", stage="metadata_ready")
    return PrepareResult(
        job_id=job_id,
        job_dir=root,
        metadata_path=files.metadata,
        cache_hit=False,
        current_milestone="metadata_ready",
    )


def load_job_context(
    job_id: str,
    *,
    config: AppConfig,
) -> tuple[JobFiles, JobManifest, PipelineState]:
    root = job_path(config.workspace_root, job_id, must_exist=True)
    files = JobFiles(root)
    manifest = _load_manifest(files.manifest)
    state = load_state(files.state)
    _validate_job_audit_files(files, manifest, state, expected_job_id=job_id)
    return files, manifest, state


def job_status(job_id: str, *, config: AppConfig) -> dict[str, Any]:
    root = job_path(config.workspace_root, job_id, must_exist=True)
    files = JobFiles(root)
    manifest = _load_manifest(files.manifest)
    state = load_state(files.state)
    _validate_job_audit_files(files, manifest, state, expected_job_id=job_id)
    stages: dict[str, Any] = {}
    suspected_interruption: list[str] = []
    for name, record in state.stages.items():
        if record.status == StageStatus.RUNNING:
            suspected_interruption.append(name)
        latest = record.attempts[-1] if record.attempts else None
        stages[name] = {
            "status": record.status.value,
            "attempt_count": len(record.attempts),
            "latest_attempt": latest.model_dump(mode="json") if latest else None,
        }
    asr_manifest_path = root / "transcript/asr/asr-manifest.json"
    subtitle_result_path = root / "transcript/subtitle-result.json"
    subtitle_result: dict[str, Any] | None = None
    if asr_manifest_path.is_file():
        try:
            payload = json.loads(asr_manifest_path.read_text(encoding="utf-8"))
            transcript_success = _latest_success_input(state, "transcript_ready")
            if (
                transcript_success is not None
                and hash_file(asr_manifest_path) != transcript_success[2]
            ):
                subtitle_result = {
                    "status": "invalid_asr_manifest",
                    "recoverable": True,
                    "message": (
                        "Run lectureflow asr inspect. This release never overwrites raw/original; "
                        "automatic derived-artifact repair is not yet implemented."
                    ),
                }
            else:
                backend = str(payload.get("backend", "mlx-whisper"))
                source = backend.replace("-", "_")
                subtitle_result = {
                    "status": "transcript_ready",
                    "selected_sources": {"P1": source},
                    "part_ids": ["P1"],
                    "recoverable": True,
                    "message": (f"{payload.get('segment_count', 0)} local {backend} segments"),
                }
        except (OSError, json.JSONDecodeError, AttributeError):
            subtitle_result = {"status": "invalid_asr_manifest", "recoverable": True}
    elif subtitle_result_path.is_file():
        try:
            payload = json.loads(subtitle_result_path.read_text(encoding="utf-8"))
            transcript_success = _latest_success_input(state, "transcript_ready")
            if (
                transcript_success is not None
                and hash_file(subtitle_result_path) != transcript_success[2]
            ):
                subtitle_result = {
                    "status": "invalid_result_file",
                    "recoverable": True,
                    "message": "Run resume to rebuild the transcript result.",
                }
            else:
                subtitle_result = {
                    "status": payload.get("status"),
                    "selected_sources": payload.get("selected_sources", {}),
                    "part_ids": payload.get("part_ids", []),
                    "recoverable": payload.get("recoverable", True),
                    "message": payload.get("message"),
                }
        except (OSError, json.JSONDecodeError, AttributeError):
            subtitle_result = {"status": "invalid_result_file", "recoverable": True}
    media_result: dict[str, Any] | None = None
    if files.media_manifest.is_file():
        try:
            payload = json.loads(files.media_manifest.read_text(encoding="utf-8"))
            media_result = {
                "status": "media_ready",
                "source_kind": payload.get("source_kind"),
                "duration": payload.get("duration"),
                "media_path": payload.get("media_path"),
            }
        except (OSError, json.JSONDecodeError, AttributeError):
            media_result = {"status": "invalid_media_manifest", "recoverable": True}
    frame_result: dict[str, Any] | None = None
    if files.frames_manifest.is_file():
        try:
            payload = json.loads(files.frames_manifest.read_text(encoding="utf-8"))
            stored = payload.get("result", {})
            frame_result = {
                "status": stored.get("status"),
                "candidate_count": stored.get("candidate_count"),
                "final_count": stored.get("final_count"),
                "coverage_path": stored.get("coverage_path"),
            }
        except (OSError, json.JSONDecodeError, AttributeError):
            frame_result = {"status": "invalid_frame_manifest", "recoverable": True}
    return {
        "schema_version": state.schema_version,
        "job_id": job_id,
        "source": manifest.source.model_dump(mode="json"),
        "requested_range": manifest.requested_range.model_dump(mode="json"),
        "overall_status": state.overall_status,
        "current_milestone": state.current_milestone,
        "last_successful_milestone": state.last_successful_milestone,
        "revision": state.revision,
        "updated_at": state.updated_at,
        "suspected_interruption": suspected_interruption,
        "stages": stages,
        "subtitle_result": subtitle_result,
        "media_result": media_result,
        "frame_result": frame_result,
        "resume_command": f"lectureflow resume {job_id}",
    }


def resume_job(job_id: str, *, config: AppConfig) -> PrepareResult:
    root = job_path(config.workspace_root, job_id, must_exist=True)
    files = JobFiles(root)
    manifest = _load_manifest(files.manifest)
    _validate_manifest_identity(files, manifest, expected_job_id=job_id)
    if files.state.exists():
        state = load_state(files.state)
        _validate_job_audit_files(files, manifest, state, expected_job_id=job_id)
    return prepare_job(
        manifest.source.normalized,
        config=config,
        start=manifest.requested_range.start,
        end=manifest.requested_range.end,
    )
