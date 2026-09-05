from __future__ import annotations

import fcntl
import json
import os
import uuid
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from lectureflow.atomic import append_jsonl, atomic_write_json
from lectureflow.constants import MILESTONES
from lectureflow.errors import StateError
from lectureflow.schemas.state import PipelineState, StageAttempt, StageStatus
from lectureflow.security import redact_text


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_state(job_id: str) -> PipelineState:
    return PipelineState(job_id=job_id, updated_at=utc_now())


def load_state(path: Path) -> PipelineState:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return PipelineState.model_validate(json.load(handle))
    except FileNotFoundError as exc:
        raise StateError(f"Pipeline state does not exist: {path}") from exc
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise StateError(f"Cannot load pipeline state {path}: {exc}") from exc


def save_state(path: Path, state: PipelineState) -> None:
    state.revision += 1
    state.updated_at = utc_now()
    atomic_write_json(path, state.model_dump(mode="json"))


def _record_event(events_path: Path, state: PipelineState, event: str, **fields: object) -> None:
    append_jsonl(
        events_path,
        {
            "schema_version": state.schema_version,
            "timestamp": utc_now(),
            "job_id": state.job_id,
            "revision": state.revision,
            "event": event,
            **fields,
        },
    )


def start_stage(
    state: PipelineState,
    stage_name: str,
    *,
    input_hash: str,
    config_hash: str,
    tool: str,
    tool_version: str | None,
) -> str:
    if stage_name not in state.stages:
        raise StateError(f"Unknown pipeline stage: {stage_name}")
    record = state.stages[stage_name]
    if record.status == StageStatus.RUNNING:
        raise StateError(f"Stage is already running: {stage_name}")
    attempt_id = uuid.uuid4().hex
    record.attempts.append(
        StageAttempt(
            attempt_id=attempt_id,
            started_at=utc_now(),
            input_hash=input_hash,
            config_hash=config_hash,
            tool=tool,
            tool_version=tool_version,
        )
    )
    record.status = StageStatus.RUNNING
    state.overall_status = "active"
    state.current_milestone = stage_name
    return attempt_id


def finish_stage_success(
    state: PipelineState, stage_name: str, attempt_id: str, *, output_hash: str
) -> None:
    record = state.stages[stage_name]
    attempt = _find_attempt(record.attempts, attempt_id)
    if record.status != StageStatus.RUNNING or attempt.finished_at is not None:
        raise StateError(f"Stage is not running: {stage_name}")
    attempt.finished_at = utc_now()
    attempt.output_hash = output_hash
    attempt.error = None
    record.status = StageStatus.SUCCEEDED
    state.current_milestone = stage_name
    state.last_successful_milestone = stage_name
    state.overall_status = "complete" if stage_name == "complete" else "active"


def restore_stage_cache_hit(
    state: PipelineState,
    stage_name: str,
    *,
    input_hash: str,
    output_hash: str,
) -> bool:
    """Restore an invalidated stage when its audited immutable artifacts still match."""
    record = state.stages[stage_name]
    matching = next(
        (
            attempt
            for attempt in reversed(record.attempts)
            if attempt.finished_at is not None
            and attempt.error is None
            and attempt.input_hash == input_hash
            and attempt.output_hash == output_hash
        ),
        None,
    )
    if matching is None:
        return False
    latest = record.attempts[-1] if record.attempts else None
    if record.status == StageStatus.SUCCEEDED and latest is matching:
        return False
    now = utc_now()
    record.attempts.append(
        StageAttempt(
            attempt_id=uuid.uuid4().hex,
            started_at=now,
            finished_at=now,
            input_hash=input_hash,
            output_hash=output_hash,
            config_hash=matching.config_hash,
            tool=f"{matching.tool}-cache-restore",
            tool_version=matching.tool_version,
            recoverable=True,
        )
    )
    record.status = StageStatus.SUCCEEDED
    state.current_milestone = stage_name
    state.last_successful_milestone = stage_name
    state.overall_status = "active"
    return True


def finish_stage_failure(
    state: PipelineState,
    stage_name: str,
    attempt_id: str,
    *,
    error: str,
    recoverable: bool,
) -> None:
    record = state.stages[stage_name]
    attempt = _find_attempt(record.attempts, attempt_id)
    if record.status != StageStatus.RUNNING or attempt.finished_at is not None:
        raise StateError(f"Stage is not running: {stage_name}")
    attempt.finished_at = utc_now()
    attempt.error = redact_text(error)[:4000]
    attempt.recoverable = recoverable
    record.status = StageStatus.FAILED
    state.current_milestone = stage_name
    state.overall_status = "failed"


def mark_interrupted_running_stages(state: PipelineState) -> list[str]:
    interrupted: list[str] = []
    for stage_name, record in state.stages.items():
        if record.status != StageStatus.RUNNING:
            continue
        attempt = record.attempts[-1]
        attempt.finished_at = utc_now()
        attempt.error = "Previous process ended while this stage was running."
        attempt.recoverable = True
        record.status = StageStatus.FAILED
        interrupted.append(stage_name)
    if interrupted:
        state.overall_status = "failed"
        state.current_milestone = interrupted[-1]
    return interrupted


def invalidate_from(state: PipelineState, stage_name: str) -> list[str]:
    try:
        start_index = MILESTONES.index(stage_name)
    except ValueError as exc:
        raise StateError(f"Unknown pipeline stage: {stage_name}") from exc
    changed: list[str] = []
    for name in MILESTONES[start_index:]:
        record = state.stages[name]
        if record.status in {StageStatus.SUCCEEDED, StageStatus.SKIPPED, StageStatus.FAILED}:
            record.status = StageStatus.INVALIDATED
            changed.append(name)
        elif record.status == StageStatus.RUNNING:
            raise StateError(f"Cannot invalidate running stage: {name}")
    previous = [
        name
        for name in MILESTONES[:start_index]
        if state.stages[name].status == StageStatus.SUCCEEDED
    ]
    state.last_successful_milestone = previous[-1] if previous else None
    state.current_milestone = state.last_successful_milestone
    state.overall_status = "active"
    return changed


def record_transition(
    events_path: Path, state: PipelineState, event: str, *, stage: str, **fields: object
) -> None:
    _record_event(events_path, state, event, stage=stage, **fields)


def _find_attempt(attempts: list[StageAttempt], attempt_id: str) -> StageAttempt:
    for attempt in reversed(attempts):
        if attempt.attempt_id == attempt_id:
            return attempt
    raise StateError(f"Unknown stage attempt: {attempt_id}")


class JobLock(AbstractContextManager["JobLock"]):
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def __enter__(self) -> JobLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self._fd)
            self._fd = None
            raise StateError(
                f"Another LectureFlow process is using job {self.path.parent.name}."
            ) from exc
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
