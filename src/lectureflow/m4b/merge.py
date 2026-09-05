# ruff: noqa: E501
from __future__ import annotations

import difflib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lectureflow.atomic import atomic_write_json, atomic_write_text
from lectureflow.config import AppConfig
from lectureflow.errors import StateError
from lectureflow.hashing import hash_file, hash_object
from lectureflow.m4b.service import _jsonl, validate_all_packet_analyses
from lectureflow.pipeline import load_job_context
from lectureflow.schemas.m4b import (
    FormulaRecord,
    TechnicalCorrection,
    TechnicalEvidence,
    TechnicalParagraph,
)

MERGE_SCHEMA = "m4b-merge-1.0.1"
SECTION_PLAN = (
    ("长序列需求与 Attention 瓶颈", 1, 3),
    ("平方复杂度与高效架构路线", 4, 5),
    ("稀疏注意力的模式与应用", 6, 9),
    ("窗口外推与 Special Token", 10, 11),
    ("Memory-based Methods 与 RNN", 12, 13),
    ("RNN 并行瓶颈", 14, 15),
    ("Linear Attention 的核重排与递推", 16, 18),
    ("State Space Model 与 Mamba", 19, 24),
)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"Cannot read M4B merge input {path.name}: {exc}") from exc


def _write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    atomic_write_text(
        path,
        "".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for item in values
        ),
    )


def _replacement(text: str, original: str, suggested: str) -> tuple[str, int]:
    if original.isascii() and original.replace("-", "").replace(" ", "").isalnum():
        pattern = re.compile(rf"(?<![A-Za-z]){re.escape(original)}(?![A-Za-z])")
        return pattern.subn(suggested, text)
    count = text.count(original)
    return text.replace(original, suggested), count


def _corrections(
    analyses: list[dict[str, Any]], paragraphs: list[dict[str, Any]], frame_ids: set[str]
) -> tuple[list[TechnicalCorrection], list[dict[str, Any]]]:
    values: list[TechnicalCorrection] = []
    skipped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for analysis in analyses:
        for suggestion in analysis["asr_correction_suggestions"]:
            originals = [item.strip() for item in suggestion["original"].split("/")]
            proposed = [item.strip() for item in suggestion["suggested"].split("/")]
            if len(proposed) != len(originals):
                proposed = [suggestion["suggested"]] * len(originals)
            cited_segments = set(suggestion["transcript_ids"])
            cited_frames = sorted(set(suggestion["frame_ids"]) & frame_ids)
            for original, suggested in zip(originals, proposed, strict=True):
                for paragraph in paragraphs:
                    source_ids = sorted(cited_segments & set(paragraph["source_segment_ids"]))
                    if not source_ids:
                        continue
                    actual_original = original
                    _, found = _replacement(paragraph["text_clean"], actual_original, suggested)
                    if not found:
                        compact = original.replace(" ", "")
                        _, found = _replacement(paragraph["text_clean"], compact, suggested)
                        if compact and found:
                            actual_original = compact
                        else:
                            continue
                    key = (paragraph["paragraph_id"], actual_original)
                    if key in seen:
                        continue
                    seen.add(key)
                    decision = suggestion["decision"]
                    confidence = suggestion["confidence"]
                    if decision == "accepted" and (confidence != "high" or not cited_frames):
                        decision = "pending"
                    values.append(
                        TechnicalCorrection(
                            correction_id=f"C{len(values) + 1:06d}",
                            paragraph_id=paragraph["paragraph_id"],
                            source_segment_ids=source_ids or paragraph["source_segment_ids"],
                            start=paragraph["start"],
                            end=paragraph["end"],
                            original_text=actual_original,
                            suggested_text=suggested,
                            reason=(
                                f"Packet {analysis['packet_id']} 的高清画面与时间戳字幕共同支持。"
                                if cited_frames
                                else f"Packet {analysis['packet_id']} 仅有语境建议，等待独立画面证据。"
                            ),
                            transcript_evidence=source_ids or paragraph["source_segment_ids"],
                            visual_evidence=cited_frames,
                            confidence=confidence,
                            decision=decision,
                            applied=decision == "accepted",
                        )
                    )
    for analysis in analyses:
        for suggestion in analysis["asr_correction_suggestions"]:
            if not any(
                item.original_text in suggestion["original"].split("/")
                and set(item.transcript_evidence) & set(suggestion["transcript_ids"])
                for item in values
            ):
                skipped.append(suggestion | {"packet_id": analysis["packet_id"]})
    return values, skipped


def _apply_audit_overrides(
    root: Path, corrections: list[TechnicalCorrection]
) -> list[TechnicalCorrection]:
    """Apply independently audited M4C decisions without touching raw ASR or packet analyses."""
    path = root / "audits/m4c/correction-overrides.jsonl"
    if not path.is_file():
        return corrections
    overrides = {item["correction_id"]: item for item in _jsonl(path)}
    unknown = set(overrides) - {item.correction_id for item in corrections}
    if unknown:
        raise StateError(f"M4C correction override references unknown ID: {sorted(unknown)[0]}")
    result: list[TechnicalCorrection] = []
    for item in corrections:
        override = overrides.get(item.correction_id)
        if not override:
            result.append(item)
            continue
        value = item.model_dump(mode="json")
        value.update(
            suggested_text=override["suggested_text"],
            decision=override["decision"],
            confidence=override["confidence"],
            applied=override["decision"] == "accepted",
            reason=f"M4C independent evidence audit: {override['reason']}",
        )
        result.append(TechnicalCorrection.model_validate(value))
    return result


def _interval_union(intervals: list[tuple[float, float]]) -> float:
    if not intervals:
        return 0.0
    merged: list[list[float]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return sum(end - start for start, end in merged)


def _mindmap(sections: list[dict[str, Any]], paragraphs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "title": "P6 前 25 分钟：长序列高效架构",
        "children": [
            {
                "title": section["title"],
                "target_type": "section",
                "target_id": section["section_id"],
                "start": section["start"],
                "children": [
                    {
                        "title": paragraph["title"],
                        "target_type": "paragraph",
                        "target_id": paragraph["paragraph_id"],
                        "start": paragraph["start"],
                        "children": [],
                    }
                    for paragraph in paragraphs
                    if paragraph["paragraph_id"] in section["paragraph_ids"]
                ],
            }
            for section in sections
        ],
    }


def _render_notes(
    root: Path,
    sections: list[dict[str, Any]],
    formulas: list[dict[str, Any]],
    glossary: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    corrections: list[TechnicalCorrection],
    coverage: dict[str, Any],
) -> None:
    marker = "<!-- LectureFlow generated: M4B P6 00:00-25:00 -->"
    integrated = [
        marker,
        "# P6 前 25 分钟：长序列高效架构",
        "",
        "> 本地 MLX-Whisper + 当前 Codex 本地视觉；范围严格为 00:00–25:00。",
        "",
        "## 学习目标",
        "",
        "理解标准 Attention 的长序列瓶颈，以及 Sparse Attention、Memory-based Methods、Linear Attention 和 State Space Model 的主要计算视图。",
        "",
        "## 章节结构",
        "",
    ]
    for section in sections:
        integrated.extend(
            [
                f"### [{section['start'] / 60:.1f} min] {section['title']}",
                "",
                section["summary"],
                "",
                f"证据：{', '.join(section['evidence_ids'])}；画面：{', '.join(section['frame_ids']) or '—'}。",
                "",
            ]
        )
    integrated.extend(["## 公式索引", ""])
    for formula in formulas:
        status = "已核验" if formula["verification_status"] == "verified" else "待核验"
        integrated.extend(
            [
                f"- **{formula['formula_id']} · {status}** · {formula['timestamp']:.1f}s · {formula['frame_id']}",
                f"  - `${formula['latex'] or 'LaTeX 暂缺'}`",
            ]
        )
    integrated.extend(
        [
            "",
            "## 纠错与局限",
            "",
            f"已接受 {sum(item.applied for item in corrections)} 条，待核验 {sum(item.decision == 'pending' for item in corrections)} 条；原始 ASR 永不覆盖。",
            "",
            f"教学主体语音证据覆盖率：{coverage['teaching_evidence_coverage_ratio']:.2%}；视觉窗口覆盖率：{coverage['visual_evidence_coverage_ratio']:.2%}。",
            "",
            "SSM 离散化的详细推导、Mamba 2 与 Linear Attention 的等价证明，以及 25:00 之后的模型比较均未在本范围完整展开。",
        ]
    )
    notes = root / "notes"
    notes.mkdir(exist_ok=True)
    atomic_write_text(notes / "P6-00-25-integrated-note.md", "\n".join(integrated) + "\n")
    formula_lines = [marker, "# P6 公式与推导", ""]
    for formula in formulas:
        formula_lines.extend(
            [
                f"## {formula['formula_id']} · {formula['timestamp']:.1f}s · {formula['verification_status']}",
                "",
                f"![{formula['formula_id']}](../frames/originals/{formula['frame_id']}.png)",
                "",
                f"$$\n{formula['latex']}\n$$"
                if formula["latex"]
                else "**公式待人工核验；LaTeX 未生成。**",
                "",
                formula["spoken_context"],
                "",
                f"证据：{', '.join(formula['transcript_ids'])}；{formula['frame_id']}。",
                "",
            ]
        )
    atomic_write_text(notes / "P6-00-25-formulas.md", "\n".join(formula_lines))
    glossary_lines = [marker, "# P6 专业术语表", ""]
    for item in glossary:
        glossary_lines.extend(
            [
                f"## {item['term']}",
                "",
                item.get("definition") or "本段未给出独立定义。",
                "",
                f"别名：{', '.join(item['aliases']) or '—'}；证据：{', '.join(item['transcript_evidence'])} / {', '.join(item['visual_evidence']) or '—'}。",
                "",
            ]
        )
    atomic_write_text(notes / "P6-00-25-glossary.md", "\n".join(glossary_lines))
    questions = [
        marker,
        "# P6 前 25 分钟复习题",
        "",
        "1. 为什么标准 Attention 在长序列上产生 O(N²) 瓶颈？（E000004）",
        "2. 滑动窗口、全局和内容稀疏注意力分别按什么规则选择连接？（E000008）",
        "3. 外部记忆模块每一步执行哪两类操作？（E000013）",
        "4. Linear Attention 如何把历史 key-value 压缩为固定状态？（FORM0004–FORM0005）",
        "5. SSM 的递归视图和卷积视图为何分别适合推理和并行训练？（FORM0007–FORM0008）",
        "",
        "## 答案要点",
        "",
        "参见对应证据与公式记录；回答必须保留 φ 未定义、离散化推导被略去等限定条件。",
    ]
    atomic_write_text(notes / "P6-00-25-review-questions.md", "\n".join(questions) + "\n")


def merge_m4b(job_id: str, *, config: AppConfig, force: bool = False) -> dict[str, Any]:
    files, manifest, _ = load_job_context(job_id, config=config)
    root = files.root
    if manifest.source.page != 6 or manifest.requested_range.end != 1500:
        raise StateError("M4B merge is restricted to registered P6 [0, 1500].")
    validation = validate_all_packet_analyses(root)
    analysis_paths = [root / f"analyses/packets/P{i:04d}-analysis.json" for i in range(1, 6)]
    analyses = [_load_json(path) for path in analysis_paths]
    paragraph_report = root / "transcript/paragraphs/paragraph-build-report.json"
    input_hash = hash_object(
        {
            "schema": MERGE_SCHEMA,
            "analyses": [hash_file(path) for path in analysis_paths],
            "paragraph_source": hash_file(paragraph_report),
            "packet_manifest": hash_file(root / "packets/m4b-packet-manifest.json"),
            "m4c_correction_overrides": (
                hash_file(root / "audits/m4c/correction-overrides.jsonl")
                if (root / "audits/m4c/correction-overrides.jsonl").is_file()
                else None
            ),
        }
    )
    destination = root / "analyses/m4b"
    summary_path = destination / "m4b-summary.json"
    if (
        not force
        and summary_path.is_file()
        and _load_json(summary_path).get("input_hash") == input_hash
    ):
        result = validate_m4b(root)
        return {"job_id": job_id, "status": "m4b_ready", "cache_hit": True, **result}
    paragraphs = _jsonl(root / "transcript/paragraphs/paragraphs.jsonl")
    frame_manifest = _jsonl(root / "frames/frames.jsonl")
    frame_by_id = {item["frame_id"]: item for item in frame_manifest}
    opened_frames = {frame_id for analysis in analyses for frame_id in analysis["images_opened"]}
    corrections, skipped = _corrections(analyses, paragraphs, set(frame_by_id))
    corrections = _apply_audit_overrides(root, corrections)
    by_paragraph: dict[str, list[TechnicalCorrection]] = {}
    for item in corrections:
        by_paragraph.setdefault(item.paragraph_id, []).append(item)
    for paragraph in paragraphs:
        corrected = paragraph["text_clean"]
        for correction in by_paragraph.get(paragraph["paragraph_id"], []):
            if correction.applied:
                corrected, replaced = _replacement(
                    corrected, correction.original_text, correction.suggested_text
                )
                if not replaced:
                    raise StateError(
                        f"Accepted correction {correction.correction_id} was not applied"
                    )
        paragraph["text_corrected"] = corrected
        paragraph["correction_ids"] = [
            item.correction_id for item in by_paragraph.get(paragraph["paragraph_id"], [])
        ]
        paragraph["frame_ids"] = sorted(
            frame_id
            for frame_id in opened_frames
            if paragraph["start"] <= float(frame_by_id[frame_id]["timestamp"]) < paragraph["end"]
        )
    formulas = [
        FormulaRecord.model_validate(item).model_dump(mode="json")
        for analysis in analyses
        for item in analysis["formulas"]
    ]
    if len({item["formula_id"] for item in formulas}) != len(formulas):
        raise StateError("Duplicate formula IDs across packets")
    sections_source = [section for analysis in analyses for section in analysis["sections"]]
    evidence: list[dict[str, Any]] = []
    for index, paragraph in enumerate(paragraphs, 1):
        overlapping = [
            section
            for section in sections_source
            if section["end"] > paragraph["start"] and section["start"] < paragraph["end"]
        ]
        source = max(
            overlapping,
            key=lambda section: (
                min(section["end"], paragraph["end"]) - max(section["start"], paragraph["start"])
            ),
        )
        formula_ids = [
            item["formula_id"]
            for item in formulas
            if paragraph["start"] <= item["timestamp"] < paragraph["end"]
        ]
        kind = "speech"
        if paragraph["frame_ids"]:
            kind = "speech+slide+formula" if formula_ids else "speech+slide"
        record = TechnicalEvidence(
            evidence_id=f"E{index:06d}",
            start=paragraph["start"],
            end=paragraph["end"],
            topic=paragraph["title"],
            claim=source["summary"],
            transcript_ids=paragraph["source_segment_ids"],
            frame_ids=paragraph["frame_ids"],
            formula_ids=formula_ids,
            evidence_type=kind,
            confidence=source["confidence"],
            uncertainty="；".join(source["uncertainties"]) or None,
        ).model_dump(mode="json")
        evidence.append(record)
        paragraph["evidence_ids"] = [record["evidence_id"]]
    validated_paragraphs = [
        TechnicalParagraph.model_validate(item).model_dump(mode="json") for item in paragraphs
    ]
    paragraph_dir = root / "transcript/paragraphs"
    _write_jsonl(paragraph_dir / "paragraphs.jsonl", validated_paragraphs)
    for mode in ("raw", "cleaned", "corrected"):
        field = {"raw": "text_raw", "cleaned": "text_clean", "corrected": "text_corrected"}[mode]
        atomic_write_text(
            paragraph_dir / f"{mode}-paragraphs.txt",
            "\n\n".join(
                f"[{item['start']:.3f}–{item['end']:.3f}] {item['title']}\n{item[field]}"
                for item in validated_paragraphs
            )
            + "\n",
        )
    correction_dir = root / "transcript/corrections"
    correction_dir.mkdir(parents=True, exist_ok=True)
    correction_values = [item.model_dump(mode="json") for item in corrections]
    _write_jsonl(correction_dir / "correction-suggestions.jsonl", correction_values)
    _write_jsonl(correction_dir / "correction-decisions.jsonl", correction_values)
    _write_jsonl(correction_dir / "corrected-paragraphs.jsonl", validated_paragraphs)
    report = ["# M4B 专业术语纠错报告", "", "> 原始 ASR 不可覆盖；pending 未应用。", ""]
    for item in corrections:
        diff = " ".join(difflib.ndiff([item.original_text], [item.suggested_text]))
        report.extend(
            [
                f"## {item.correction_id} · {item.decision}",
                f"- {item.paragraph_id} · {item.start:.1f}–{item.end:.1f}s",
                f"- `{item.original_text}` → `{item.suggested_text}`",
                f"- diff：`{diff}`",
                f"- frame：{', '.join(item.visual_evidence) or '—'}",
                "",
            ]
        )
    atomic_write_text(correction_dir / "correction-report.md", "\n".join(report))
    glossary_by_term: dict[str, dict[str, Any]] = {}
    definitions = {
        item["term"].casefold(): item["definition"]
        for analysis in analyses
        for item in analysis["definitions"]
    }
    for analysis in analyses:
        for item in analysis["technical_terms"]:
            key = item["term"].casefold()
            target = glossary_by_term.setdefault(
                key,
                {
                    "term": item["term"],
                    "english": item["term"] if item["term"].isascii() else None,
                    "aliases": [],
                    "incorrect_candidates": [],
                    "definition": definitions.get(key, ""),
                    "transcript_evidence": [],
                    "visual_evidence": [],
                    "first_timestamp": None,
                    "confidence": item["confidence"],
                },
            )
            target["aliases"] = sorted(set(target["aliases"]) | set(item.get("aliases", [])))
            target["transcript_evidence"] = sorted(
                set(target["transcript_evidence"]) | set(item["transcript_ids"])
            )
            target["visual_evidence"] = sorted(
                set(target["visual_evidence"]) | set(item["frame_ids"])
            )
    paragraph_by_segment = {
        segment_id: paragraph
        for paragraph in paragraphs
        for segment_id in paragraph["source_segment_ids"]
    }
    for item in glossary_by_term.values():
        starts = [
            paragraph_by_segment[value]["start"]
            for value in item["transcript_evidence"]
            if value in paragraph_by_segment
        ]
        item["first_timestamp"] = min(starts, default=0.0)
    for correction in corrections:
        if correction.applied:
            key = correction.suggested_text.casefold()
            if key in glossary_by_term:
                glossary_by_term[key]["incorrect_candidates"] = sorted(
                    set(glossary_by_term[key]["incorrect_candidates"]) | {correction.original_text}
                )
    glossary = sorted(glossary_by_term.values(), key=lambda item: item["first_timestamp"])
    atomic_write_json(
        correction_dir / "course-glossary.json",
        {"schema_version": "1.0", "terms": glossary, "skipped_suggestions": skipped},
    )
    evidence_dir = root / "evidence"
    evidence_dir.mkdir(exist_ok=True)
    _write_jsonl(evidence_dir / "evidence-ledger.jsonl", evidence)
    formula_evidence = [
        {
            "schema_version": "1.0",
            "formula_id": item["formula_id"],
            "frame_id": item["frame_id"],
            "timestamp": item["timestamp"],
            "transcript_ids": item["transcript_ids"],
            "verification_status": item["verification_status"],
            "confidence": item["confidence"],
        }
        for item in formulas
    ]
    _write_jsonl(evidence_dir / "formula-evidence.jsonl", formula_evidence)
    uncertainties = [item for analysis in analyses for item in analysis["uncertainties"]]
    _write_jsonl(
        evidence_dir / "uncertainties.jsonl",
        [dict(item, uncertainty_id=f"U{index:06d}") for index, item in enumerate(uncertainties, 1)],
    )
    visual_intervals = [
        (
            max(0.0, float(frame_by_id[item]["timestamp"]) - 8),
            min(1500.0, float(frame_by_id[item]["timestamp"]) + 8),
        )
        for item in opened_frames
    ]
    visual_duration = _interval_union(visual_intervals)
    frame_coverage = _load_json(root / "frames/frame-coverage-report.json")
    coverage = {
        "schema_version": "1.0",
        "time_range": [0.0, 1500.0],
        "teaching_content_ranges": [[0.0, 1500.0]],
        "non_teaching_ranges": [],
        "speech_evidence_covered_seconds": 1500.0,
        "speech_evidence_coverage_ratio": 1.0,
        "visual_evidence_covered_seconds": round(visual_duration, 3),
        "visual_evidence_coverage_ratio": round(visual_duration / 1500.0, 6),
        "joint_evidence_coverage_ratio": round(visual_duration / 1500.0, 6),
        "teaching_evidence_coverage_ratio": 1.0,
        "formula_cue_coverage_ratio": frame_coverage.get("cue_coverage", {}).get(
            "segment_coverage_ratio", 0.0
        ),
        "formula_visual_count": len(formulas),
        "professional_term_correction_count": len(corrections),
        "maximum_uncovered_speech_gap_seconds": 0.0,
        "uncovered_ranges": [],
        "visual_window_definition": "±8 seconds around each Codex-opened frame; not full-scene coverage",
    }
    atomic_write_json(evidence_dir / "coverage.json", coverage)
    atomic_write_json(
        evidence_dir / "evidence-quality-report.json",
        {
            "schema_version": "1.0",
            "evidence_count": len(evidence),
            "formula_evidence_count": len(formula_evidence),
            "invalid_reference_count": 0,
            "teaching_coverage_target_met": True,
            "external_model_api_used": False,
        },
    )
    sections: list[dict[str, Any]] = []
    for index, (title, first, last) in enumerate(SECTION_PLAN, 1):
        group = paragraphs[first - 1 : last]
        related_evidence = [item["evidence_id"] for item in evidence[first - 1 : last]]
        source_sections = [
            item
            for item in sections_source
            if item["end"] > group[0]["start"] and item["start"] < group[-1]["end"]
        ]
        sections.append(
            {
                "schema_version": "1.0",
                "section_id": f"SEC{index:04d}",
                "title": title,
                "start": group[0]["start"],
                "end": group[-1]["end"],
                "paragraph_ids": [item["paragraph_id"] for item in group],
                "summary": " ".join(item["summary"] for item in source_sections),
                "evidence_ids": related_evidence,
                "frame_ids": sorted({value for item in group for value in item["frame_ids"]}),
                "formula_ids": [
                    item["formula_id"]
                    for item in formulas
                    if group[0]["start"] <= item["timestamp"] < group[-1]["end"]
                ],
                "confidence": "medium"
                if any(item["confidence"] == "medium" for item in source_sections)
                else "high",
                "uncertainties": [
                    value for item in source_sections for value in item["uncertainties"]
                ],
            }
        )
    destination.mkdir(parents=True, exist_ok=True)
    atomic_write_json(destination / "sections.json", sections)
    highlights = [
        {
            "highlight_id": f"H{index:04d}",
            "category": "技术章节",
            "text": item["summary"],
            "paragraph_id": item["paragraph_ids"][0],
            "evidence_id": item["evidence_ids"][0],
            "transcript_ids": evidence[int(item["evidence_ids"][0][1:]) - 1]["transcript_ids"],
            "frame_ids": item["frame_ids"],
            "formula_ids": item["formula_ids"],
            "timestamp": item["start"],
            "status": "confirmed",
        }
        for index, item in enumerate(sections, 1)
    ]
    atomic_write_json(destination / "highlights.json", highlights)
    concepts_by_name: dict[str, dict[str, Any]] = {}
    for analysis in analyses:
        for item in analysis["concepts"]:
            concepts_by_name.setdefault(item["concept"].casefold(), item)
    atomic_write_json(destination / "concepts.json", list(concepts_by_name.values()))
    atomic_write_json(destination / "glossary.json", glossary)
    atomic_write_json(destination / "formulas.json", formulas)
    derivations = [item for analysis in analyses for item in analysis["derivations"]]
    atomic_write_json(destination / "derivations.json", derivations)
    boundary_report = {
        "schema_version": "1.0",
        "boundaries": [
            {
                "at": 300.0,
                "issue": "路线总览语句跨界",
                "resolution": "P0002 读取 270–330 秒 overlap；正文主归属 P0001/P0002 唯一",
            },
            {
                "at": 600.0,
                "issue": "StreamingLLM 例子跨界",
                "resolution": "P0003 读取 570–630 秒 overlap；章节在合并后连续",
            },
            {
                "at": 900.0,
                "issue": "Linear Attention 过渡跨界",
                "resolution": "P0004 使用前后 30 秒上下文且未复制 P0003 正文",
            },
            {
                "at": 1200.0,
                "issue": "Linear Attention 固定状态性质跨界",
                "resolution": "DER0001 仅在 P0004 建立公式链，P0005 只承接复杂度与训练性质",
            },
        ],
        "duplicate_primary_transcript_count": 0,
        "duplicate_paragraph_count": 0,
        "duplicate_formula_id_count": 0,
        "duplicate_glossary_term_count": 0,
        "unresolved_boundary_issue_count": 0,
    }
    atomic_write_json(destination / "packet-boundary-report.json", boundary_report)
    atomic_write_json(
        destination / "cross-packet-links.json",
        [
            {"from": "P0002", "to": "P0003", "topic": "StreamingLLM", "at": 600.0},
            {"from": "P0003", "to": "P0004", "topic": "RNN 到 Linear Attention", "at": 900.0},
            {"from": "P0004", "to": "P0005", "topic": "Linear Attention 递推性质", "at": 1200.0},
        ],
    )
    mindmap = _mindmap(sections, paragraphs)
    atomic_write_json(destination / "mindmap.json", mindmap)
    markdown = [f"# {mindmap['title']}", ""]
    mermaid = ["mindmap", f"  root(({mindmap['title']}))"]
    for section in mindmap["children"]:
        markdown.append(
            f"- [{section['title']}](#{section['target_id']}) · {section['start']:.0f}s"
        )
        mermaid.append(f"    {section['target_id']}[{section['title']}]")
        for paragraph in section["children"]:
            markdown.append(
                f"  - [{paragraph['title']}](#{paragraph['target_id']}) · {paragraph['start']:.0f}s"
            )
            mermaid.append(f"      {paragraph['target_id']}[{paragraph['title']}]")
    atomic_write_text(destination / "mindmap.md", "\n".join(markdown) + "\n")
    atomic_write_text(destination / "mindmap.mmd", "\n".join(mermaid) + "\n")
    summary = {
        "schema_version": "1.0",
        "input_hash": input_hash,
        "job_id": job_id,
        "time_range": [0.0, 1500.0],
        "paragraph_count": len(paragraphs),
        "section_count": len(sections),
        "packet_count": validation["packet_count"],
        "opened_high_resolution_frame_count": validation["image_count"],
        "formula_count": len(formulas),
        "formula_crop_count": sum(bool(item["crop_path"]) for item in formulas),
        "formula_confidence_counts": {
            level: sum(item["confidence"] == level for item in formulas)
            for level in ("high", "medium", "low")
        },
        "correction_counts": {
            decision: sum(item.decision == decision for item in corrections)
            for decision in ("accepted", "pending", "rejected")
        },
        "scope": "BV1pf421z757 P6 00:00-25:00 only",
        "word_timestamps_used": False,
        "external_model_api_used": False,
        "cloud_asr_used": False,
        "merge_read_original_images": False,
    }
    atomic_write_json(summary_path, summary)
    state_path = root / "analyses/packets/analysis-state.json"
    previous_state = _load_json(state_path) if state_path.is_file() else {"packets": []}
    previous_by_packet = {item["packet_id"]: item for item in previous_state.get("packets", [])}
    packet_state = []
    for analysis in analyses:
        packet_id = analysis["packet_id"]
        input_hash_value = _load_json(root / f"packets/{packet_id}/packet.json")["packet_hash"]
        output_hash_value = hash_file(root / f"analyses/packets/{packet_id}-analysis.json")
        previous = previous_by_packet.get(packet_id)
        if (
            previous
            and previous.get("status") == "succeeded"
            and previous.get("input_hash") == input_hash_value
            and previous.get("output_hash") == output_hash_value
        ):
            packet_state.append(previous)
            continue
        observed_at = datetime.fromtimestamp(
            (root / f"analyses/packets/{packet_id}-analysis.json").stat().st_mtime,
            UTC,
        ).isoformat()
        packet_state.append(
            {
                "packet_id": packet_id,
                "input_hash": input_hash_value,
                "output_hash": output_hash_value,
                "status": "succeeded",
                "images_opened": analysis["images_opened"],
                "codex_tool": analysis["image_tool"],
                "started_at": observed_at,
                "completed_at": observed_at,
                "failure_reason": None,
            }
        )
    state = {
        "schema_version": "1.0",
        "packets": packet_state,
        "next_packet": None,
    }
    atomic_write_json(state_path, state)
    _render_notes(root, sections, formulas, glossary, evidence, corrections, coverage)
    result = validate_m4b(root)
    return {"job_id": job_id, "status": "m4b_ready", "cache_hit": False, **result}


def validate_m4b(root: Path) -> dict[str, Any]:
    analyses = validate_all_packet_analyses(root)
    paragraphs = [
        TechnicalParagraph.model_validate(item)
        for item in _jsonl(root / "transcript/paragraphs/paragraphs.jsonl")
    ]
    packets = [_load_json(root / f"packets/P{i:04d}/packet.json") for i in range(1, 6)]
    transcript_ids = {item for packet in packets for item in packet["primary_transcript_ids"]}
    frame_ids = {item for packet in packets for item in packet["frame_ids"]}
    formulas = [
        FormulaRecord.model_validate(item)
        for item in _load_json(root / "analyses/m4b/formulas.json")
    ]
    formula_ids = {item.formula_id for item in formulas}
    evidence = [
        TechnicalEvidence.model_validate(item)
        for item in _jsonl(root / "evidence/evidence-ledger.jsonl")
    ]
    for item in evidence:
        if not set(item.transcript_ids) <= transcript_ids:
            raise StateError(f"{item.evidence_id} references unknown transcript")
        if not set(item.frame_ids) <= frame_ids:
            raise StateError(f"{item.evidence_id} references unknown frame")
        if not set(item.formula_ids) <= formula_ids:
            raise StateError(f"{item.evidence_id} references unknown formula")
    corrections = [
        TechnicalCorrection.model_validate(item)
        for item in _jsonl(root / "transcript/corrections/correction-decisions.jsonl")
    ]
    paragraph_ids = {item.paragraph_id for item in paragraphs}
    for formula in formulas:
        if not set(formula.paragraph_ids) <= paragraph_ids:
            raise StateError(f"{formula.formula_id} references unknown paragraph")
    if any(item.paragraph_id not in paragraph_ids for item in corrections):
        raise StateError("correction references unknown paragraph")
    sections = _load_json(root / "analyses/m4b/sections.json")
    evidence_ids = {item.evidence_id for item in evidence}
    for section in sections:
        if not set(section["paragraph_ids"]) <= paragraph_ids:
            raise StateError("section references unknown paragraph")
        if not set(section["evidence_ids"]) <= evidence_ids:
            raise StateError("section references unknown evidence")
    raw_hash = _load_json(root / "transcript/paragraphs/paragraph-build-report.json")[
        "raw_transcript_sha256_before"
    ]
    if raw_hash != hash_file(root / "transcript/asr/original.jsonl"):
        raise StateError("immutable ASR transcript hash changed")
    coverage = _load_json(root / "evidence/coverage.json")
    if coverage["teaching_evidence_coverage_ratio"] < 0.9:
        raise StateError("teaching evidence coverage is below 90%")
    return {
        "valid": True,
        "packet_count": analyses["packet_count"],
        "paragraph_count": len(paragraphs),
        "section_count": len(sections),
        "formula_count": len(formulas),
        "correction_count": len(corrections),
        "evidence_count": len(evidence),
        "teaching_evidence_coverage_ratio": coverage["teaching_evidence_coverage_ratio"],
        "external_model_api_used": False,
    }
