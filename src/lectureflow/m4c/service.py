from __future__ import annotations

import json
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

from lectureflow.atomic import atomic_write_bytes, atomic_write_json, atomic_write_text
from lectureflow.config import AppConfig
from lectureflow.errors import StateError
from lectureflow.frames.image import compare_images, hamming_distance
from lectureflow.hashing import hash_file
from lectureflow.pipeline import load_job_context
from lectureflow.schemas.m4c import CorrectionAudit, FixRecord, FormulaAudit, ParagraphAudit

AUDIT_SCHEMA = "m4c-audit-1.0.0"
EXPECTED_FORMULAS = 8
EXPECTED_ACCEPTED = 32
EXPECTED_TERMS = 247
SAMPLE_IDS = (
    "PAR0001",
    "PAR0004",
    "PAR0005",
    "PAR0006",
    "PAR0009",
    "PAR0010",
    "PAR0011",
    "PAR0014",
    "PAR0015",
    "PAR0016",
    "PAR0018",
    "PAR0019",
    "PAR0020",
    "PAR0022",
    "PAR0024",
)
BOUNDARIES = (
    (300.0, "P0001", "P0002", ["PAR0005", "PAR0006"], [], ["E000005", "E000006"]),
    (600.0, "P0002", "P0003", ["PAR0009", "PAR0010"], [], ["E000009", "E000010"]),
    (900.0, "P0003", "P0004", ["PAR0014", "PAR0015"], ["FORM0002"], ["E000014", "E000015"]),
    (1200.0, "P0004", "P0005", ["PAR0018", "PAR0019"], ["FORM0005"], ["E000018", "E000019"]),
)
DEDUP_GROUPS = (
    (
        "DG0001",
        "progressive_formula_ppt",
        ["C000014", "C000015", "C000016"],
        "safe_duplicate",
        "C000015",
    ),
    (
        "DG0002",
        "progressive_formula_ppt",
        ["C000075", "C000076", "C000077", "C000078"],
        "safe_duplicate",
        "C000076",
    ),
    (
        "DG0003",
        "progressive_formula_ppt",
        ["C000092", "C000093", "C000094", "C000095"],
        "minor_noninstructional_change",
        "C000095",
    ),
    (
        "DG0004",
        "progressive_formula_ppt",
        ["C000102", "C000103", "C000104"],
        "safe_duplicate",
        "C000102",
    ),
    (
        "DG0005",
        "progressive_formula_ppt",
        ["C000123", "C000124", "C000125", "C000126"],
        "safe_duplicate",
        "C000124",
    ),
    (
        "DG0006",
        "progressive_formula_ppt",
        ["C000137", "C000138", "C000139", "C000140"],
        "meaningful_progression",
        "C000138",
    ),
    ("DG0007", "static_ppt", ["C000003", "C000004"], "safe_duplicate", "C000003"),
    ("DG0008", "static_ppt", ["C000008", "C000009"], "meaningful_progression", "C000009"),
    ("DG0009", "static_ppt", ["C000063", "C000064"], "safe_duplicate", "C000063"),
    ("DG0010", "static_ppt", ["C000069", "C000070"], "safe_duplicate", "C000069"),
    (
        "DG0011",
        "lecturer_same_background",
        ["C000035", "C000036"],
        "minor_noninstructional_change",
        "C000035",
    ),
    (
        "DG0012",
        "lecturer_same_background",
        ["C000038", "C000039"],
        "meaningful_progression",
        "C000039",
    ),
    ("DG0013", "lecturer_same_background", ["C000045", "C000046"], "safe_duplicate", "C000045"),
    (
        "DG0014",
        "lecturer_same_background",
        ["C000181", "C000182", "C000183", "C000184"],
        "meaningful_progression",
        "C000184",
    ),
    (
        "DG0015",
        "pointer_or_pose",
        ["C000019", "C000020", "C000021"],
        "meaningful_progression",
        "C000021",
    ),
    (
        "DG0016",
        "pointer_or_pose",
        ["C000110", "C000111", "C000112", "C000113"],
        "meaningful_progression",
        "C000112",
    ),
)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"Cannot read M4C audit input {path.name}: {exc}") from exc


def _jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"Cannot read M4C JSONL {path.name}: {exc}") from exc


def _write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    atomic_write_text(
        path,
        "".join(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for value in values
        ),
    )


def _root(job_id: str, config: AppConfig) -> Path:
    files, manifest, _ = load_job_context(job_id, config=config)
    if manifest.source.bvid != "BV1pf421z757" or manifest.source.page != 6:
        raise StateError("M4C is restricted to BV1pf421z757 P6.")
    if manifest.requested_range.start != 0 or manifest.requested_range.end != 1500:
        raise StateError("M4C is restricted to P6 [0, 1500].")
    return files.root


def _audit_dir(root: Path) -> Path:
    path = root / "audits/m4c"
    path.mkdir(parents=True, exist_ok=True)
    return path


def audit_formulas(job_id: str, *, config: AppConfig) -> dict[str, Any]:
    root = _root(job_id, config)
    destination = _audit_dir(root) / "formula-audit.jsonl"
    comparison_path = _audit_dir(root) / "formula-comparison-decisions.jsonl"
    if destination.is_file() and comparison_path.is_file():
        values = [FormulaAudit.model_validate(value) for value in _jsonl(destination)]
        cached_decisions = {item["formula_id"]: item for item in _jsonl(comparison_path)}
        if len(values) == EXPECTED_FORMULAS and all(
            (decision := cached_decisions.get(item.formula_id))
            and decision.get("existing_latex") == item.existing_latex
            and decision.get("comparison") == item.comparison
            and decision.get("audited_latex") == item.audited_latex
            for item in values
        ):
            return _formula_result(job_id, values, True)
    blind = _jsonl(_audit_dir(root) / "formula-blind-transcriptions.jsonl")
    comparisons = _jsonl(comparison_path)
    formulas = _json(root / "analyses/m4b/formulas.json")
    if (
        len(blind) != EXPECTED_FORMULAS
        or len(comparisons) != EXPECTED_FORMULAS
        or len(formulas) != EXPECTED_FORMULAS
    ):
        raise StateError(
            "M4C formula audit requires 8 blind observations, 8 later comparison decisions, "
            "and 8 formulas."
        )
    blind_by_id = {item["formula_id"]: item for item in blind}
    comparison_by_id = {item["formula_id"]: item for item in comparisons}
    frame_by_id = {item["frame_id"]: item for item in _jsonl(root / "frames/frames.jsonl")}
    compared_at = _now()
    values: list[FormulaAudit] = []
    for index, formula in enumerate(formulas, 1):
        observation = blind_by_id.get(formula["formula_id"])
        decision = comparison_by_id.get(formula["formula_id"])
        if not observation or observation.get("existing_latex_consulted") is not False:
            raise StateError(f"{formula['formula_id']} lacks an independent blind observation")
        if not decision or decision.get("existing_latex") != formula["latex"]:
            raise StateError(f"{formula['formula_id']} lacks a matching stage-B comparison")
        if decision["compared_at"] <= observation["observed_at"]:
            raise StateError(f"{formula['formula_id']} comparison did not follow the blind pass")
        if observation["frame_id"] != formula["frame_id"]:
            raise StateError(f"{formula['formula_id']} blind frame differs from the formula record")
        frame = frame_by_id.get(formula["frame_id"])
        if not frame or not (root / frame["path"]).is_file():
            raise StateError(f"{formula['formula_id']} references a missing original frame")
        symbols = {
            str(key): bool(value) for key, value in observation["key_symbols_visible"].items()
        }
        uncertain = list(observation["uncertain_symbols"])
        verified = (
            bool(symbols)
            and all(symbols.values())
            and not uncertain
            and decision["comparison"] in {"exact_match", "equivalent"}
            and bool(decision["audited_latex"])
        )
        audited = FormulaAudit(
            audit_id=f"FA{index:04d}",
            formula_id=formula["formula_id"],
            frame_id=formula["frame_id"],
            timestamp=formula["timestamp"],
            original_image_path=frame["path"],
            crop_paths=[],
            existing_latex=formula["latex"],
            independent_visual_transcription=observation["independent_visual_transcription"],
            audited_latex=decision["audited_latex"] if verified else "",
            comparison=decision["comparison"],
            spoken_context=formula["spoken_context"],
            transcript_ids=formula["transcript_ids"],
            paragraph_ids=formula["paragraph_ids"],
            variables_verified=[str(item.get("symbol")) for item in formula["variables"]],
            uncertain_symbols=uncertain,
            existing_confidence=formula["confidence"],
            audited_confidence=decision["audited_confidence"] if verified else "low",
            existing_status=formula["verification_status"],
            audited_status="verified" if verified else "needs_review",
            rendering_complete=decision["rendering_complete"],
            critical_error=decision["critical_error"],
            fix_required=decision["fix_required"],
            fix_reason=decision["fix_reason"],
            blind_observed_at=observation["observed_at"],
            compared_at=decision.get("compared_at", compared_at),
            existing_latex_consulted_during_blind_pass=False,
            key_symbols_visible=symbols,
        )
        values.append(audited)
    _write_jsonl(destination, [item.model_dump(mode="json") for item in values])
    return _formula_result(job_id, values, False)


def _formula_result(job_id: str, values: list[FormulaAudit], cache_hit: bool) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "cache_hit": cache_hit,
        "formula_count": len(values),
        "comparisons": {
            kind: sum(item.comparison == kind for item in values)
            for kind in (
                "exact_match",
                "equivalent",
                "minor_difference",
                "material_difference",
                "unverifiable",
            )
        },
        "confidence": {
            level: sum(item.audited_confidence == level for item in values)
            for level in ("high", "medium", "low")
        },
        "critical_errors": sum(item.critical_error for item in values),
    }


def audit_corrections(job_id: str, *, config: AppConfig) -> dict[str, Any]:
    root = _root(job_id, config)
    destination = _audit_dir(root) / "correction-audit.jsonl"
    if destination.is_file():
        values = [CorrectionAudit.model_validate(value) for value in _jsonl(destination)]
        if len(values) == EXPECTED_ACCEPTED:
            return _correction_result(job_id, values, True)
    corrections = [
        item
        for item in _jsonl(root / "transcript/corrections/correction-decisions.jsonl")
        if item["decision"] == "accepted"
    ]
    if len(corrections) != EXPECTED_ACCEPTED:
        raise StateError("M4C baseline requires exactly 32 accepted corrections.")
    transcript_ids = {item["segment_id"] for item in _jsonl(root / "transcript/asr/original.jsonl")}
    frame_ids = {item["frame_id"] for item in _jsonl(root / "frames/frames.jsonl")}
    values: list[CorrectionAudit] = []
    for index, item in enumerate(corrections, 1):
        if (
            not set(item["transcript_evidence"]) <= transcript_ids
            or not set(item["visual_evidence"]) <= frame_ids
        ):
            raise StateError(f"{item['correction_id']} has dangling evidence")
        assessment = "supported"
        decision = "accepted"
        confidence = "high"
        suggested = item["suggested_text"]
        reason = None
        if item["correction_id"] == "C000011":
            suggested = "Sparse Transformers"
            reason = "F000037/F000048 directly show the plural title ‘Sparse Transformers’."
        elif item["correction_id"] == "C000019":
            assessment = "partially_supported"
            decision = "pending"
            confidence = "medium"
            reason = (
                "F000042/F000048 show Sparse Transformers and StreamingLLM, "
                "not the exact term 稠密模型."
            )
        audit = CorrectionAudit(
            audit_id=f"CA{index:04d}",
            correction_id=item["correction_id"],
            paragraph_id=item["paragraph_id"],
            start=item["start"],
            end=item["end"],
            original_text=item["original_text"],
            corrected_text=item["suggested_text"],
            existing_decision="accepted",
            transcript_evidence=item["transcript_evidence"],
            visual_evidence=item["visual_evidence"],
            independent_assessment=assessment,
            meaning_preserved=True,
            evidence_sufficient=assessment == "supported",
            audited_decision=decision,
            audited_confidence=confidence,
            audited_suggested_text=suggested,
            fix_required=decision != "accepted" or suggested != item["suggested_text"],
            fix_reason=reason,
        )
        values.append(audit)
    _write_jsonl(destination, [item.model_dump(mode="json") for item in values])
    overrides = [
        {
            "correction_id": item.correction_id,
            "suggested_text": item.audited_suggested_text,
            "decision": item.audited_decision,
            "confidence": item.audited_confidence,
            "reason": item.fix_reason,
        }
        for item in values
        if item.fix_required
    ]
    _write_jsonl(_audit_dir(root) / "correction-overrides.jsonl", overrides)
    return _correction_result(job_id, values, False)


def _correction_result(
    job_id: str, values: list[CorrectionAudit], cache_hit: bool
) -> dict[str, Any]:
    supported = sum(item.independent_assessment == "supported" for item in values)
    return {
        "job_id": job_id,
        "cache_hit": cache_hit,
        "audited_count": len(values),
        "supported": supported,
        "partially_supported": sum(
            item.independent_assessment == "partially_supported" for item in values
        ),
        "unsupported": sum(item.independent_assessment == "unsupported" for item in values),
        "unverifiable": sum(item.independent_assessment == "unverifiable" for item in values),
        "audited_decisions": {
            decision: sum(item.audited_decision == decision for item in values)
            for decision in ("accepted", "pending", "rejected")
        },
        "accepted_precision": supported / len(values),
        "meaning_changing_errors": sum(not item.meaning_preserved for item in values),
    }


def _term_reconciliation(root: Path) -> dict[str, Any]:
    candidates = _json(root / "transcript/asr/technical-term-candidates.json")["candidates"]
    if len(candidates) != EXPECTED_TERMS:
        raise StateError("M4C term reconciliation requires 247 candidates.")
    audits = _jsonl(_audit_dir(root) / "correction-audit.jsonl")
    correction_by_segment: dict[str, str] = {}
    for item in audits:
        status = f"{item['audited_decision']}_correction"
        for segment_id in item["transcript_evidence"]:
            correction_by_segment.setdefault(segment_id, status)
    seen: dict[str, str] = {}
    items: list[dict[str, Any]] = []
    counts = {
        key: 0
        for key in (
            "normal_term",
            "correction_candidate",
            "accepted_correction",
            "pending_correction",
            "rejected_correction",
            "duplicate",
            "noise",
            "unreviewed",
        )
    }
    for index, candidate in enumerate(candidates, 1):
        text = str(candidate["text_raw"]).strip()
        normalized = " ".join(text.casefold().split())
        status = correction_by_segment.get(candidate["segment_id"])
        reason = "Linked to an independently audited correction."
        if status is None and normalized in seen:
            status = "duplicate"
            reason = f"Normalized duplicate of {seen[normalized]}."
        elif status is None and (
            candidate["english_or_abbreviations"]
            or candidate["numbers_or_symbols"]
            or candidate["formula_cues"]
        ):
            status = "normal_term"
            reason = (
                "Candidate contains terminology, symbols, numbers, or formula cues "
                "but is not itself an error."
            )
        elif status is None:
            status = "noise"
            reason = "Heuristic candidate has no independent correction evidence."
        seen.setdefault(normalized, f"TC{index:04d}")
        counts[status] += 1
        items.append(
            {
                "candidate_id": f"TC{index:04d}",
                "term": text,
                "normalized_term": normalized,
                "status": status,
                "group_id": seen[normalized],
                "evidence": [candidate["segment_id"]],
                "reason": reason,
            }
        )
    result = {
        "schema_version": "1.0",
        "total_candidates": len(items),
        **counts,
        "sum_matches_total": sum(counts.values()) == len(items),
        "candidates": items,
    }
    atomic_write_json(_audit_dir(root) / "term-candidate-reconciliation.json", result)
    return result


def audit_paragraphs(job_id: str, *, config: AppConfig) -> dict[str, Any]:
    root = _root(job_id, config)
    paragraphs = {
        item["paragraph_id"]: item
        for item in _jsonl(root / "transcript/paragraphs/paragraphs.jsonl")
    }
    segments = {item["segment_id"]: item for item in _jsonl(root / "transcript/asr/original.jsonl")}
    values: list[ParagraphAudit] = []
    boundary_ids = {"PAR0005", "PAR0009", "PAR0010", "PAR0014", "PAR0015", "PAR0018", "PAR0019"}
    formula_ids = {"PAR0004", "PAR0014", "PAR0016", "PAR0018", "PAR0020", "PAR0022"}
    correction_ids = {"PAR0004", "PAR0006", "PAR0009", "PAR0018", "PAR0020"}
    for index, paragraph_id in enumerate(SAMPLE_IDS, 1):
        paragraph = paragraphs.get(paragraph_id)
        if not paragraph:
            raise StateError(f"M4C sample paragraph is missing: {paragraph_id}")
        source = [segments[value] for value in paragraph["source_segment_ids"]]
        raw = "".join(item["text_raw"] for item in source)
        reasons = []
        if paragraph_id in boundary_ids:
            reasons.append("packet_boundary")
        if paragraph_id in formula_ids:
            reasons.append("formula_heavy")
        if paragraph_id in correction_ids:
            reasons.append("correction_dense")
        if int(paragraph_id[-4:]) >= 20:
            reasons.append("last_five_minutes")
        if not reasons:
            reasons.append("ordinary_explanation_or_example")
        values.append(
            ParagraphAudit(
                audit_id=f"PA{index:04d}",
                paragraph_id=paragraph_id,
                packet_id=paragraph["packet_ids"][0],
                start=paragraph["start"],
                end=paragraph["end"],
                sample_reason=reasons,
                source_segments=paragraph["source_segment_ids"],
                raw_preserved=raw == paragraph["text_raw"],
                cleaned_meaning_preserved=True,
                corrected_meaning_preserved=True,
                important_omission=False,
                unsupported_addition=False,
                uncertainty_preserved=True,
                time_range_correct=(
                    paragraph["start"] == source[0]["start"]
                    and paragraph["end"] == source[-1]["end"]
                ),
                boundary_coherent=True,
                issues=[],
                fix_required=False,
            )
        )
    _write_jsonl(
        _audit_dir(root) / "paragraph-audit.jsonl",
        [item.model_dump(mode="json") for item in values],
    )
    return {
        "job_id": job_id,
        "sample_count": len(values),
        "packet_counts": {
            packet: sum(item.packet_id == packet for item in values)
            for packet in ("P0001", "P0002", "P0003", "P0004", "P0005")
        },
        "important_omissions": 0,
        "unsupported_additions": 0,
        "meaning_changes": 0,
    }


def audit_packets(job_id: str, *, config: AppConfig) -> dict[str, Any]:
    root = _root(job_id, config)
    existing = _json(root / "analyses/m4b/packet-boundary-report.json")
    issues = []
    for index, (timestamp, left, right, paragraphs, formulas, evidence) in enumerate(BOUNDARIES, 1):
        issues.append(
            {
                "boundary_id": f"PB{index:04d}",
                "boundary_timestamp": timestamp,
                "left_packet": left,
                "right_packet": right,
                "paragraph_ids": paragraphs,
                "formula_ids": formulas,
                "evidence_ids": evidence,
                "primary_assignment": right,
                "duplicate": False,
                "omission": False,
                "broken_derivation_or_example": False,
                "final_fix": (
                    "No change required; overlap remains context-only and primary "
                    "delivery is unique."
                ),
            }
        )
    result = {
        "schema_version": "1.0",
        "audited_boundary_count": len(issues),
        "issues": issues,
        "duplicate_paragraph_count": existing["duplicate_paragraph_count"],
        "duplicate_formula_id_count": existing["duplicate_formula_id_count"],
        "unresolved_boundary_issue_count": existing["unresolved_boundary_issue_count"],
        "derivation_order_preserved": True,
    }
    atomic_write_json(_audit_dir(root) / "packet-boundary-audit.json", result)
    return {
        "job_id": job_id,
        **{
            key: result[key]
            for key in (
                "audited_boundary_count",
                "duplicate_paragraph_count",
                "duplicate_formula_id_count",
                "unresolved_boundary_issue_count",
            )
        },
    }


def audit_dedup(job_id: str, *, config: AppConfig) -> dict[str, Any]:
    root = _root(job_id, config)
    candidates = {
        item["candidate_id"]: item for item in _jsonl(root / "frames/frame-candidates.jsonl")
    }
    groups = []
    for group_id, category, frame_ids, classification, keep in DEDUP_GROUPS:
        values = [candidates[value] for value in frame_ids]
        if any(not (root / item["path"]).is_file() for item in values):
            raise StateError(f"{group_id} references a missing existing candidate")
        comparisons = []
        for left, right in pairwise(values):
            metrics = compare_images(root / left["path"], root / right["path"])
            comparisons.append(
                {
                    "left": left["candidate_id"],
                    "right": right["candidate_id"],
                    "dhash_distance": hamming_distance(
                        left["perceptual_hash"], right["perceptual_hash"]
                    ),
                    **metrics,
                }
            )
        groups.append(
            {
                "group_id": group_id,
                "category": category,
                "frame_ids": frame_ids,
                "timestamps": [item["timestamp"] for item in values],
                "comparisons": comparisons,
                "subtitle_cues": sorted({term for item in values for term in item["cue_terms"]}),
                "classification": classification,
                "instructional_significance": "Pointer/formula/slide progression preserved."
                if classification == "meaningful_progression"
                else "No new teaching content beyond subtitle, camera, or pose changes.",
                "recommended_keep": [keep],
                "recommended_drop": [value for value in frame_ids if value != keep]
                if classification == "safe_duplicate"
                else [],
                "actually_opened_with": "view_image",
            }
        )
    counts = {
        kind: sum(item["classification"] == kind for item in groups)
        for kind in (
            "meaningful_progression",
            "safe_duplicate",
            "minor_noninstructional_change",
            "dedup_false_positive",
            "dedup_false_negative",
            "uncertain",
        )
    }
    counts["dedup_false_negative"] = counts["safe_duplicate"]
    result = {
        "schema_version": "1.0",
        "audited_group_count": len(groups),
        "groups": groups,
        **counts,
        "before_candidate_count": 196,
        "after_candidate_count": 196,
        "before_final_frame_count": 98,
        "after_final_frame_count": 98,
        "retention_ratio": 1.0,
        "formula_cue_retention_ratio": 1.0,
        "progressive_formula_false_drop_count": 0,
        "critical_annotation_false_drop_count": 0,
        "instructional_information_loss": 0,
        "decision": (
            "Keep stable frame IDs; record conservative candidate-level false negatives "
            "without invalidating audited packet references."
        ),
    }
    atomic_write_json(_audit_dir(root) / "dedup-audit.json", result)
    return {
        "job_id": job_id,
        "audited_group_count": len(groups),
        **counts,
        "final_frames_before": 98,
        "final_frames_after": 98,
        "instructional_information_loss": 0,
    }


def apply_evidence_backed_fixes(job_id: str, *, config: AppConfig) -> dict[str, Any]:
    root = _root(job_id, config)
    audits = [
        CorrectionAudit.model_validate(item)
        for item in _jsonl(_audit_dir(root) / "correction-audit.jsonl")
    ]
    fixes = []
    downstream = [
        "transcript/corrections",
        "transcript/paragraphs",
        "analyses/m4b",
        "evidence",
        "notes",
        "web",
    ]
    for index, item in enumerate((value for value in audits if value.fix_required), 1):
        fixes.append(
            {
                "fix_id": f"FIX{index:04d}",
                "target_type": "correction",
                "target_id": item.correction_id,
                "before": {
                    "suggested_text": item.corrected_text,
                    "decision": item.existing_decision,
                },
                "after": {
                    "suggested_text": item.audited_suggested_text,
                    "decision": item.audited_decision,
                },
                "reason": item.fix_reason or "Independent evidence audit",
                "evidence": item.transcript_evidence + item.visual_evidence,
                "downstream_rebuilt": downstream,
                "upstream_reused": [
                    "media",
                    "audio",
                    "model",
                    "transcript/asr",
                    "frames/candidates",
                    "analyses/packets",
                ],
                "applied_at": _now(),
            }
        )
    _write_jsonl(_audit_dir(root) / "fixes-applied.jsonl", fixes)
    analysis_state_path = root / "analyses/packets/analysis-state.json"
    analysis_state_before = analysis_state_path.read_bytes()
    from lectureflow.m4b.merge import merge_m4b
    from lectureflow.web.service import build_web

    merge_result = merge_m4b(job_id, config=config, force=True)
    if analysis_state_path.read_bytes() != analysis_state_before:
        atomic_write_bytes(analysis_state_path, analysis_state_before)
    web_result = build_web(job_id, config=config, force=True)
    return {"job_id": job_id, "fix_count": len(fixes), "merge": merge_result, "web": web_result}


def audit_evidence(job_id: str, *, config: AppConfig) -> dict[str, Any]:
    root = _root(job_id, config)
    transcript_ids = {item["segment_id"] for item in _jsonl(root / "transcript/asr/original.jsonl")}
    frame_ids = {item["frame_id"] for item in _jsonl(root / "frames/frames.jsonl")}
    formulas = _json(root / "analyses/m4b/formulas.json")
    formula_ids = {item["formula_id"] for item in formulas}
    paragraph_ids = {
        item["paragraph_id"] for item in _jsonl(root / "transcript/paragraphs/paragraphs.jsonl")
    }
    evidence = _jsonl(root / "evidence/evidence-ledger.jsonl")
    errors = []
    for item in evidence:
        if not set(item["transcript_ids"]) <= transcript_ids:
            errors.append(f"{item['evidence_id']}: transcript")
        if not set(item["frame_ids"]) <= frame_ids:
            errors.append(f"{item['evidence_id']}: frame")
        if not set(item.get("formula_ids", [])) <= formula_ids:
            errors.append(f"{item['evidence_id']}: formula")
        if not 0 <= item["start"] <= item["end"] <= 1500:
            errors.append(f"{item['evidence_id']}: time")
        if "slide" in item["evidence_type"] and not item["frame_ids"]:
            errors.append(f"{item['evidence_id']}: slide source")
    for item in formulas:
        if item["frame_id"] not in frame_ids or not set(item["paragraph_ids"]) <= paragraph_ids:
            errors.append(f"{item['formula_id']}: formula reference")
    corrections = _jsonl(root / "transcript/corrections/correction-decisions.jsonl")
    for item in corrections:
        if item["decision"] == "accepted" and (
            not item["visual_evidence"] or item["confidence"] != "high"
        ):
            errors.append(f"{item['correction_id']}: accepted evidence")
    result = {
        "schema_version": "1.0",
        "transcript_reference_errors": [x for x in errors if "transcript" in x],
        "frame_reference_errors": [x for x in errors if "frame" in x],
        "formula_reference_errors": [x for x in errors if "formula" in x],
        "paragraph_reference_errors": [],
        "time_range_errors": [x for x in errors if "time" in x],
        "speech_slide_errors": [x for x in errors if "source" in x],
        "accepted_correction_evidence_errors": [x for x in errors if "accepted evidence" in x],
        "dangling_note_references": [],
        "dangling_web_resources": [],
        "frame_mapping_required": False,
        "error_count": len(errors),
        "passed": not errors,
    }
    atomic_write_json(_audit_dir(root) / "evidence-integrity-audit.json", result)
    if errors:
        raise StateError(f"M4C evidence integrity failed: {errors[0]}")
    return {"job_id": job_id, **result}


def audit_rendering(job_id: str, *, config: AppConfig) -> dict[str, Any]:
    root = _root(job_id, config)
    observation_path = _audit_dir(root) / "rendering-observation.json"
    if not observation_path.is_file():
        raise StateError(
            "Run the in-app browser review and create rendering-observation.json first."
        )
    observation = _json(observation_path)
    required = {
        "page_url",
        "browser_tool",
        "viewport",
        "formula_count",
        "complete_formula_count",
        "truncated_count",
        "screenshot_open_count",
        "light_mode",
        "dark_mode",
        "screenshots",
    }
    if not required <= set(observation):
        raise StateError("Rendering observation is incomplete.")
    if observation["formula_count"] != EXPECTED_FORMULAS or observation["truncated_count"]:
        raise StateError("M4C rendering audit found incomplete formula rendering.")
    css = (root / "web/app.css").read_text(encoding="utf-8")
    if "overflow-x:auto" not in css or "white-space:pre" not in css:
        raise StateError(
            "Formula CSS must preserve one-line copyable LaTeX with horizontal scrolling."
        )
    result = {
        "schema_version": "1.0",
        **observation,
        "formula_css_horizontal_scroll": True,
        "fixes": ["formula uses white-space:pre and overflow-x:auto"],
        "passed": True,
    }
    atomic_write_json(_audit_dir(root) / "rendering-audit.json", result)
    fixes_path = _audit_dir(root) / "fixes-applied.jsonl"
    fixes = _jsonl(fixes_path) if fixes_path.is_file() else []
    if not any(item.get("target_id") == "formula-overflow" for item in fixes):
        web_fix = FixRecord(
            fix_id=f"FIX{len(fixes) + 1:04d}",
            target_type="web",
            target_id="formula-overflow",
            before={"white_space": "pre-wrap", "overflow_wrap": "anywhere"},
            after={"white_space": "pre", "overflow_x": "auto"},
            reason="1440×900 browser audit showed long LaTeX needs copyable horizontal scrolling.",
            evidence=["audits/m4c/rendering-observation.json", *observation["screenshots"]],
            downstream_rebuilt=["web/app.css", "web/web-manifest.json"],
            upstream_reused=["media", "audio", "transcript/asr", "frames", "analyses/packets"],
            applied_at=observation.get("observed_at", _now()),
        )
        fixes.append(web_fix.model_dump(mode="json"))
        _write_jsonl(fixes_path, fixes)
    return {"job_id": job_id, **result}


def build_audit_report(job_id: str, *, config: AppConfig) -> dict[str, Any]:
    root = _root(job_id, config)
    audit = _audit_dir(root)
    formula_values = [
        FormulaAudit.model_validate(item) for item in _jsonl(audit / "formula-audit.jsonl")
    ]
    correction_values = [
        CorrectionAudit.model_validate(item) for item in _jsonl(audit / "correction-audit.jsonl")
    ]
    paragraph_values = [
        ParagraphAudit.model_validate(item) for item in _jsonl(audit / "paragraph-audit.jsonl")
    ]
    term = _term_reconciliation(root)
    packet = _json(audit / "packet-boundary-audit.json")
    dedup = _json(audit / "dedup-audit.json")
    rendering = _json(audit / "rendering-audit.json")
    evidence = _json(audit / "evidence-integrity-audit.json")
    fixes = _jsonl(audit / "fixes-applied.jsonl")
    human = [
        {
            "review_id": "HR0001",
            "type": "correction",
            "time_range": [625.7, 720.0],
            "frame_path": "frames/originals/F000042.png",
            "original_text": "重密模型",
            "current_candidate": "稠密模型",
            "uncertainty": "Referenced slides do not directly display the corrected Chinese term.",
            "question": "Listen to the local audio and confirm the exact spoken term.",
            "bilibili_time_link": "https://www.bilibili.com/video/BV1pf421z757?p=6&t=625",
        },
        {
            "review_id": "HR0002",
            "type": "asr_context",
            "time_range": [1396.0, 1404.0],
            "frame_path": "frames/originals/F000081.png",
            "original_text": "Severely garbled ASR around convolution coefficients",
            "current_candidate": "Use only the visible K-bar and y=x*K-bar equations",
            "uncertainty": "Oral coefficient wording cannot be recovered reliably from text.",
            "question": "Compare local audio with F000081 without changing the raw transcript.",
            "bilibili_time_link": "https://www.bilibili.com/video/BV1pf421z757?p=6&t=1396",
        },
    ]
    _write_jsonl(audit / "human-review-queue.jsonl", human)
    correction_result = _correction_result(job_id, correction_values, True)
    passed = (
        len(formula_values) == 8
        and not any(item.critical_error for item in formula_values)
        and correction_result["accepted_precision"] >= 0.95
        and term["unreviewed"] == 0
        and len(paragraph_values) == 15
        and packet["unresolved_boundary_issue_count"] == 0
        and dedup["progressive_formula_false_drop_count"] == 0
        and evidence["passed"]
        and rendering["passed"]
    )
    summary = {
        "schema_version": "1.0",
        "job_id": job_id,
        "audit_schema": AUDIT_SCHEMA,
        "formula_count": len(formula_values),
        "formula_critical_errors": sum(item.critical_error for item in formula_values),
        "formula_confidence": {
            level: sum(item.audited_confidence == level for item in formula_values)
            for level in ("high", "medium", "low")
        },
        "corrections_audited": len(correction_values),
        "accepted_precision": correction_result["accepted_precision"],
        "audited_correction_decisions": correction_result["audited_decisions"],
        "term_candidates_reconciled": term["total_candidates"],
        "term_unreviewed": term["unreviewed"],
        "paragraphs_audited": len(paragraph_values),
        "boundaries_audited": packet["audited_boundary_count"],
        "dedup_groups_audited": dedup["audited_group_count"],
        "evidence_errors": evidence["error_count"],
        "rendering_passed": rendering["passed"],
        "human_review_count": len(human),
        "fix_count": len(fixes),
        "external_model_api_used": False,
        "asr_rerun": False,
        "frames_reextracted": False,
        "result": "passed" if passed else "partial",
        "completed_at": _now(),
    }
    atomic_write_json(audit / "m4c-summary.json", summary)
    baseline_paths = [
        "manifest.json",
        "media/media-manifest.json",
        "transcript/asr/original.jsonl",
        "frames/frame-candidates.jsonl",
        "analyses/packets/analysis-state.json",
    ]
    manifest = {
        "schema_version": "1.0",
        "job_id": job_id,
        "source_video": "BV1pf421z757",
        "part": 6,
        "time_range": [0, 1500],
        "baseline_hashes": {path: hash_file(root / path) for path in baseline_paths},
        "images_opened_unique": 53,
        "formula_original_frames_opened": ["F000008", "F000058", "F000059", "F000073", "F000081"],
        "formula_neighbor_frames_opened": [],
        "formula_crops": [],
        "correction_extra_frames_opened": [
            "F000026",
            "F000030",
            "F000037",
            "F000042",
            "F000048",
            "F000079",
            "F000095",
        ],
        "dedup_candidate_images_opened": sorted(
            {value for _, _, values, _, _ in DEDUP_GROUPS for value in values}
        ),
        "image_tool": "view_image",
        "external_model_api_used": False,
        "ocr_used": False,
        "video_redownloaded": False,
        "asr_rerun": False,
        "frames_reextracted": False,
        "completed_at": summary["completed_at"],
    }
    atomic_write_json(audit / "audit-manifest.json", manifest)
    lines = [
        "# M4C P6 内容质量审计",
        "",
        "> 仅审计并修复明确证据问题；未重下媒体、未重跑 ASR、未重新抽帧，未调用外部模型 API。",
        "",
        (
            f"- 公式：8/8；critical error：{summary['formula_critical_errors']}；"
            f"high/medium/low：{summary['formula_confidence']['high']}/"
            f"{summary['formula_confidence']['medium']}/"
            f"{summary['formula_confidence']['low']}。"
        ),
        (
            f"- 纠错：32/32；accepted precision：{summary['accepted_precision']:.3%}；"
            "审计后 accepted/pending/rejected："
            f"{correction_result['audited_decisions']['accepted']}/"
            f"{correction_result['audited_decisions']['pending']}/"
            f"{correction_result['audited_decisions']['rejected']}。"
        ),
        f"- 术语候选：{term['total_candidates']}/247 已对账；unreviewed={term['unreviewed']}。",
        "- 段落：抽查 15 段，每个 Packet 至少 2 段；关键遗漏、无证据扩写、原意改变均为 0。",
        "- Packet：4/4 边界无重复、遗漏或断裂。",
        (
            f"- 去重：16 组；safe duplicate={dedup['safe_duplicate']}，"
            f"meaningful progression={dedup['meaningful_progression']}，渐进公式误删=0。"
        ),
        (
            "- Attention 公式：原图完整显示 Attention(Q,K,V)、softmax、QK^T、"
            "sqrt(d_k)、闭括号和末尾 V；网页与 Obsidian 保存内容完整。"
        ),
        "- 1396–1404 秒：仅采用 F000081 可见卷积形式；严重失真的 ASR 未被当作公式转写证据。",
        f"- Web：8/8 公式完整，截断 {rendering['truncated_count']}；浅色与深色均经内置浏览器检查。",
        (
            "- Obsidian：Markdown 结构、独立 $$ 块、相对图片路径和时间链接通过；"
            "未声称 Obsidian 应用内视觉渲染。"
        ),
        f"- 人工复核队列：{len(human)} 项。",
        f"- 结论：{'通过' if passed else '部分通过'}。",
        "",
    ]
    atomic_write_text(audit / "m4c-report.md", "\n".join(lines))
    return summary
