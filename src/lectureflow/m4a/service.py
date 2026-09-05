from __future__ import annotations

import difflib
import json
import re
from itertools import pairwise
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from lectureflow.atomic import atomic_write_json, atomic_write_text
from lectureflow.config import AppConfig
from lectureflow.errors import StateError
from lectureflow.hashing import hash_file, hash_object
from lectureflow.pipeline import load_job_context
from lectureflow.schemas.m4a import Correction, Highlight, MindMap, MindMapNode, Paragraph, Section
from lectureflow.schemas.transcript import TranscriptSegment

PARAGRAPH_SCHEMA = "paragraphs-1.0.0"
CORRECTION_SCHEMA = "corrections-1.0.0"
RENDER_SCHEMA = "m4a-render-1.0.0"


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"Cannot read M4A JSON {path.name}: {error}") from error


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"Cannot read M4A JSONL {path.name}: {error}") from error


def _jsonl(values: list[Any]) -> str:
    return "".join(
        json.dumps(
            value.model_dump(mode="json") if hasattr(value, "model_dump") else value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        for value in values
    )


def _read_segments(root: Path) -> list[TranscriptSegment]:
    try:
        return [
            TranscriptSegment.model_validate(item)
            for item in _load_jsonl(root / "transcript/asr/original.jsonl")
        ]
    except ValidationError as error:
        raise StateError(f"Invalid immutable ASR segment: {error}") from error


def _normalize_readable(text: str) -> str:
    text = text.replace(",", "，").replace(";", "；").replace(":", "：")
    text = re.sub(r"[ \t]+", "", text)
    text = re.sub(r"([，。！？；：])\1+", r"\1", text)
    return text.strip()


def _read_corrections(root: Path) -> list[Correction]:
    path = root / "transcript/corrections/correction-decisions.jsonl"
    if not path.is_file():
        return []
    try:
        return [Correction.model_validate(item) for item in _load_jsonl(path)]
    except ValidationError as error:
        raise StateError(f"Invalid correction decision: {error}") from error


def _paragraph_input_hash(root: Path) -> str:
    plan = root / "analyses/m4a/paragraph-plan.json"
    correction = root / "transcript/corrections/correction-decisions.jsonl"
    return hash_object(
        {
            "schema": PARAGRAPH_SCHEMA,
            "original": hash_file(root / "transcript/asr/original.jsonl"),
            "analysis": hash_file(root / "analyses/P0001-analysis.json"),
            "evidence": hash_file(root / "evidence/evidence-ledger.jsonl"),
            "plan": hash_file(plan),
            "corrections": hash_file(correction) if correction.is_file() else None,
        }
    )


def validate_paragraphs(root: Path) -> dict[str, Any]:
    segments = _read_segments(root)
    segment_by_id = {item.segment_id: item for item in segments}
    try:
        paragraphs = [
            Paragraph.model_validate(item)
            for item in _load_jsonl(root / "transcript/paragraphs/paragraphs.jsonl")
        ]
    except ValidationError as error:
        raise StateError(f"Invalid paragraph: {error}") from error
    if not paragraphs:
        raise StateError("Paragraph timeline is empty.")
    positions = {item.segment_id: index for index, item in enumerate(segments)}
    assignments: list[str] = []
    previous_start = -1.0
    for paragraph in paragraphs:
        unknown = set(paragraph.source_segment_ids) - set(segment_by_id)
        if unknown:
            raise StateError(f"Paragraph cites unknown segment IDs: {sorted(unknown)}")
        indexes = [positions[item] for item in paragraph.source_segment_ids]
        if indexes != list(range(indexes[0], indexes[-1] + 1)):
            raise StateError(f"Paragraph {paragraph.paragraph_id} merges non-contiguous segments.")
        first, last = (
            segment_by_id[paragraph.source_segment_ids[0]],
            segment_by_id[paragraph.source_segment_ids[-1]],
        )
        if paragraph.start != first.start or paragraph.end != last.end:
            raise StateError(
                f"Paragraph {paragraph.paragraph_id} time does not match source segments."
            )
        expected_raw = "".join(
            segment_by_id[item].text_raw for item in paragraph.source_segment_ids
        )
        if paragraph.text_raw != expected_raw:
            raise StateError(
                f"Paragraph {paragraph.paragraph_id} raw text is not immutable source text."
            )
        if paragraph.start < previous_start:
            raise StateError("Paragraph timeline is out of order.")
        previous_start = paragraph.start
        if not paragraph.overlap_context:
            assignments.extend(paragraph.source_segment_ids)
    missing = sorted(set(segment_by_id) - set(assignments))
    duplicate = sorted(item for item in set(assignments) if assignments.count(item) > 1)
    if missing or duplicate:
        raise StateError(
            f"Paragraph segment coverage failed; missing={missing}, duplicate={duplicate}."
        )
    original = root / "transcript/asr/original.jsonl"
    report = _load_json(root / "transcript/paragraphs/paragraph-build-report.json")
    if report.get("raw_transcript_sha256_before") != hash_file(original):
        raise StateError("Immutable raw transcript hash changed after paragraph build.")
    return {
        "schema_version": "1.0",
        "valid": True,
        "paragraph_count": len(paragraphs),
        "segment_count": len(segments),
        "missing_segments": missing,
        "duplicate_segments": duplicate,
    }


def build_paragraphs(job_id: str, *, config: AppConfig, force: bool = False) -> dict[str, Any]:
    files, _, _ = load_job_context(job_id, config=config)
    root = files.root
    plan_path = root / "analyses/m4a/paragraph-plan.json"
    if not plan_path.is_file():
        raise StateError(
            "M4A paragraph plan is missing; current Codex must write the JSON plan first."
        )
    input_hash = _paragraph_input_hash(root)
    report_path = root / "transcript/paragraphs/paragraph-build-report.json"
    if not force and report_path.is_file():
        report = _load_json(report_path)
        if report.get("input_hash") == input_hash:
            validate_paragraphs(root)
            return {"job_id": job_id, "status": "paragraphs_ready", "cache_hit": True, **report}
    original_path = root / "transcript/asr/original.jsonl"
    raw_hash = hash_file(original_path)
    segments = _read_segments(root)
    segment_by_id = {item.segment_id: item for item in segments}
    evidence = _load_jsonl(root / "evidence/evidence-ledger.jsonl")
    frames = {item["frame_id"] for item in _load_jsonl(files.frames_jsonl)}
    plan = _load_json(plan_path)
    if plan.get("schema_version") != "1.0" or not isinstance(plan.get("paragraphs"), list):
        raise StateError("Paragraph plan must be a Schema 1.0 object with paragraphs.")
    corrections = _read_corrections(root)
    paragraphs: list[Paragraph] = []
    for index, proposed in enumerate(plan["paragraphs"], 1):
        source_ids = proposed.get("source_segment_ids", [])
        if not source_ids or any(item not in segment_by_id for item in source_ids):
            raise StateError(f"Paragraph plan item {index} has missing/unknown source segments.")
        first, last = segment_by_id[source_ids[0]], segment_by_id[source_ids[-1]]
        raw = "".join(segment_by_id[item].text_raw for item in source_ids)
        clean = proposed.get("text_clean") or _normalize_readable(
            "".join(segment_by_id[item].text_clean for item in source_ids)
        )
        related_evidence = [
            item for item in evidence if set(item["transcript_ids"]) & set(source_ids)
        ]
        paragraph_id = f"PAR{index:04d}"
        related_corrections = [item for item in corrections if item.paragraph_id == paragraph_id]
        corrected = clean
        for correction in related_corrections:
            if correction.applied:
                if correction.original_text not in corrected:
                    raise StateError(
                        f"Accepted correction {correction.correction_id} text is absent "
                        "from paragraph."
                    )
                corrected = corrected.replace(
                    correction.original_text, correction.suggested_text, 1
                )
        frame_ids = sorted(
            {
                frame_id
                for item in related_evidence
                for frame_id in item["frame_ids"]
                if frame_id in frames
            }
        )
        paragraphs.append(
            Paragraph(
                paragraph_id=paragraph_id,
                start=first.start,
                end=last.end,
                title=str(proposed.get("title", "")).strip(),
                text_raw=raw,
                text_clean=clean,
                text_corrected=corrected,
                source_segment_ids=source_ids,
                correction_ids=[item.correction_id for item in related_corrections],
                evidence_ids=[item["evidence_id"] for item in related_evidence],
                frame_ids=frame_ids,
                confidence=proposed.get("confidence", "medium"),
                uncertainties=list(proposed.get("uncertainties", [])),
            )
        )
    destination = root / "transcript/paragraphs"
    destination.mkdir(parents=True, exist_ok=True)
    atomic_write_text(destination / "paragraphs.jsonl", _jsonl(paragraphs))
    atomic_write_text(
        destination / "raw-paragraphs.txt", "\n\n".join(x.text_raw for x in paragraphs) + "\n"
    )
    atomic_write_text(
        destination / "cleaned-paragraphs.txt", "\n\n".join(x.text_clean for x in paragraphs) + "\n"
    )
    atomic_write_text(
        destination / "corrected-paragraphs.txt",
        "\n\n".join(x.text_corrected for x in paragraphs) + "\n",
    )
    durations = [item.end - item.start for item in paragraphs]
    assignments = [item for paragraph in paragraphs for item in paragraph.source_segment_ids]
    gaps = [max(0.0, current.start - previous.end) for previous, current in pairwise(paragraphs)]
    quality = {
        "schema_version": "1.0",
        "source_segment_count": len(segments),
        "paragraph_count": len(paragraphs),
        "average_duration_seconds": round(sum(durations) / len(durations), 3),
        "longest_duration_seconds": max(durations),
        "shortest_duration_seconds": min(durations),
        "average_character_count": round(
            sum(len(item.text_clean) for item in paragraphs) / len(paragraphs), 3
        ),
        "over_75_seconds": sum(item > 75 for item in durations),
        "over_90_seconds": sum(item > 90 for item in durations),
        "paragraphs_without_source": sum(not item.source_segment_ids for item in paragraphs),
        "missing_segment_count": len(set(segment_by_id) - set(assignments)),
        "duplicate_segment_count": len(assignments) - len(set(assignments)),
        "invalid_time_count": sum(item.end <= item.start for item in paragraphs),
        "reverse_order_count": sum(
            current.start < previous.start for previous, current in pairwise(paragraphs)
        ),
        "maximum_gap_seconds": max(gaps, default=0.0),
        "paragraphs_with_corrections": sum(bool(item.correction_ids) for item in paragraphs),
        "paragraphs_with_visual_evidence": sum(bool(item.frame_ids) for item in paragraphs),
        "paragraphs_with_speech_evidence": sum(bool(item.evidence_ids) for item in paragraphs),
        "raw_transcript_sha256": raw_hash,
    }
    atomic_write_json(destination / "paragraph-quality-report.json", quality)
    artifacts = {
        str(path.relative_to(root)): hash_file(path)
        for path in (
            destination / "paragraphs.jsonl",
            destination / "raw-paragraphs.txt",
            destination / "cleaned-paragraphs.txt",
            destination / "corrected-paragraphs.txt",
            destination / "paragraph-quality-report.json",
        )
    }
    report = {
        "schema_version": "1.0",
        "input_hash": input_hash,
        "paragraph_count": len(paragraphs),
        "source_segment_count": len(segments),
        "raw_transcript_sha256_before": raw_hash,
        "raw_transcript_sha256_after": hash_file(original_path),
        "content_level_cleaning_recorded": any(
            item.text_raw != item.text_clean for item in paragraphs
        ),
        "cleaning_diffs": [
            {
                "paragraph_id": item.paragraph_id,
                "diff": list(
                    difflib.unified_diff(
                        [item.text_raw],
                        [item.text_clean],
                        fromfile="text_raw",
                        tofile="text_clean",
                        lineterm="",
                    )
                ),
            }
            for item in paragraphs
            if item.text_raw != item.text_clean
        ],
        "artifacts": artifacts,
        "external_model_api_used": False,
        "asr_rerun": False,
    }
    atomic_write_json(report_path, report)
    validate_paragraphs(root)
    return {"job_id": job_id, "status": "paragraphs_ready", "cache_hit": False, **report}


def build_corrections(job_id: str, *, config: AppConfig, force: bool = False) -> dict[str, Any]:
    files, _, _ = load_job_context(job_id, config=config)
    root = files.root
    plan_path = root / "analyses/m4a/correction-plan.json"
    if not plan_path.is_file():
        raise StateError("M4A correction plan is missing; current Codex must write it first.")
    build_paragraphs(job_id, config=config)
    paragraphs = [
        Paragraph.model_validate(item)
        for item in _load_jsonl(root / "transcript/paragraphs/paragraphs.jsonl")
    ]
    paragraph_by_id = {item.paragraph_id: item for item in paragraphs}
    segments = {item.segment_id: item for item in _read_segments(root)}
    frame_ids = {item["frame_id"] for item in _load_jsonl(files.frames_jsonl)}
    plan = _load_json(plan_path)
    try:
        corrections = [Correction.model_validate(item) for item in plan["corrections"]]
    except (KeyError, ValidationError) as error:
        raise StateError(f"Invalid correction plan: {error}") from error
    for correction in corrections:
        if correction.paragraph_id not in paragraph_by_id:
            raise StateError(f"Correction {correction.correction_id} cites unknown paragraph.")
        if not set(correction.source_segment_ids) <= set(segments):
            raise StateError(f"Correction {correction.correction_id} cites unknown segment.")
        if not set(correction.visual_evidence) <= frame_ids:
            raise StateError(f"Correction {correction.correction_id} cites unknown frame.")
        source_text = "".join(segments[item].text_raw for item in correction.source_segment_ids)
        if correction.original_text not in source_text:
            raise StateError(f"Correction {correction.correction_id} original text is absent.")
    input_hash = hash_object(
        {
            "schema": CORRECTION_SCHEMA,
            "paragraph_plan": hash_file(root / "analyses/m4a/paragraph-plan.json"),
            "correction_plan": hash_file(plan_path),
            "original": hash_file(root / "transcript/asr/original.jsonl"),
            "frames": hash_file(files.frames_jsonl),
        }
    )
    destination = root / "transcript/corrections"
    manifest_path = destination / "correction-manifest.json"
    if (
        not force
        and manifest_path.is_file()
        and _load_json(manifest_path).get("input_hash") == input_hash
    ):
        build_paragraphs(job_id, config=config)
        return {
            "job_id": job_id,
            "status": "corrections_ready",
            "cache_hit": True,
            **_load_json(manifest_path),
        }
    destination.mkdir(parents=True, exist_ok=True)
    serialized = _jsonl(corrections)
    atomic_write_text(destination / "correction-suggestions.jsonl", serialized)
    atomic_write_text(destination / "correction-decisions.jsonl", serialized)
    atomic_write_json(
        destination / "course-glossary.json",
        {
            "schema_version": "1.0",
            "terms": [
                {
                    "term": item.suggested_text,
                    "source_correction_id": item.correction_id,
                    "transcript_evidence": item.transcript_evidence,
                    "visual_evidence": item.visual_evidence,
                    "decision": item.decision,
                }
                for item in corrections
            ],
        },
    )
    accepted = [item for item in corrections if item.applied]
    report_lines = ["# M4A 纠错报告", "", "> LectureFlow generated — 请勿覆盖原始 ASR。", ""]
    for item in corrections:
        diff = "".join(difflib.ndiff([item.original_text], [item.suggested_text])).replace(
            "\n", " "
        )
        report_lines.extend(
            [
                f"## {item.correction_id} · {item.decision}",
                "",
                f"- 段落：{item.paragraph_id}；时间：{item.start:g}–{item.end:g} 秒",
                f"- 原文：`{item.original_text}`",
                f"- 建议：`{item.suggested_text}`",
                f"- 可复核 diff：`{diff.strip()}`",
                f"- 依据：{item.reason}",
                "",
            ]
        )
    atomic_write_text(destination / "correction-report.md", "\n".join(report_lines))
    manifest = {
        "schema_version": "1.0",
        "input_hash": input_hash,
        "accepted": len(accepted),
        "pending": sum(item.decision == "pending" for item in corrections),
        "rejected": sum(item.decision == "rejected" for item in corrections),
        "raw_transcript_sha256": hash_file(root / "transcript/asr/original.jsonl"),
        "external_model_api_used": False,
    }
    atomic_write_json(manifest_path, manifest)
    build_paragraphs(job_id, config=config, force=True)
    corrected_paragraphs = _load_jsonl(root / "transcript/paragraphs/paragraphs.jsonl")
    atomic_write_text(destination / "corrected-paragraphs.jsonl", _jsonl(corrected_paragraphs))
    return {"job_id": job_id, "status": "corrections_ready", "cache_hit": False, **manifest}


def _validate_mermaid(value: str) -> None:
    lines = value.splitlines()
    if not lines or lines[0] != "mindmap" or any("<" in line or ">" in line for line in lines):
        raise StateError("Generated Mermaid mindmap failed the local syntax safety check.")
    if any(line.strip().startswith("```") for line in lines):
        raise StateError("Mermaid source cannot contain nested code fences.")


def validate_render(root: Path) -> dict[str, Any]:
    paragraphs = {
        item.paragraph_id: item
        for item in (
            Paragraph.model_validate(raw)
            for raw in _load_jsonl(root / "transcript/paragraphs/paragraphs.jsonl")
        )
    }
    sections = [
        Section.model_validate(item) for item in _load_json(root / "analyses/m4a/sections.json")
    ]
    evidence_ids = {
        item["evidence_id"] for item in _load_jsonl(root / "evidence/evidence-ledger.jsonl")
    }
    for section in sections:
        if not set(section.paragraph_ids) <= set(paragraphs):
            raise StateError(f"Section {section.section_id} cites an unknown paragraph.")
        first, last = paragraphs[section.paragraph_ids[0]], paragraphs[section.paragraph_ids[-1]]
        if section.start != first.start or section.end != last.end:
            raise StateError(f"Section {section.section_id} time does not match its paragraphs.")
        if not set(section.evidence_ids) <= evidence_ids:
            raise StateError(f"Section {section.section_id} cites unknown evidence.")
    mindmap = MindMap.model_validate(_load_json(root / "analyses/m4a/mindmap.json"))
    targets = set(paragraphs) | {item.section_id for item in sections}
    nodes = list(mindmap.children)
    while nodes:
        node = nodes.pop()
        if node.target_id not in targets:
            raise StateError(f"Mindmap cites unknown target {node.target_id}.")
        nodes.extend(node.children)
    _validate_mermaid((root / "analyses/m4a/mindmap.mmd").read_text())
    return {"valid": True, "section_count": len(sections), "paragraph_count": len(paragraphs)}


def render_m4a(job_id: str, *, config: AppConfig, force: bool = False) -> dict[str, Any]:
    files, _, _ = load_job_context(job_id, config=config)
    root = files.root
    build_corrections(job_id, config=config)
    paragraphs = [
        Paragraph.model_validate(item)
        for item in _load_jsonl(root / "transcript/paragraphs/paragraphs.jsonl")
    ]
    paragraph_by_segment = {
        segment_id: paragraph
        for paragraph in paragraphs
        for segment_id in paragraph.source_segment_ids
    }
    packet_analysis = _load_json(root / "analyses/P0001-analysis.json")
    evidence = _load_jsonl(root / "evidence/evidence-ledger.jsonl")
    evidence_by_id = {item["evidence_id"]: item for item in evidence}
    input_hash = hash_object(
        {
            "schema": RENDER_SCHEMA,
            "paragraphs": hash_file(root / "transcript/paragraphs/paragraphs.jsonl"),
            "corrections": hash_file(root / "transcript/corrections/correction-decisions.jsonl"),
            "analysis": hash_file(root / "analyses/P0001-analysis.json"),
            "evidence": hash_file(root / "evidence/evidence-ledger.jsonl"),
        }
    )
    destination = root / "analyses/m4a"
    summary_path = destination / "m4a-summary.json"
    if (
        not force
        and summary_path.is_file()
        and _load_json(summary_path).get("input_hash") == input_hash
    ):
        validate_render(root)
        return {
            "job_id": job_id,
            "status": "m4a_ready",
            "cache_hit": True,
            **_load_json(summary_path),
        }
    sections: list[Section] = []
    for index, source in enumerate(packet_analysis["sections"], 1):
        section_paragraphs = [
            paragraph
            for paragraph in paragraphs
            if set(paragraph.source_segment_ids) & set(source["transcript_evidence"])
        ]
        related_evidence = [
            item
            for item in evidence
            if set(item["transcript_ids"]) & set(source["transcript_evidence"])
        ]
        sections.append(
            Section(
                section_id=f"SEC{index:04d}",
                title=source["title"],
                start=section_paragraphs[0].start,
                end=section_paragraphs[-1].end,
                paragraph_ids=[item.paragraph_id for item in section_paragraphs],
                summary=source["summary"],
                evidence_ids=[item["evidence_id"] for item in related_evidence],
                frame_ids=source["visual_evidence"],
                confidence=source["confidence"],
                uncertainties=source["uncertainties"],
            )
        )
    atomic_write_json(
        destination / "sections.json", [item.model_dump(mode="json") for item in sections]
    )
    categories = [
        ("课程定位", "E000001"),
        ("内容范围", "E000003"),
        ("课程安排", "E000004"),
        ("作业与项目", "E000005"),
        ("组队要求", "E000006"),
        ("计算资源", "E000007"),
        ("重要日期", "E000007"),
        ("技术术语", "E000002"),
        ("待核验信息", "E000008"),
    ]
    highlights: list[Highlight] = []
    for index, (category, evidence_id) in enumerate(categories, 1):
        item = evidence_by_id[evidence_id]
        paragraph = paragraph_by_segment[item["transcript_ids"][0]]
        highlights.append(
            Highlight(
                highlight_id=f"H{index:04d}",
                category=category,
                text=item["claim"],
                paragraph_id=paragraph.paragraph_id,
                evidence_id=evidence_id,
                transcript_ids=item["transcript_ids"],
                frame_ids=item["frame_ids"],
                timestamp=paragraph.start,
                status="pending" if category == "待核验信息" else "confirmed",
            )
        )
    atomic_write_json(
        destination / "highlights.json", [item.model_dump(mode="json") for item in highlights]
    )
    atomic_write_json(destination / "course-info.json", packet_analysis["course_information"])
    mindmap = MindMap(
        title="课程前五分钟",
        children=[
            MindMapNode(
                title=section.title,
                target_type="section",
                target_id=section.section_id,
                start=section.start,
                children=[
                    MindMapNode(
                        title=paragraph.title or paragraph.paragraph_id,
                        target_type="paragraph",
                        target_id=paragraph.paragraph_id,
                        start=paragraph.start,
                    )
                    for paragraph in paragraphs
                    if paragraph.paragraph_id in section.paragraph_ids
                ],
            )
            for section in sections
        ],
    )
    atomic_write_json(destination / "mindmap.json", mindmap.model_dump(mode="json"))
    markdown = [f"# {mindmap.title}", ""]
    mermaid = ["mindmap", f"  root(({mindmap.title}))"]
    for section in mindmap.children:
        markdown.append(f"- [{section.title}](#{section.target_id}) · {section.start:g}s")
        mermaid.append(f"    {section.target_id}[{section.title}]")
        for paragraph in section.children:
            markdown.append(
                f"  - [{paragraph.title}](#{paragraph.target_id}) · {paragraph.start:g}s"
            )
            mermaid.append(f"      {paragraph.target_id}[{paragraph.title}]")
    mermaid_text = "\n".join(mermaid) + "\n"
    _validate_mermaid(mermaid_text)
    atomic_write_text(destination / "mindmap.md", "\n".join(markdown) + "\n")
    atomic_write_text(destination / "mindmap.mmd", mermaid_text)
    summary = {
        "schema_version": "1.0",
        "input_hash": input_hash,
        "job_id": job_id,
        "time_range": [0.0, 300.0],
        "paragraph_count": len(paragraphs),
        "section_count": len(sections),
        "highlight_count": len(highlights),
        "scope": "BV1pf421z757 P1 first five minutes only",
        "jump_granularity": "paragraph_start",
        "word_timestamps_used": False,
        "external_model_api_used": False,
        "asr_rerun": False,
    }
    atomic_write_json(summary_path, summary)
    validate_render(root)
    return {"job_id": job_id, "status": "m4a_ready", "cache_hit": False, **summary}
