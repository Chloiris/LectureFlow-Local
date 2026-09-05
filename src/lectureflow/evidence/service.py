from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from lectureflow.errors import StateError
from lectureflow.schemas.multimodal import EvidenceRecord


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"Cannot read evidence audit file {path.name}: {error}") from error


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"Cannot read evidence JSONL {path.name}: {error}") from error


def validate_evidence(root: Path) -> dict[str, Any]:
    packet = _load_json(root / "packets/P0001/packet.json")
    transcript_ids = set(packet["transcript_ids"])
    frame_ids = set(packet["frame_ids"])
    raw_records = _load_jsonl(root / "evidence/evidence-ledger.jsonl")
    if not raw_records:
        raise StateError("Evidence ledger is empty.")
    records = []
    for raw in raw_records:
        try:
            record = EvidenceRecord.model_validate(raw)
        except ValidationError as error:
            raise StateError(f"Invalid evidence record: {error}") from error
        if not set(record.transcript_ids) <= transcript_ids:
            raise StateError(f"Evidence {record.evidence_id} cites unknown transcript IDs.")
        if not set(record.frame_ids) <= frame_ids:
            raise StateError(f"Evidence {record.evidence_id} cites unknown frame IDs.")
        records.append(record)
    coverage = _load_json(root / "evidence/coverage.json")
    required = {
        "informational_ranges",
        "excluded_ranges",
        "covered_ranges",
        "uncovered_ranges",
        "max_uncovered_gap_seconds",
        "speech_evidence_coverage_ratio",
        "visual_evidence_coverage_ratio",
        "joint_evidence_coverage_ratio",
    }
    if not required <= set(coverage):
        raise StateError("Evidence coverage report is incomplete.")
    return {
        "schema_version": "1.0",
        "valid": True,
        "evidence_count": len(records),
        "coverage": coverage,
    }
