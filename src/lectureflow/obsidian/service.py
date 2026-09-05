from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from lectureflow.atomic import atomic_write_json, atomic_write_text
from lectureflow.config import AppConfig
from lectureflow.errors import StateError
from lectureflow.hashing import hash_file, hash_object
from lectureflow.pipeline import load_job_context
from lectureflow.schemas.m4a import Correction, Paragraph, Section

MARKER = "<!-- LectureFlow generated: M4A; edits may block the next export -->"
EXPORT_SCHEMA = "obsidian-m4a-1.0.1"


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"Cannot read Obsidian export input {path.name}: {error}") from error


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"Cannot read Obsidian JSONL {path.name}: {error}") from error


def _time(seconds: float) -> str:
    value = int(seconds)
    return f"{value // 60:02d}:{value % 60:02d}"


def _bili_link(bvid: str, seconds: float, part: int = 1) -> str:
    return f"https://www.bilibili.com/video/{bvid}?p={part}&t={int(seconds)}"


def _assert_managed_outputs(destination: Path, previous: dict[str, Any] | None) -> None:
    if not destination.exists():
        return
    if previous is None:
        unexpected = [item for item in destination.rglob("*") if item.is_file()]
        if unexpected:
            raise StateError(
                "Obsidian destination contains files without a LectureFlow export manifest; "
                "refusing to overwrite user content."
            )
        return
    for relative, digest in previous.get("artifacts", {}).items():
        target = destination / relative
        if target.is_file() and hash_file(target) != digest:
            raise StateError(
                f"Generated Obsidian file was modified; refusing overwrite: {relative}"
            )


def _markdown_paragraph(paragraph: Paragraph, bvid: str, mode: str) -> str:
    text = {
        "raw": paragraph.text_raw,
        "cleaned": paragraph.text_clean,
        "corrected": paragraph.text_corrected,
    }[mode]
    frames = "\n".join(
        f"![[assets/frames/{frame_id}.png|{frame_id} · {_time(paragraph.start)}]]"
        for frame_id in paragraph.frame_ids
    )
    time_link = f"[{_time(paragraph.start)}]({_bili_link(bvid, paragraph.start)})"
    evidence = ", ".join(paragraph.evidence_ids) or "—"
    frame_list = ", ".join(paragraph.frame_ids) or "—"
    return (
        f"### {paragraph.paragraph_id} · {time_link}"
        f" · {paragraph.title}\n\n{text}\n\n"
        f"证据：{evidence}；画面：{frame_list}\n\n"
        f"{frames}\n"
    )


def _export_m4b(
    job_id: str,
    *,
    root: Path,
    bvid: str,
    vault: Path,
    force: bool,
) -> dict[str, Any]:
    marker = "<!-- LectureFlow generated: M4B P6 00:00-25:00 -->"
    analysis = root / "analyses/m4b"
    paragraphs = _load_jsonl(root / "transcript/paragraphs/paragraphs.jsonl")
    sections = _load_json(analysis / "sections.json")
    corrections = _load_jsonl(root / "transcript/corrections/correction-decisions.jsonl")
    formulas = _load_json(analysis / "formulas.json")
    glossary = _load_json(analysis / "glossary.json")
    evidence_path = root / "evidence/evidence-ledger.jsonl"
    inputs = {
        "paragraphs": hash_file(root / "transcript/paragraphs/paragraphs.jsonl"),
        "sections": hash_file(analysis / "sections.json"),
        "corrections": hash_file(root / "transcript/corrections/correction-decisions.jsonl"),
        "formulas": hash_file(analysis / "formulas.json"),
        "glossary": hash_file(analysis / "glossary.json"),
        "mindmap": hash_file(analysis / "mindmap.mmd"),
        "evidence": hash_file(evidence_path),
        "template": hash_object({"schema": "obsidian-m4b-1.0.0", "marker": marker}),
    }
    input_hash = hash_object(inputs)
    destination = vault.expanduser().resolve() / "LectureFlow P6 Demo"
    export_manifest = destination / ".lectureflow-export.json"
    previous = _load_json(export_manifest) if export_manifest.is_file() else None
    _assert_managed_outputs(destination, previous)
    if previous and not force and previous.get("input_hash") == input_hash:
        return {
            "job_id": job_id,
            "status": "obsidian_ready",
            "cache_hit": True,
            "output": str(destination),
            **previous,
        }
    destination.mkdir(parents=True, exist_ok=True)

    def time_link(seconds: float) -> str:
        return f"[{_time(seconds)}]({_bili_link(bvid, seconds, 6)})"

    def paragraph_md(paragraph: dict[str, Any], mode: str) -> str:
        field = {"raw": "text_raw", "cleaned": "text_clean", "corrected": "text_corrected"}[mode]
        images = "\n".join(
            f"![[assets/frames/{frame_id}.png|{frame_id} · {_time(paragraph['start'])}]]"
            for frame_id in paragraph["frame_ids"]
        )
        return (
            f"### {paragraph['paragraph_id']} · {time_link(paragraph['start'])} · "
            f"{paragraph['title']}\n\n{paragraph[field]}\n\n"
            f"证据：{', '.join(paragraph['evidence_ids'])}；"
            f"画面：{', '.join(paragraph['frame_ids']) or '—'}。\n\n{images}\n"
        )

    content: dict[str, str] = {
        "00-P6前25分钟总览.md": "\n".join(
            [
                marker,
                "# P6 前 25 分钟总览",
                "",
                "> 范围：BV1pf421z757 P6 00:00–25:00；本地 ASR；段落级跳转。",
                "",
                "- [[01-章节笔记]]",
                "- [[02-段落级时间轴]]",
                "- [[03-专业术语表]]",
                "- [[04-公式与推导]]",
                "- [[05-纠错记录]]",
                "- [[06-思维导图]]",
                "- [[07-复习题]]",
            ]
        )
        + "\n"
    }
    chapter_lines = [marker, "# 章节笔记", ""]
    for item in sections:
        chapter_lines.extend(
            [
                f"## {item['section_id']} · {time_link(item['start'])} · {item['title']}",
                "",
                item["summary"],
                "",
                f"段落：{', '.join(item['paragraph_ids'])}",
                f"证据：{', '.join(item['evidence_ids'])}",
                f"公式：{', '.join(item['formula_ids']) or '—'}",
                "",
            ]
        )
    content["01-章节笔记.md"] = "\n".join(chapter_lines)
    timeline = [marker, "# 段落级时间轴", ""]
    timeline.extend(paragraph_md(item, "corrected") for item in paragraphs)
    content["02-段落级时间轴.md"] = "\n".join(timeline)
    glossary_lines = [marker, "# 专业术语表", ""]
    for item in glossary:
        glossary_lines.extend(
            [
                f"## {item['term']} · {time_link(item['first_timestamp'])}",
                "",
                item.get("definition") or "本段未给出独立定义。",
                "",
                f"别名：{', '.join(item['aliases']) or '—'}；"
                f"误识别候选：{', '.join(item['incorrect_candidates']) or '—'}。",
                "",
            ]
        )
    content["03-专业术语表.md"] = "\n".join(glossary_lines)
    formula_lines = [marker, "# 公式与推导", ""]
    for item in formulas:
        status = "✅ 已核验" if item["verification_status"] == "verified" else "⚠️ 待核验"
        formula_lines.extend(
            [
                f"## {item['formula_id']} · {time_link(item['timestamp'])} · {status}",
                "",
                f"![[assets/frames/{item['frame_id']}.png|{item['frame_id']}]]",
                "",
                f"$$\n{item['latex']}\n$$" if item["latex"] else "**公式待人工核验。**",
                "",
                item["spoken_context"],
                "",
                f"证据：{', '.join(item['transcript_ids'])}；{item['frame_id']}。",
                "",
            ]
        )
    content["04-公式与推导.md"] = "\n".join(formula_lines)
    correction_lines = [marker, "# 纠错记录", ""]
    for item in corrections:
        correction_lines.extend(
            [
                f"## {item['correction_id']} · {item['decision']}",
                "",
                f"{time_link(item['start'])}：`{item['original_text']}` → "
                f"`{item['suggested_text']}`",
                "",
                f"画面：{', '.join(item['visual_evidence']) or '—'}；"
                f"应用：{'是' if item['applied'] else '否'}。",
                "",
            ]
        )
    content["05-纠错记录.md"] = "\n".join(correction_lines)
    mindmap = (analysis / "mindmap.mmd").read_text()
    content["06-思维导图.md"] = f"{marker}\n# 思维导图\n\n```mermaid\n{mindmap}```\n"
    content["07-复习题.md"] = (root / "notes/P6-00-25-review-questions.md").read_text()
    for mode in ("raw", "cleaned", "corrected"):
        lines = [marker, f"# {mode} transcript", ""]
        for paragraph in paragraphs:
            lines.append(paragraph_md(paragraph, mode))
            if mode == "corrected":
                decisions = [
                    item
                    for item in corrections
                    if item["paragraph_id"] == paragraph["paragraph_id"]
                ]
                for item in decisions:
                    lines.append(
                        f"[^{item['correction_id']}]: {item['decision']} — "
                        f"{item['original_text']} → {item['suggested_text']}\n"
                    )
        content[f"transcript/{mode}.md"] = "\n".join(lines)
    for relative, value in content.items():
        target = destination / relative
        if target.is_file() and marker not in target.read_text(encoding="utf-8"):
            raise StateError(f"Refusing to overwrite non-generated Markdown: {relative}")
        atomic_write_text(target, value)
    for relative in ("evidence/evidence-ledger.jsonl", "evidence/formula-evidence.jsonl"):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, target)
    opened = {
        frame_id
        for packet in _load_json(root / "analyses/packets/analysis-state.json")["packets"]
        for frame_id in packet["images_opened"]
    }
    frame_by_id = {item["frame_id"]: item for item in _load_jsonl(root / "frames/frames.jsonl")}
    for frame_id in sorted(opened):
        source = root / frame_by_id[frame_id]["path"]
        target = destination / "assets/frames" / f"{frame_id}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    artifacts = {
        str(path.relative_to(destination)): hash_file(path)
        for path in destination.rglob("*")
        if path.is_file() and path != export_manifest
    }
    stored = {
        "schema_version": "1.0",
        "input_hash": input_hash,
        "generated_marker": marker,
        "artifacts": artifacts,
        "raw_asr_sha256": hash_file(root / "transcript/asr/original.jsonl"),
        "paragraph_count": len(paragraphs),
        "section_count": len(sections),
        "formula_count": len(formulas),
        "frame_count": len(opened),
        "canvas_generated": False,
        "external_model_api_used": False,
    }
    atomic_write_json(export_manifest, stored)
    return {
        "job_id": job_id,
        "status": "obsidian_ready",
        "cache_hit": False,
        "output": str(destination),
        **stored,
    }


def export_obsidian(
    job_id: str, *, config: AppConfig, vault: Path, force: bool = False
) -> dict[str, Any]:
    files, manifest, _ = load_job_context(job_id, config=config)
    root = files.root
    if (root / "merged/p6-complete/manifest.json").is_file():
        from lectureflow.m5 import export_complete_p6_obsidian

        return export_complete_p6_obsidian(job_id, config=config, vault=vault, force=force)
    bvid = manifest.source.bvid
    if not bvid:
        raise StateError("M4A Obsidian smoke export requires the registered Bilibili source.")
    if (root / "analyses/m4b/m4b-summary.json").is_file():
        return _export_m4b(job_id, root=root, bvid=bvid, vault=vault, force=force)
    paragraphs = [
        Paragraph.model_validate(item)
        for item in _load_jsonl(root / "transcript/paragraphs/paragraphs.jsonl")
    ]
    sections = [
        Section.model_validate(item) for item in _load_json(root / "analyses/m4a/sections.json")
    ]
    corrections = [
        Correction.model_validate(item)
        for item in _load_jsonl(root / "transcript/corrections/correction-decisions.jsonl")
    ]
    frames = _load_jsonl(files.frames_jsonl)
    evidence_path = root / "evidence/evidence-ledger.jsonl"
    inputs = {
        "paragraphs": hash_file(root / "transcript/paragraphs/paragraphs.jsonl"),
        "sections": hash_file(root / "analyses/m4a/sections.json"),
        "corrections": hash_file(root / "transcript/corrections/correction-decisions.jsonl"),
        "mindmap": hash_file(root / "analyses/m4a/mindmap.mmd"),
        "evidence": hash_file(evidence_path),
        "frames": hash_file(files.frames_jsonl),
        "template": hash_object({"schema": EXPORT_SCHEMA, "marker": MARKER}),
    }
    input_hash = hash_object(inputs)
    vault_root = vault.expanduser().resolve()
    destination = vault_root / "LectureFlow Demo"
    export_manifest = destination / ".lectureflow-export.json"
    previous = _load_json(export_manifest) if export_manifest.is_file() else None
    _assert_managed_outputs(destination, previous)
    if previous and not force and previous.get("input_hash") == input_hash:
        return {
            "job_id": job_id,
            "status": "obsidian_ready",
            "cache_hit": True,
            "output": str(destination),
            **previous,
        }
    destination.mkdir(parents=True, exist_ok=True)
    content: dict[str, str] = {}
    overview = [
        MARKER,
        "# 五分钟总览",
        "",
        "> 范围：BV1pf421z757 P1 00:00–05:00；跳转粒度为段落开头。",
        "",
        "这五分钟主要介绍课程定位、内容范围、作业项目、计算资源与教学团队，并非完整课程讲义。",
        "",
        "- [[01-章节笔记]]",
        "- [[02-段落级时间轴]]",
        "- [[03-纠错记录]]",
        "- [[04-思维导图]]",
    ]
    content["00-五分钟总览.md"] = "\n".join(overview) + "\n"
    chapter_lines = [MARKER, "# 章节笔记", ""]
    for section in sections:
        time_link = f"[{_time(section.start)}]({_bili_link(bvid, section.start)})"
        paragraph_links = ", ".join(f"[[02-段落级时间轴#{item}]]" for item in section.paragraph_ids)
        chapter_lines.extend(
            [
                f"## {section.section_id} · {time_link} · {section.title}",
                "",
                section.summary,
                "",
                f"段落：{paragraph_links}",
                f"证据：{', '.join(section.evidence_ids)}",
                "",
            ]
        )
    content["01-章节笔记.md"] = "\n".join(chapter_lines)
    timeline = [MARKER, "# 段落级时间轴", ""]
    for paragraph in paragraphs:
        timeline.append(_markdown_paragraph(paragraph, bvid, "corrected"))
    content["02-段落级时间轴.md"] = "\n".join(timeline)
    correction_lines = [MARKER, "# 纠错记录", ""]
    for item in corrections:
        time_link = f"[{_time(item.start)}]({_bili_link(bvid, item.start)})"
        transcript_evidence = ", ".join(item.transcript_evidence)
        visual_evidence = ", ".join(item.visual_evidence)
        correction_lines.extend(
            [
                f"## {item.correction_id} · {item.decision}",
                "",
                f"{time_link}：`{item.original_text}` → `{item.suggested_text}`",
                "",
                f"依据：{item.reason}；字幕：{transcript_evidence}；画面：{visual_evidence}。",
                "",
            ]
        )
    content["03-纠错记录.md"] = "\n".join(correction_lines)
    mindmap = (root / "analyses/m4a/mindmap.mmd").read_text()
    content["04-思维导图.md"] = f"{MARKER}\n# 思维导图\n\n```mermaid\n{mindmap}```\n"
    for mode in ("raw", "cleaned", "corrected"):
        lines = [MARKER, f"# {mode} transcript", ""]
        for paragraph in paragraphs:
            lines.append(_markdown_paragraph(paragraph, bvid, mode))
            if mode == "corrected":
                paragraph_corrections = [
                    item for item in corrections if item.paragraph_id == paragraph.paragraph_id
                ]
                if paragraph_corrections:
                    references = " ".join(
                        f"[^{item.correction_id}]" for item in paragraph_corrections
                    )
                    lines.append(f"纠错记录：{references}\n")
                for correction in paragraph_corrections:
                    lines.append(
                        f"[^{correction.correction_id}]: {correction.decision} — "
                        f"{correction.original_text} → {correction.suggested_text}\n"
                    )
        content[f"transcript/{mode}.md"] = "\n".join(lines)
    for relative, value in content.items():
        target = destination / relative
        if target.is_file() and MARKER not in target.read_text(encoding="utf-8"):
            raise StateError(f"Refusing to overwrite non-generated Markdown: {relative}")
        atomic_write_text(target, value)
    evidence_target = destination / "evidence/evidence-ledger.jsonl"
    evidence_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(evidence_path, evidence_target)
    for frame in frames:
        source = root / frame["path"]
        target = destination / "assets/frames" / f"{frame['frame_id']}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    artifacts = {
        str(path.relative_to(destination)): hash_file(path)
        for path in destination.rglob("*")
        if path.is_file() and path != export_manifest
    }
    stored = {
        "schema_version": "1.0",
        "input_hash": input_hash,
        "generated_marker": MARKER,
        "artifacts": artifacts,
        "raw_asr_sha256": hash_file(root / "transcript/asr/original.jsonl"),
        "paragraph_count": len(paragraphs),
        "section_count": len(sections),
        "frame_count": len(frames),
        "canvas_generated": False,
    }
    atomic_write_json(export_manifest, stored)
    return {
        "job_id": job_id,
        "status": "obsidian_ready",
        "cache_hit": False,
        "output": str(destination),
        **stored,
    }
