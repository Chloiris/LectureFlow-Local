from __future__ import annotations

import json
from pathlib import Path

import pytest

import lectureflow.pipeline as pipeline_module
from lectureflow.config import AppConfig, load_config
from lectureflow.errors import CommandError, PrepareError, SourceError, StateError
from lectureflow.hashing import hash_file
from lectureflow.pipeline import job_status, prepare_job, resume_job


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    base = load_config(cwd=tmp_path)
    return base.model_copy(update={"workspace_root": tmp_path / "工作 空间"})


def test_prepare_is_idempotent_and_detects_source_change(
    tmp_path: Path,
    config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "课程 视频.mp4"
    source.write_bytes(b"first")
    calls: list[str] = []

    def fake_probe(descriptor: object) -> tuple[dict[str, object], str]:
        calls.append("probe")
        return {"format": {"duration": "5.0"}, "streams": []}, "ffprobe test"

    monkeypatch.setattr(pipeline_module, "probe_local", fake_probe)
    first = prepare_job(str(source), config=config, start=0, end=5)
    second = prepare_job(str(source), config=config, start=0, end=5)
    assert first.job_id == second.job_id
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert calls == ["probe"]
    state_payload = json.loads((first.job_dir / "pipeline-state.json").read_text())
    created_hash = state_payload["stages"]["created"]["attempts"][0]["output_hash"]
    assert created_hash == hash_file(first.job_dir / "job-identity.json")

    source.write_bytes(b"second version")
    third = prepare_job(str(source), config=config, start=0, end=5)
    assert third.job_id == first.job_id
    assert third.cache_hit is False
    assert calls == ["probe", "probe"]
    report = job_status(first.job_id, config=config)
    assert report["stages"]["metadata_ready"]["attempt_count"] == 2
    assert report["last_successful_milestone"] == "metadata_ready"


def test_failed_metadata_is_auditable_and_resumable(
    tmp_path: Path,
    config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "broken.mp4"
    source.write_bytes(b"fixture")

    def broken_probe(descriptor: object) -> tuple[dict[str, object], str]:
        raise CommandError("ffprobe unavailable; token=must-not-leak")

    monkeypatch.setattr(pipeline_module, "probe_local", broken_probe)
    with pytest.raises(PrepareError) as captured:
        prepare_job(str(source), config=config)
    assert "must-not-leak" not in str(captured.value)

    job_dirs = [item for item in config.workspace_root.iterdir() if item.is_dir()]
    assert len(job_dirs) == 1
    job_id = job_dirs[0].name
    report = job_status(job_id, config=config)
    assert report["overall_status"] == "failed"
    assert report["last_successful_milestone"] == "created"
    assert "must-not-leak" not in json.dumps(report)

    monkeypatch.setattr(
        pipeline_module,
        "probe_local",
        lambda descriptor: ({"format": {"duration": "1"}}, "ffprobe test"),
    )
    resumed = resume_job(job_id, config=config)
    assert resumed.current_milestone == "metadata_ready"
    assert resumed.cache_hit is False
    assert job_status(job_id, config=config)["overall_status"] == "active"


def test_unrelated_downstream_config_does_not_invalidate_metadata(
    tmp_path: Path,
    config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "stable.mp4"
    source.write_bytes(b"fixture")
    calls: list[str] = []

    def fake_probe(descriptor: object) -> tuple[dict[str, object], str]:
        calls.append("probe")
        return {"format": {"duration": "5.0"}, "streams": []}, "ffprobe test"

    monkeypatch.setattr(pipeline_module, "probe_local", fake_probe)
    first = prepare_job(str(source), config=config, start=0, end=5)
    changed = config.model_copy(deep=True)
    changed.frames.periodic_interval_seconds = 3
    second = prepare_job(str(source), config=changed, start=0, end=5)

    assert first.job_id == second.job_id
    assert second.cache_hit is True
    assert calls == ["probe"]


def test_force_metadata_stage_does_not_change_job_identity(
    tmp_path: Path,
    config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "force.mp4"
    source.write_bytes(b"fixture")
    monkeypatch.setattr(
        pipeline_module,
        "probe_local",
        lambda descriptor: ({"format": {"duration": "1"}}, "ffprobe test"),
    )
    first = prepare_job(str(source), config=config)
    forced = prepare_job(str(source), config=config, force_stage="metadata_ready")
    assert forced.job_id == first.job_id
    assert forced.cache_hit is False
    report = job_status(first.job_id, config=config)
    assert report["stages"]["metadata_ready"]["attempt_count"] == 2


def test_prepare_rejects_range_beyond_known_duration(
    tmp_path: Path,
    config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "short.mp4"
    source.write_bytes(b"fixture")
    monkeypatch.setattr(
        pipeline_module,
        "probe_local",
        lambda descriptor: ({"format": {"duration": "10"}}, "ffprobe test"),
    )
    with pytest.raises(PrepareError, match="beyond media duration"):
        prepare_job(str(source), config=config, start=0, end=30)


def test_prepare_rejects_inverted_range_without_traceback(
    tmp_path: Path, config: AppConfig
) -> None:
    source = tmp_path / "range.mp4"
    source.write_bytes(b"fixture")
    with pytest.raises(SourceError, match="Invalid requested time range"):
        prepare_job(str(source), config=config, start=2, end=1)


def test_prepare_recovers_bootstrap_crash_between_manifest_and_state(
    tmp_path: Path,
    config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "bootstrap.mp4"
    source.write_bytes(b"fixture")
    real_new_state = pipeline_module.new_state

    def crash_after_manifest(job_id: str) -> object:
        raise RuntimeError("simulated power loss")

    monkeypatch.setattr(pipeline_module, "new_state", crash_after_manifest)
    with pytest.raises(RuntimeError, match="simulated power loss"):
        prepare_job(str(source), config=config, start=0, end=2)

    job_dirs = [path for path in config.workspace_root.iterdir() if path.is_dir()]
    assert len(job_dirs) == 1
    assert (job_dirs[0] / "manifest.json").exists()
    assert not (job_dirs[0] / "pipeline-state.json").exists()

    monkeypatch.setattr(pipeline_module, "new_state", real_new_state)
    monkeypatch.setattr(
        pipeline_module,
        "probe_local",
        lambda descriptor: ({"format": {"duration": "2"}}, "ffprobe test"),
    )
    recovered = prepare_job(str(source), config=config, start=0, end=2)
    report = job_status(recovered.job_id, config=config)
    assert report["last_successful_milestone"] == "metadata_ready"
    events = (recovered.job_dir / "logs/events.jsonl").read_text(encoding="utf-8")
    assert "bootstrap_recovered" in events


@pytest.mark.parametrize("tamper", ["state_job_id", "missing_identity", "identity_content"])
def test_prepare_refuses_cross_contaminated_or_missing_identity(
    tmp_path: Path,
    config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    source = tmp_path / "audit.mp4"
    source.write_bytes(b"fixture")
    monkeypatch.setattr(
        pipeline_module,
        "probe_local",
        lambda descriptor: ({"format": {"duration": "2"}}, "ffprobe test"),
    )
    result = prepare_job(str(source), config=config, start=0, end=2)
    if tamper == "state_job_id":
        path = result.job_dir / "pipeline-state.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["job_id"] = "local-different-job"
        path.write_text(json.dumps(payload), encoding="utf-8")
    elif tamper == "missing_identity":
        (result.job_dir / "job-identity.json").unlink()
    else:
        path = result.job_dir / "job-identity.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["requested_range"]["end"] = 1
        path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(StateError):
        prepare_job(str(source), config=config, start=0, end=2)


def test_success_event_failure_does_not_reclassify_success_as_stage_failure(
    tmp_path: Path,
    config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "event.mp4"
    source.write_bytes(b"fixture")
    monkeypatch.setattr(
        pipeline_module,
        "probe_local",
        lambda descriptor: ({"format": {"duration": "2"}}, "ffprobe test"),
    )
    real_record = pipeline_module.record_transition

    def fail_success_event(*args: object, **kwargs: object) -> None:
        if args[2] == "stage_succeeded" and kwargs.get("stage") == "metadata_ready":
            raise OSError("simulated event log failure")
        real_record(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "record_transition", fail_success_event)
    with pytest.raises(StateError, match="state is durable"):
        prepare_job(str(source), config=config, start=0, end=2)
    job_dir = next(path for path in config.workspace_root.iterdir() if path.is_dir())
    state = json.loads((job_dir / "pipeline-state.json").read_text(encoding="utf-8"))
    assert state["stages"]["metadata_ready"]["status"] == "succeeded"


@pytest.mark.parametrize(("start", "end"), [(float("inf"), None), (0, float("inf"))])
def test_prepare_rejects_non_finite_range(
    tmp_path: Path,
    config: AppConfig,
    start: float,
    end: float | None,
) -> None:
    source = tmp_path / "finite.mp4"
    source.write_bytes(b"fixture")
    with pytest.raises(SourceError, match="Invalid requested time range"):
        prepare_job(str(source), config=config, start=start, end=end)


def test_tool_version_change_invalidates_metadata_cache(
    tmp_path: Path,
    config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "versioned.mp4"
    source.write_bytes(b"fixture")
    versions = iter(("ffprobe 1", "ffprobe 2"))
    monkeypatch.setattr(
        pipeline_module,
        "available_tool_version",
        lambda name: next(versions),
    )
    calls: list[str] = []

    def fake_probe(descriptor: object) -> tuple[dict[str, object], str]:
        calls.append("probe")
        return {"format": {"duration": "2"}}, "probe-reported-version"

    monkeypatch.setattr(pipeline_module, "probe_local", fake_probe)
    first = prepare_job(str(source), config=config, start=0, end=2)
    second = prepare_job(str(source), config=config, start=0, end=2)
    assert first.cache_hit is False
    assert second.cache_hit is False
    assert calls == ["probe", "probe"]


def test_resume_refuses_tampered_manifest_instead_of_creating_another_job(
    tmp_path: Path,
    config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_source = tmp_path / "first.mp4"
    second_source = tmp_path / "second.mp4"
    first_source.write_bytes(b"first")
    second_source.write_bytes(b"second")
    monkeypatch.setattr(
        pipeline_module,
        "probe_local",
        lambda descriptor: ({"format": {"duration": "2"}}, "ffprobe test"),
    )
    result = prepare_job(str(first_source), config=config, start=0, end=2)
    manifest_path = result.job_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source"]["normalized"] = str(second_source.resolve())
    manifest["source"]["display_name"] = second_source.name
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(StateError, match="identity"):
        resume_job(result.job_id, config=config)
    assert len([path for path in config.workspace_root.iterdir() if path.is_dir()]) == 1


def test_resume_rejects_joint_identity_tamper_during_bootstrap_window(
    tmp_path: Path,
    config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_source = tmp_path / "bootstrap-first.mp4"
    second_source = tmp_path / "bootstrap-second.mp4"
    first_source.write_bytes(b"first")
    second_source.write_bytes(b"second")

    def simulate_power_loss(job_id: str) -> object:
        raise RuntimeError("simulated power loss")

    monkeypatch.setattr(pipeline_module, "new_state", simulate_power_loss)
    with pytest.raises(RuntimeError, match="simulated power loss"):
        prepare_job(str(first_source), config=config, start=0, end=2)
    job_dir = next(path for path in config.workspace_root.iterdir() if path.is_dir())
    manifest_path = job_dir / "manifest.json"
    identity_path = job_dir / "job-identity.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    for payload in (manifest, identity):
        payload["source"]["normalized"] = str(second_source.resolve())
        payload["source"]["display_name"] = second_source.name
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    identity_path.write_text(json.dumps(identity), encoding="utf-8")

    with pytest.raises(StateError, match="deterministic source/range identity"):
        resume_job(job_dir.name, config=config)
    assert len([path for path in config.workspace_root.iterdir() if path.is_dir()]) == 1
