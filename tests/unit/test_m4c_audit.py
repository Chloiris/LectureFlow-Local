from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

import lectureflow.m4b.merge as merge
import lectureflow.m4c.service as audit
import lectureflow.web.service as web
from lectureflow.hashing import hash_file
from lectureflow.schemas.m4c import CorrectionAudit, FormulaAudit

from ..helpers_m4b import m4b_fixture


def _write_jsonl(path: Path, values: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(value) + "\n" for value in values))


def _audit_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[object, Path]:
    config, files, manifest, state = m4b_fixture(tmp_path, monkeypatch)

    def context(*args: object, **kwargs: object) -> tuple[object, object, object]:
        return files, manifest, state

    monkeypatch.setattr(merge, "load_job_context", context)
    monkeypatch.setattr(audit, "load_job_context", context)
    monkeypatch.setattr(web, "load_job_context", context)
    root = files.root
    merge.merge_m4b("m4b-fixture", config=config)
    web.build_web("m4b-fixture", config=config)

    formulas_path = root / "analyses/m4b/formulas.json"
    template = json.loads(formulas_path.read_text())[0]
    formulas = []
    blinds = []
    comparisons = []
    for index in range(1, 9):
        formula = template | {"formula_id": f"FORM{index:04d}"}
        formulas.append(formula)
        blinds.append(
            {
                "blind_id": f"FB{index:04d}",
                "formula_id": formula["formula_id"],
                "frame_id": formula["frame_id"],
                "observed_at": "2026-08-13T00:00:00Z",
                "image_tool": "view_image",
                "existing_latex_consulted": False,
                "independent_visual_transcription": formula["visual_transcription"],
                "key_symbols_visible": {"left": True, "operator": True, "right": True},
                "uncertain_symbols": [],
            }
        )
        comparisons.append(
            {
                "formula_id": formula["formula_id"],
                "compared_at": "2026-08-13T00:01:00Z",
                "existing_latex": formula["latex"],
                "comparison": "exact_match",
                "audited_latex": formula["latex"],
                "audited_confidence": "high",
                "rendering_complete": True,
                "critical_error": False,
                "fix_required": False,
                "fix_reason": None,
            }
        )
    formulas_path.write_text(json.dumps(formulas))
    _write_jsonl(root / "audits/m4c/formula-blind-transcriptions.jsonl", blinds)
    _write_jsonl(root / "audits/m4c/formula-comparison-decisions.jsonl", comparisons)

    paragraphs = [
        json.loads(line)
        for line in (root / "transcript/paragraphs/paragraphs.jsonl").read_text().splitlines()
    ]
    segments = [
        json.loads(line)
        for line in (root / "transcript/asr/original.jsonl").read_text().splitlines()
    ]
    frame = json.loads((root / "frames/frames.jsonl").read_text().splitlines()[0])
    corrections = []
    for index in range(1, 33):
        paragraph = paragraphs[(index - 1) % len(paragraphs)]
        segment_id = paragraph["source_segment_ids"][0]
        corrections.append(
            {
                "schema_version": "1.0",
                "correction_id": f"C{index:06d}",
                "paragraph_id": paragraph["paragraph_id"],
                "source_segment_ids": [segment_id],
                "start": paragraph["start"],
                "end": paragraph["end"],
                "original_text": f"错词{index}",
                "suggested_text": f"术语{index}",
                "change_type": "term",
                "reason": "fixture visual evidence",
                "transcript_evidence": [segment_id],
                "visual_evidence": [frame["frame_id"]],
                "confidence": "high",
                "decision": "accepted",
                "applied": True,
            }
        )
    _write_jsonl(root / "transcript/corrections/correction-decisions.jsonl", corrections)

    candidates = []
    for index in range(1, 197):
        candidates.append(
            {
                "candidate_id": f"C{index:06d}",
                "path": frame["path"],
                "timestamp": min(1499.0, index * 7.0),
                "perceptual_hash": "0000000000000000",
                "cue_terms": ["公式"] if index > 100 else [],
            }
        )
    _write_jsonl(root / "frames/frame-candidates.jsonl", candidates)

    term_candidates = []
    for index in range(247):
        segment = segments[index % len(segments)]
        term_candidates.append(
            {
                "segment_id": segment["segment_id"],
                "text_raw": f"term {index % 20}",
                "english_or_abbreviations": ["LLM"] if index % 3 == 0 else [],
                "numbers_or_symbols": [str(index)] if index % 5 == 0 else [],
                "formula_cues": ["公式"] if index % 7 == 0 else [],
            }
        )
    (root / "transcript/asr/technical-term-candidates.json").write_text(
        json.dumps({"candidate_count": 247, "candidates": term_candidates})
    )
    (root / "manifest.json").write_text("{}")
    (root / "media/media-manifest.json").write_text("{}")
    return config, root


def test_m4c_formula_and_correction_schemas_enforce_audit_rules() -> None:
    formula = {
        "audit_id": "FA0001",
        "formula_id": "FORM0001",
        "frame_id": "F000001",
        "timestamp": 1,
        "original_image_path": "frames/originals/F000001.png",
        "crop_paths": [],
        "existing_latex": "x",
        "independent_visual_transcription": "x",
        "audited_latex": "x",
        "comparison": "exact_match",
        "spoken_context": "context",
        "transcript_ids": ["T000001"],
        "paragraph_ids": ["PAR0001"],
        "variables_verified": [],
        "uncertain_symbols": ["subscript"],
        "existing_confidence": "high",
        "audited_confidence": "high",
        "existing_status": "verified",
        "audited_status": "verified",
        "rendering_complete": True,
        "critical_error": False,
        "fix_required": False,
        "fix_reason": None,
        "blind_observed_at": "before",
        "compared_at": "after",
        "existing_latex_consulted_during_blind_pass": False,
        "key_symbols_visible": {"x": True},
    }
    with pytest.raises(ValidationError, match="every key symbol"):
        FormulaAudit.model_validate(formula)

    formula["uncertain_symbols"] = []
    formula["key_symbols_visible"] = {
        "attention_arguments": True,
        "softmax": True,
        "qk_transpose": True,
        "sqrt_d_sub_k": True,
        "closing_parenthesis": True,
    }
    with pytest.raises(ValidationError, match="every structural symbol"):
        FormulaAudit.model_validate(formula)

    formula["key_symbols_visible"]["trailing_v"] = True
    formula["crop_paths"] = ["audits/m4c/crops/FORM0001.png"]
    formula["crop_regions"] = [
        {
            "image_path": "audits/m4c/crops/FORM0001.png",
            "x": 10,
            "y": 20,
            "width": 0,
            "height": 40,
        }
    ]
    with pytest.raises(ValidationError):
        FormulaAudit.model_validate(formula)

    formula["crop_paths"] = []
    formula["crop_regions"] = []
    formula["existing_latex_consulted_during_blind_pass"] = True
    with pytest.raises(ValidationError):
        FormulaAudit.model_validate(formula)

    correction = {
        "audit_id": "CA0001",
        "correction_id": "C000001",
        "paragraph_id": "PAR0001",
        "start": 0,
        "end": 1,
        "original_text": "a",
        "corrected_text": "b",
        "existing_decision": "accepted",
        "transcript_evidence": ["T000001"],
        "visual_evidence": ["F000001"],
        "independent_assessment": "unsupported",
        "meaning_preserved": False,
        "evidence_sufficient": False,
        "audited_decision": "pending",
        "audited_confidence": "low",
        "audited_suggested_text": "b",
        "fix_required": True,
        "fix_reason": "meaning changed",
    }
    with pytest.raises(ValidationError, match="must be rejected"):
        CorrectionAudit.model_validate(correction)


def test_m4c_end_to_end_audit_reconciles_every_item_and_preserves_upstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, root = _audit_fixture(tmp_path, monkeypatch)
    raw_before = hash_file(root / "transcript/asr/original.jsonl")
    candidate_before = hash_file(root / "frames/frame-candidates.jsonl")

    formulas = audit.audit_formulas("m4b-fixture", config=config)
    corrections = audit.audit_corrections("m4b-fixture", config=config)
    assert audit.audit_formulas("m4b-fixture", config=config)["cache_hit"] is True
    assert audit.audit_corrections("m4b-fixture", config=config)["cache_hit"] is True
    paragraphs = audit.audit_paragraphs("m4b-fixture", config=config)
    packets = audit.audit_packets("m4b-fixture", config=config)
    dedup = audit.audit_dedup("m4b-fixture", config=config)
    monkeypatch.setattr(merge, "merge_m4b", lambda *args, **kwargs: {"cache_hit": False})
    monkeypatch.setattr(web, "build_web", lambda *args, **kwargs: {"cache_hit": False})
    fixes = audit.apply_evidence_backed_fixes("m4b-fixture", config=config)
    evidence = audit.audit_evidence("m4b-fixture", config=config)
    (root / "audits/m4c/rendering-observation.json").write_text(
        json.dumps(
            {
                "page_url": "http://127.0.0.1:8765/",
                "browser_tool": "browser control",
                "viewport": [1440, 900],
                "formula_count": 8,
                "complete_formula_count": 8,
                "truncated_count": 0,
                "screenshot_open_count": 8,
                "light_mode": "pass",
                "dark_mode": "pass",
                "screenshots": ["reports/m4c-light.png", "reports/m4c-dark.png"],
            }
        )
    )
    rendering = audit.audit_rendering("m4b-fixture", config=config)
    summary = audit.build_audit_report("m4b-fixture", config=config)
    reconciliation = json.loads(
        (root / "audits/m4c/term-candidate-reconciliation.json").read_text()
    )

    assert formulas["formula_count"] == 8 and formulas["critical_errors"] == 0
    assert corrections["audited_count"] == 32
    assert corrections["accepted_precision"] == 31 / 32
    assert corrections["audited_decisions"] == {"accepted": 31, "pending": 1, "rejected": 0}
    assert min(paragraphs["packet_counts"].values()) >= 2
    assert packets["audited_boundary_count"] == 4
    assert dedup["audited_group_count"] == 16 and dedup["safe_duplicate"] > 0
    assert dedup["instructional_information_loss"] == 0
    assert fixes["fix_count"] == 2
    assert evidence["passed"] and rendering["passed"]
    assert reconciliation["total_candidates"] == 247
    assert reconciliation["unreviewed"] == 0 and reconciliation["sum_matches_total"]
    assert summary["result"] == "passed" and summary["external_model_api_used"] is False
    assert summary["fix_count"] == 3
    assert hash_file(root / "transcript/asr/original.jsonl") == raw_before
    assert hash_file(root / "frames/frame-candidates.jsonl") == candidate_before


def test_m4c_formula_comparison_follows_blind_pass_and_never_auto_adds_symbols(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, root = _audit_fixture(tmp_path, monkeypatch)
    comparison_path = root / "audits/m4c/formula-comparison-decisions.jsonl"
    comparisons = [json.loads(line) for line in comparison_path.read_text().splitlines()]
    comparisons[0]["compared_at"] = "2026-08-12T23:59:00Z"
    _write_jsonl(comparison_path, comparisons)
    with pytest.raises(audit.StateError, match="did not follow the blind pass"):
        audit.audit_formulas("m4b-fixture", config=config)

    comparisons[0]["compared_at"] = "2026-08-13T00:01:00Z"
    _write_jsonl(comparison_path, comparisons)
    blind_path = root / "audits/m4c/formula-blind-transcriptions.jsonl"
    blinds = [json.loads(line) for line in blind_path.read_text().splitlines()]
    blinds[0]["key_symbols_visible"] = {
        "attention_arguments": True,
        "softmax": True,
        "qk_transpose": True,
        "sqrt_d_sub_k": True,
        "closing_parenthesis": True,
        "trailing_v": False,
    }
    _write_jsonl(blind_path, blinds)
    result = audit.audit_formulas("m4b-fixture", config=config)
    first = json.loads((root / "audits/m4c/formula-audit.jsonl").read_text().splitlines()[0])
    assert result["confidence"]["low"] == 1
    assert first["audited_latex"] == "" and first["audited_status"] == "needs_review"


def test_m4c_merge_audit_override_downgrades_pending_without_changing_packet_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, files, manifest, state = m4b_fixture(tmp_path, monkeypatch)

    def context(*args: object, **kwargs: object) -> tuple[object, object, object]:
        return files, manifest, state

    monkeypatch.setattr(merge, "load_job_context", context)
    merge.merge_m4b("m4b-fixture", config=config)
    root = files.root
    state_before = (root / "analyses/packets/analysis-state.json").read_bytes()
    corrections = [
        json.loads(line)
        for line in (root / "transcript/corrections/correction-decisions.jsonl")
        .read_text()
        .splitlines()
    ]
    accepted = next(item for item in corrections if item["decision"] == "accepted")
    _write_jsonl(
        root / "audits/m4c/correction-overrides.jsonl",
        [
            {
                "correction_id": accepted["correction_id"],
                "suggested_text": accepted["suggested_text"],
                "decision": "pending",
                "confidence": "medium",
                "reason": "direct frame text is insufficient",
            }
        ],
    )
    merge.merge_m4b("m4b-fixture", config=config, force=True)
    changed = [
        json.loads(line)
        for line in (root / "transcript/corrections/correction-decisions.jsonl")
        .read_text()
        .splitlines()
    ]
    audited = next(item for item in changed if item["correction_id"] == accepted["correction_id"])
    assert audited["decision"] == "pending" and audited["applied"] is False
    assert (root / "analyses/packets/analysis-state.json").read_bytes() == state_before


def test_m4c_formula_css_is_scrollable_and_copyable() -> None:
    assert "white-space:pre" in web.APP_CSS
    assert "overflow-x:auto" in web.APP_CSS
    assert "overflow-wrap:anywhere" not in web.APP_CSS
    assert ":root[data-theme=dark]" in web.APP_CSS
    assert "URLSearchParams(location.search).get('theme')" in web.APP_JS
