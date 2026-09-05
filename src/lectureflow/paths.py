from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from lectureflow.constants import JOB_ID_PATTERN
from lectureflow.errors import JobNotFoundError, StateError

_JOB_ID = re.compile(JOB_ID_PATTERN)

JOB_DIRECTORIES: tuple[str, ...] = (
    "raw/metadata",
    "raw/subtitles",
    "raw/comments",
    "media/video",
    "media/audio",
    "transcript",
    "frames/candidates",
    "frames/originals",
    "frames/contact-sheets",
    "packets",
    "analyses",
    "evidence",
    "notes",
    "obsidian",
    "web",
    "reports",
    "logs",
)


def validate_job_id(job_id: str) -> str:
    if not _JOB_ID.fullmatch(job_id):
        raise StateError(
            "Invalid job ID. Expected 3-96 lowercase ASCII letters, digits, or hyphens."
        )
    return job_id


def _within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def job_path(workspace_root: Path, job_id: str, *, must_exist: bool = False) -> Path:
    validate_job_id(job_id)
    root = workspace_root.expanduser().resolve()
    candidate = root / job_id
    if candidate.is_symlink():
        raise StateError(f"Job directory cannot be a symbolic link: {candidate}")
    resolved = candidate.resolve(strict=False)
    if not _within(resolved, root):
        raise StateError("Job path escapes the configured workspace root.")
    if must_exist and (not resolved.exists() or not resolved.is_dir()):
        raise JobNotFoundError(f"Job not found: {job_id}")
    return resolved


def ensure_job_layout(workspace_root: Path, job_id: str) -> Path:
    root = workspace_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    root_path = job_path(root, job_id)
    root_path.mkdir(mode=0o700, exist_ok=True)
    os.chmod(root_path, 0o700)
    for relative in JOB_DIRECTORIES:
        current = root_path
        for component in Path(relative).parts:
            current = current / component
            if current.is_symlink():
                raise StateError(f"Job directory component cannot be a symbolic link: {current}")
            current.mkdir(exist_ok=True)
            if not current.is_dir():
                raise StateError(f"Expected a job directory but found a file: {current}")
            os.chmod(current, 0o700)
    return root_path


@dataclass(frozen=True, slots=True)
class JobFiles:
    root: Path

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"

    @property
    def state(self) -> Path:
        return self.root / "pipeline-state.json"

    @property
    def identity(self) -> Path:
        return self.root / "job-identity.json"

    @property
    def events(self) -> Path:
        return self.root / "logs/events.jsonl"

    @property
    def lock(self) -> Path:
        return self.root / ".job.lock"

    @property
    def metadata(self) -> Path:
        return self.root / "raw/metadata/metadata.json"

    @property
    def media_manifest(self) -> Path:
        return self.root / "media/media-manifest.json"

    @property
    def frames_manifest(self) -> Path:
        return self.root / "frames/frame-artifact-manifest.json"

    @property
    def frames_jsonl(self) -> Path:
        return self.root / "frames/frames.jsonl"

    @property
    def frame_candidates_jsonl(self) -> Path:
        return self.root / "frames/frame-candidates.jsonl"

    @property
    def frame_coverage(self) -> Path:
        return self.root / "reports/frame-coverage-report.json"
