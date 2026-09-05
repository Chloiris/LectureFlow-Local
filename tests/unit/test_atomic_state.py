from __future__ import annotations

import json
from pathlib import Path

import pytest

import lectureflow.atomic as atomic_module
from lectureflow.atomic import atomic_write_json
from lectureflow.errors import StateError
from lectureflow.schemas.state import StageStatus
from lectureflow.state import (
    finish_stage_failure,
    finish_stage_success,
    invalidate_from,
    mark_interrupted_running_stages,
    new_state,
    restore_stage_cache_hit,
    start_stage,
)


def test_atomic_write_keeps_previous_file_if_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    atomic_write_json(target, {"revision": 1})

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(atomic_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated crash"):
        atomic_write_json(target, {"revision": 2})
    assert json.loads(target.read_text()) == {"revision": 1}
    assert list(tmp_path.glob(".state.json.*")) == []


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_atomic_json_rejects_non_finite_numbers(tmp_path: Path, value: float) -> None:
    with pytest.raises(ValueError):
        atomic_write_json(tmp_path / "invalid.json", {"value": value})


def test_state_failure_preserves_last_successful_milestone() -> None:
    state = new_state("local-abc")
    created = start_stage(
        state,
        "created",
        input_hash="in-created",
        config_hash="config",
        tool="lectureflow",
        tool_version="test",
    )
    finish_stage_success(state, "created", created, output_hash="out-created")
    metadata = start_stage(
        state,
        "metadata_ready",
        input_hash="in-metadata",
        config_hash="config",
        tool="ffprobe",
        tool_version="test",
    )
    finish_stage_failure(
        state,
        "metadata_ready",
        metadata,
        error="Cookie=secret should be hidden",
        recoverable=True,
    )
    assert state.last_successful_milestone == "created"
    assert state.stages["metadata_ready"].status == StageStatus.FAILED
    assert "secret" not in (state.stages["metadata_ready"].attempts[-1].error or "")


def test_interrupted_stage_can_be_marked_recoverable() -> None:
    state = new_state("local-abc")
    start_stage(
        state,
        "metadata_ready",
        input_hash="input",
        config_hash="config",
        tool="ffprobe",
        tool_version="test",
    )
    assert mark_interrupted_running_stages(state) == ["metadata_ready"]
    attempt = state.stages["metadata_ready"].attempts[-1]
    assert attempt.finished_at is not None
    assert attempt.recoverable is True
    assert state.stages["metadata_ready"].status == StageStatus.FAILED


def test_invalidate_stage_and_all_successful_descendants() -> None:
    state = new_state("local-abc")
    for stage in ("created", "metadata_ready", "subtitle_ready"):
        attempt = start_stage(
            state,
            stage,
            input_hash=stage,
            config_hash="config",
            tool="test",
            tool_version="test",
        )
        finish_stage_success(state, stage, attempt, output_hash=stage)
    changed = invalidate_from(state, "metadata_ready")
    assert changed == ["metadata_ready", "subtitle_ready"]
    assert state.last_successful_milestone == "created"


def test_matching_audited_cache_restores_invalidated_stage() -> None:
    state = new_state("local-abc")
    attempt = start_stage(
        state,
        "audio_ready",
        input_hash="audio-input",
        config_hash="config",
        tool="ffmpeg",
        tool_version="test",
    )
    finish_stage_success(state, "audio_ready", attempt, output_hash="audio-output")
    invalidate_from(state, "audio_ready")

    assert restore_stage_cache_hit(
        state,
        "audio_ready",
        input_hash="audio-input",
        output_hash="audio-output",
    )
    assert state.stages["audio_ready"].status == StageStatus.SUCCEEDED
    assert state.last_successful_milestone == "audio_ready"
    assert not restore_stage_cache_hit(
        state,
        "audio_ready",
        input_hash="audio-input",
        output_hash="audio-output",
    )


def test_unknown_major_pipeline_schema_is_rejected(tmp_path: Path) -> None:
    state = new_state("local-abc")
    payload = state.model_dump(mode="json")
    payload["schema_version"] = "2.0.0"
    path = tmp_path / "pipeline-state.json"
    atomic_write_json(path, payload)
    from lectureflow.state import load_state

    with pytest.raises(StateError, match="unsupported pipeline-state schema"):
        load_state(path)
