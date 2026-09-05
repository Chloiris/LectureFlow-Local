from __future__ import annotations

from pathlib import Path

from lectureflow.config import AppConfig
from lectureflow.constants import MILESTONES
from lectureflow.hashing import hash_file, hash_object
from lectureflow.pipeline import load_job_context
from lectureflow.schemas.state import StageStatus
from lectureflow.state import (
    JobLock,
    finish_stage_success,
    record_transition,
    save_state,
    start_stage,
)


def record_validated_artifact_stage(
    job_id: str,
    *,
    config: AppConfig,
    stage: str,
    artifact: Path,
    tool: str,
) -> None:
    files, _, state = load_job_context(job_id, config=config)
    artifact_path = files.root / artifact
    digest = hash_file(artifact_path)
    with JobLock(files.lock):
        previous_milestone = state.last_successful_milestone
        record = state.stages[stage]
        latest = record.attempts[-1] if record.attempts else None
        if (
            record.status == StageStatus.SUCCEEDED
            and latest is not None
            and latest.output_hash == digest
        ):
            if previous_milestone is None or MILESTONES.index(stage) > MILESTONES.index(
                previous_milestone
            ):
                state.current_milestone = stage
                state.last_successful_milestone = stage
                save_state(files.state, state)
            record_transition(files.events, state, "stage_cache_hit", stage=stage)
            return
        attempt = start_stage(
            state,
            stage,
            input_hash=hash_object({"artifact": str(artifact), "sha256": digest}),
            config_hash=hash_object({"schema": "m3b-validated-artifact-1.0"}),
            tool=tool,
            tool_version="1.0",
        )
        finish_stage_success(state, stage, attempt, output_hash=digest)
        if previous_milestone is not None and MILESTONES.index(
            previous_milestone
        ) > MILESTONES.index(stage):
            state.current_milestone = previous_milestone
            state.last_successful_milestone = previous_milestone
        save_state(files.state, state)
        record_transition(files.events, state, "stage_succeeded", stage=stage)
