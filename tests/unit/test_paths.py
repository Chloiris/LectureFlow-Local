from __future__ import annotations

from pathlib import Path

import pytest

from lectureflow.errors import StateError
from lectureflow.paths import ensure_job_layout, job_path, validate_job_id


@pytest.mark.parametrize("job_id", ["../escape", "/absolute", "UPPER", "ab", "with space"])
def test_reject_unsafe_job_id(job_id: str) -> None:
    with pytest.raises(StateError):
        validate_job_id(job_id)


def test_job_layout_supports_unicode_parent(tmp_path: Path) -> None:
    workspace = tmp_path / "中文 工作空间"
    root = ensure_job_layout(workspace, "local-safe-job")
    assert (root / "frames/contact-sheets").is_dir()
    assert job_path(workspace, "local-safe-job", must_exist=True) == root


def test_reject_job_directory_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspaces"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "local-safe-job").symlink_to(outside, target_is_directory=True)
    with pytest.raises(StateError, match="symbolic link"):
        job_path(workspace, "local-safe-job")


def test_reject_internal_job_directory_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspaces"
    root = workspace / "local-safe-job"
    outside = tmp_path / "outside"
    root.mkdir(parents=True)
    outside.mkdir()
    (root / "raw").symlink_to(outside, target_is_directory=True)
    with pytest.raises(StateError, match="component cannot be a symbolic link"):
        ensure_job_layout(workspace, "local-safe-job")
