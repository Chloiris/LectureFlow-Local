from __future__ import annotations

SCHEMA_VERSION = "1.0.0"
PIPELINE_VERSION = "0.3.0"

MILESTONES: tuple[str, ...] = (
    "created",
    "metadata_ready",
    "subtitle_ready",
    "audio_ready",
    "transcript_ready",
    "frames_ready",
    "packets_ready",
    "analysis_ready",
    "evidence_ready",
    "notes_ready",
    "obsidian_ready",
    "web_ready",
    "complete",
)

JOB_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{2,95}$"
