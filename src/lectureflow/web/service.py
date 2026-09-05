# ruff: noqa: E501
from __future__ import annotations

import json
import mimetypes
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from lectureflow.atomic import atomic_write_json, atomic_write_text
from lectureflow.config import AppConfig
from lectureflow.errors import StateError
from lectureflow.hashing import hash_file, hash_object
from lectureflow.media.service import resolve_media_path
from lectureflow.pipeline import load_job_context
from lectureflow.schemas.frames import MediaManifest
from lectureflow.schemas.m4a import Correction, MindMap, Paragraph, Section

GENERATED_MARKER = "LectureFlow generated local review web"

INDEX_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LectureFlow · 本地听悟</title><link rel="stylesheet" href="/app.css"></head>
<body><header><div><strong>LectureFlow</strong><span>本地听悟 · 段落级</span></div><div id="status">正在读取本地数据…</div></header>
<main>
<section class="panel left"><h1 id="course-title">本地课程片段</h1><video id="video" controls preload="metadata" src="/media/video"></video><div class="time-row"><span id="clock">00:00 / --:--</span><label>倍速 <select id="rate"><option>0.75</option><option selected>1</option><option>1.25</option><option>1.5</option><option>2</option></select></label></div><h2>关键画面</h2><div id="frames" class="frames"></div></section>
<section class="panel center"><div class="toolbar"><div id="modes" class="segmented"><button data-mode="raw">原始</button><button class="active" data-mode="clean">整理</button><button data-mode="corrected">纠错</button></div><label class="autoscroll"><input id="autoscroll" type="checkbox" checked> 自动滚动</label></div><div class="search"><input id="search" type="search" placeholder="搜索段落，例如：大语言模型"><span id="search-count" class="search-count">0 个结果</span></div><div id="search-results" class="search-results"></div><div id="paragraphs" class="paragraphs"></div></section>
<aside class="panel right"><nav id="tabs"><button class="active" data-tab="sections">章节</button><button data-tab="summary">摘要</button><button data-tab="mindmap">导图</button><button data-tab="formulas">公式</button><button data-tab="glossary">术语</button><button data-tab="evidence">证据</button></nav><div id="right-content"></div></aside>
</main><dialog id="frame-dialog"><button id="close-dialog" aria-label="关闭">×</button><img id="frame-full" alt="关键帧高清图"><p id="frame-caption"></p></dialog><div id="error" role="alert" hidden></div><script src="/app.js"></script></body></html>
"""

APP_CSS = """:root{color-scheme:light dark;--bg:#f4f5f7;--panel:#fff;--text:#19212d;--muted:#667085;--line:#dce1e8;--accent:#246bfe;--soft:#eaf1ff;--warn:#9a6700}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.62 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}header{height:58px;padding:0 20px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line);background:var(--panel)}header strong{font-size:18px;margin-right:12px}header span,#status{color:var(--muted);font-size:13px}main{display:grid;grid-template-columns:minmax(280px,32%) minmax(390px,41%) minmax(290px,27%);height:calc(100vh - 58px);gap:1px;background:var(--line)}.panel{background:var(--panel);padding:18px;overflow:auto}.left h1{font-size:20px;margin:0 0 14px}.left video{width:100%;aspect-ratio:16/9;background:#090b10;border-radius:10px}.time-row,.toolbar,.search{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:12px 0}.time-row{color:var(--muted)}h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}.frames{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.frame{border:1px solid var(--line);background:none;border-radius:8px;padding:4px;cursor:pointer;text-align:left}.frame img{width:100%;display:block;border-radius:5px}.frame span{display:block;padding:4px 2px 0;color:var(--muted);font-size:12px}.segmented,nav{display:flex;gap:4px;padding:3px;background:var(--bg);border-radius:9px}.segmented button,nav button{border:0;background:transparent;padding:7px 9px;border-radius:7px;color:var(--muted);cursor:pointer}.segmented button.active,nav button.active{background:var(--panel);color:var(--accent);box-shadow:0 1px 4px #0001}.search input{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:inherit}.search-count{white-space:nowrap;color:var(--muted);font-size:12px}.search-results:empty{display:none}.search-results{border:1px solid var(--line);border-radius:8px;padding:6px;margin-bottom:10px}.search-result{display:block;width:100%;border:0;background:none;color:inherit;text-align:left;padding:6px;cursor:pointer}.paragraph{border:1px solid transparent;border-bottom-color:var(--line);padding:16px 13px;scroll-margin-top:12px;cursor:pointer;border-radius:8px}.paragraph:hover{background:var(--bg)}.paragraph.current{border-color:var(--accent);background:var(--soft)}.paragraph-head{display:flex;align-items:center;gap:9px;margin-bottom:7px}.timestamp{font-variant-numeric:tabular-nums;color:var(--accent);font-weight:650}.paragraph-title{font-weight:650}.badges{display:flex;flex-wrap:wrap;gap:5px;margin-top:9px}.badge{font-size:11px;padding:2px 6px;background:var(--bg);color:var(--muted);border-radius:10px}.badge.accepted{color:#067647;background:#ecfdf3}.badge.pending{color:var(--warn);background:#fff8db}.diff{margin-top:8px;padding:8px;border-left:3px solid #12b76a;background:#ecfdf3;color:#064e3b;font-size:13px}.diff.pending{border-left-color:#f79009;background:#fff8db;color:#7a2e0e}.right nav{position:sticky;top:-18px;z-index:2;margin:-18px -18px 14px;padding:12px 18px;border-radius:0;border-bottom:1px solid var(--line);background:var(--panel);overflow-x:auto}.card{border-bottom:1px solid var(--line);padding:12px 0}.card h3{font-size:15px;margin:0 0 6px}.card p{margin:5px 0;color:var(--muted)}.formula{display:block;white-space:pre-wrap;overflow-wrap:anywhere;padding:9px;border:1px solid var(--line);border-radius:7px;background:var(--bg);font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--text)}.jump{border:0;background:none;color:var(--accent);padding:0;cursor:pointer}.tree{list-style:none;padding-left:15px}.tree button{border:0;background:none;color:inherit;padding:5px 0;cursor:pointer;text-align:left}.tree button:hover{color:var(--accent)}dialog{max-width:min(1100px,90vw);border:0;border-radius:12px;padding:12px;background:var(--panel)}dialog img{max-width:100%;max-height:78vh;display:block}#close-dialog{float:right;border:0;background:#0008;color:#fff;border-radius:50%;width:30px;height:30px;font-size:20px}#error{position:fixed;inset:auto 20px 20px;padding:12px;background:#b42318;color:white;border-radius:8px}@media(max-width:1000px){main{grid-template-columns:1fr;height:auto}.panel{min-height:420px}.right nav{top:0}header{position:sticky;top:0;z-index:3}}@media(prefers-color-scheme:dark){:root{--bg:#11151c;--panel:#181e27;--text:#eef2f6;--muted:#9aa6b2;--line:#303846;--soft:#172b4d;--accent:#7ca7ff}.diff{background:#123527;color:#b7f3d3}.diff.pending{background:#3b2e0d;color:#fedf89}.badge.accepted{background:#123527;color:#75e0a7}.badge.pending{background:#3b2e0d;color:#fedf89}}
"""
APP_CSS = APP_CSS.replace(
    "white-space:pre-wrap;overflow-wrap:anywhere",
    "max-width:100%;white-space:pre;overflow-x:auto;overflow-y:hidden",
)
APP_CSS += """:root[data-theme=light]{color-scheme:light;--bg:#f4f5f7;--panel:#fff;--text:#19212d;--muted:#667085;--line:#dce1e8;--accent:#246bfe;--soft:#eaf1ff;--warn:#9a6700}:root[data-theme=dark]{color-scheme:dark;--bg:#11151c;--panel:#181e27;--text:#eef2f6;--muted:#9aa6b2;--line:#303846;--soft:#172b4d;--accent:#7ca7ff}:root[data-theme=dark] .diff{background:#123527;color:#b7f3d3}:root[data-theme=dark] .diff.pending{background:#3b2e0d;color:#fedf89}:root[data-theme=dark] .badge.accepted{background:#123527;color:#75e0a7}:root[data-theme=dark] .badge.pending{background:#3b2e0d;color:#fedf89}"""

APP_JS = r"""'use strict';
const requestedTheme=new URLSearchParams(location.search).get('theme');if(requestedTheme==='light'||requestedTheme==='dark')document.documentElement.dataset.theme=requestedTheme;
const state={data:null,mode:'clean',tab:'sections',current:null};
const $=id=>document.getElementById(id); const video=$('video');
const format=s=>{const m=Math.floor(s/60),x=Math.floor(s%60);return `${String(m).padStart(2,'0')}:${String(x).padStart(2,'0')}`};
const el=(tag,cls,text)=>{const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=text;return node};
function jump(seconds){video.currentTime=Number(seconds);video.focus()}
function paragraphForTranscript(id){return state.data.paragraphs.find(p=>p.source_segment_ids.includes(id))}
function openFrame(frameId,timestamp){const frame=state.data.frames.find(x=>x.frame_id===frameId);if(!frame)return;jump(timestamp??frame.timestamp);$('frame-full').src=frame.url;$('frame-caption').textContent=`${frame.frame_id} · ${format(frame.timestamp)}`;$('frame-dialog').showModal()}
function badges(node,p){const box=el('div','badges');p.evidence_ids.forEach(x=>box.append(el('span','badge',x)));p.frame_ids.forEach(x=>box.append(el('span','badge',x)));const decisions=state.data.corrections.filter(x=>x.paragraph_id===p.paragraph_id);decisions.forEach(x=>box.append(el('span',`badge ${x.decision}`,`${x.correction_id} · ${x.decision==='accepted'?'已接受':'待核验'}`)));node.append(box)}
function renderParagraphs(){const box=$('paragraphs');box.replaceChildren();state.data.paragraphs.forEach(p=>{const article=el('article','paragraph');article.id=p.paragraph_id;article.dataset.start=p.start;article.dataset.end=p.end;article.tabIndex=0;const head=el('div','paragraph-head');const time=el('button','jump timestamp',format(p.start));time.addEventListener('click',e=>{e.stopPropagation();jump(p.start)});head.append(time,el('span','paragraph-title',p.title));article.append(head);const key=state.mode==='raw'?'text_raw':state.mode==='corrected'?'text_corrected':'text_clean';article.append(el('div','paragraph-text',p[key]));badges(article,p);const accepted=state.data.corrections.filter(x=>x.paragraph_id===p.paragraph_id&&x.applied);accepted.forEach(x=>article.append(el('div','diff',`已接受：${x.original_text} → ${x.suggested_text}（${x.correction_id}）`)));const pending=state.data.corrections.filter(x=>x.paragraph_id===p.paragraph_id&&x.decision==='pending');pending.forEach(x=>article.append(el('div','diff pending',`待核验，未应用：${x.original_text} → ${x.suggested_text}（${x.correction_id}）`)));article.addEventListener('click',()=>jump(p.start));article.addEventListener('keydown',e=>{if(e.key==='Enter')jump(p.start)});box.append(article)})}
function renderFrames(){const box=$('frames');state.data.frames.forEach(f=>{const b=el('button','frame');const img=el('img');img.src=f.url;img.loading='lazy';img.alt=`${f.frame_id} 关键帧`;b.append(img,el('span','',`${f.frame_id} · ${format(f.timestamp)}`));b.addEventListener('click',()=>openFrame(f.frame_id,f.timestamp));box.append(b)})}
function card(title,text,start){const c=el('section','card');const h=el('h3');const b=el('button','jump',title);b.addEventListener('click',()=>jump(start));h.append(b);c.append(h,el('p','',text));return c}
function treeNode(node){const li=el('li');const b=el('button','',`${node.title} · ${format(node.start)}`);b.addEventListener('click',()=>jump(node.start));li.append(b);if(node.children?.length){const ul=el('ul','tree');node.children.forEach(x=>ul.append(treeNode(x)));li.append(ul)}return li}
function renderRight(){const box=$('right-content');box.replaceChildren();if(state.tab==='sections')state.data.sections.forEach(s=>box.append(card(`${s.title} · ${format(s.start)}`,s.summary,s.start)));else if(state.tab==='summary')state.data.highlights.forEach(h=>box.append(card(h.category,h.text,h.timestamp)));else if(state.tab==='mindmap'){const ul=el('ul','tree');state.data.mindmap.children.forEach(x=>ul.append(treeNode(x)));box.append(ul)}else if(state.tab==='formulas')(state.data.formulas||[]).forEach(f=>{const c=card(`${f.formula_id} · ${format(f.timestamp)}`,f.spoken_context,f.timestamp);c.append(el('code','formula',f.latex||'LaTeX 暂缺'));c.append(el('span',`badge ${f.verification_status==='verified'?'accepted':'pending'}`,f.verification_status==='verified'?'已核验':'待核验'));const b=el('button','jump',`查看 ${f.frame_id}`);b.addEventListener('click',()=>openFrame(f.frame_id,f.timestamp));c.append(b);box.append(c)});else if(state.tab==='glossary')(state.data.glossary||[]).forEach(g=>box.append(card(`${g.term} · ${format(g.first_timestamp)}`,`${g.definition||'本段未给出独立定义'}${g.aliases?.length?`；别名：${g.aliases.join(', ')}`:''}`,g.first_timestamp)));else state.data.evidence.forEach(e=>{const c=card(`${e.evidence_id} · ${e.evidence_type}`,e.claim,e.start);c.append(el('p','',`字幕：${e.transcript_ids.join(', ')||'—'} · 画面：${e.frame_ids.join(', ')||'—'} · 公式：${(e.formula_ids||[]).join(', ')||'—'} · 置信度：${e.confidence}`));if(e.uncertainty)c.append(el('p','',`不确定：${e.uncertainty}`));e.transcript_ids.slice(0,3).forEach(id=>{const p=paragraphForTranscript(id);if(p){const b=el('button','jump',`跳到 ${id}`);b.addEventListener('click',()=>jump(p.start));c.append(b)}});box.append(c)})}
function search(){const q=$('search').value.trim().toLocaleLowerCase();const items=[];if(q){state.data.paragraphs.filter(p=>[p.text_raw,p.text_clean,p.text_corrected,p.title].some(x=>x.toLocaleLowerCase().includes(q))).forEach(p=>items.push({label:`段落 · ${p.title}`,start:p.start,paragraph:p.paragraph_id}));(state.data.formulas||[]).filter(f=>[f.latex,f.visual_transcription,...f.variables.map(v=>v.symbol)].some(x=>String(x).toLocaleLowerCase().includes(q))).forEach(f=>items.push({label:`公式 · ${f.formula_id} · ${f.latex}`,start:f.timestamp}));(state.data.glossary||[]).filter(g=>[g.term,...g.aliases].some(x=>x.toLocaleLowerCase().includes(q))).forEach(g=>items.push({label:`术语 · ${g.term}`,start:g.first_timestamp}))}$('search-count').textContent=`${items.length} 个结果`;const box=$('search-results');box.replaceChildren();items.forEach(item=>{const b=el('button','search-result',`${format(item.start)} · ${item.label}`);b.addEventListener('click',()=>{jump(item.start);if(item.paragraph)document.getElementById(item.paragraph)?.scrollIntoView({block:'center'})});box.append(b)})}
function updateCurrent(){const t=video.currentTime;$('clock').textContent=`${format(t)} / ${format(state.data.time_range[1])}`;const p=state.data.paragraphs.find(x=>x.start<=t&&t<x.end);if(!p||p.paragraph_id===state.current)return;document.querySelector('.paragraph.current')?.classList.remove('current');const node=document.getElementById(p.paragraph_id);node?.classList.add('current');state.current=p.paragraph_id;if($('autoscroll').checked)node?.scrollIntoView({behavior:'smooth',block:'center'})}
async function init(){try{const response=await fetch('/api/data',{cache:'no-store'});if(!response.ok)throw new Error(`本地数据读取失败：HTTP ${response.status}`);state.data=await response.json();$('course-title').textContent=state.data.title;$('clock').textContent=`00:00 / ${format(state.data.time_range[1])}`;renderParagraphs();renderFrames();renderRight();$('status').textContent=`${state.data.paragraphs.length} 段 · ${state.data.sections.length} 章 · ${state.data.formulas?.length||0} 公式 · 仅本机`;document.querySelectorAll('#modes button').forEach(b=>b.addEventListener('click',()=>{state.mode=b.dataset.mode;document.querySelector('#modes .active')?.classList.remove('active');b.classList.add('active');renderParagraphs()}));document.querySelectorAll('#tabs button').forEach(b=>b.addEventListener('click',()=>{state.tab=b.dataset.tab;document.querySelector('#tabs .active')?.classList.remove('active');b.classList.add('active');renderRight()}));$('search').addEventListener('input',search);$('rate').addEventListener('change',e=>video.playbackRate=Number(e.target.value));video.addEventListener('timeupdate',updateCurrent);$('close-dialog').addEventListener('click',()=>$('frame-dialog').close())}catch(error){$('error').hidden=false;$('error').textContent=error.message;$('status').textContent='本地数据错误'}}init();
"""

M5_APP_JS = (
    APP_JS.replace(
        "const state={data:null,mode:'clean',tab:'sections',current:null};",
        "const state={data:null,mode:'clean',tab:'sections',current:null};const media={index:0};",
    )
    .replace(
        "function jump(seconds){video.currentTime=Number(seconds);video.focus()}",
        "function jump(seconds){const t=Number(seconds);const parts=state.data.media_segments||[];const next=Math.max(0,parts.findIndex((p,i)=>t>=p.timeline_start&&t<(p.timeline_end||(i===parts.length-1?Infinity:0))));if(next!==media.index){media.index=next;video.src=parts[next].url;video.load()}video.currentTime=t-(parts[next]?.timeline_start||0);updateCurrent();video.focus()}",
    )
    .replace(
        "function updateCurrent(){const t=video.currentTime;",
        "function updateCurrent(){const t=(state.data.media_segments?.[media.index]?.timeline_start||0)+video.currentTime;",
    )
    .replace(
        "state.data=await response.json();$('course-title')",
        "state.data=await response.json();if(state.data.media_segments?.length){media.index=0;video.src=state.data.media_segments[0].url;video.load()}$('course-title')",
    )
    .replace(
        "video.addEventListener('timeupdate',updateCurrent);",
        "video.addEventListener('timeupdate',updateCurrent);video.addEventListener('ended',()=>{if(state.data.media_segments&&media.index+1<state.data.media_segments.length){media.index++;video.src=state.data.media_segments[media.index].url;video.load();video.play()}});",
    )
)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"Cannot read web input {path.name}: {error}") from error


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"Cannot read web JSONL {path.name}: {error}") from error


def build_web(job_id: str, *, config: AppConfig, force: bool = False) -> dict[str, Any]:
    files, manifest, _ = load_job_context(job_id, config=config)
    root = files.root
    if (root / "merged/p6-complete/manifest.json").is_file():
        return _build_m5_web(job_id, root=root, force=force)
    m4b = (root / "analyses/m4b/m4b-summary.json").is_file()
    analysis_dir = root / ("analyses/m4b" if m4b else "analyses/m4a")
    inputs = [
        root / "transcript/paragraphs/paragraphs.jsonl",
        root / "transcript/corrections/correction-decisions.jsonl",
        analysis_dir / "sections.json",
        analysis_dir / "highlights.json",
        analysis_dir / "mindmap.json",
        root / "evidence/evidence-ledger.jsonl",
        files.frames_jsonl,
    ]
    if m4b:
        inputs.extend([analysis_dir / "formulas.json", analysis_dir / "glossary.json"])
    if any(not path.is_file() for path in inputs):
        raise StateError("Lecture review web inputs are incomplete; run lectureflow render first.")
    input_hash = hash_object(
        {
            "inputs": {str(path.relative_to(root)): hash_file(path) for path in inputs},
            "html": hash_object(INDEX_HTML),
            "css": hash_object(APP_CSS),
            "javascript": hash_object(APP_JS),
        }
    )
    destination = root / "web"
    manifest_path = destination / "web-manifest.json"
    if (
        not force
        and manifest_path.is_file()
        and _load_json(manifest_path).get("input_hash") == input_hash
    ):
        return {
            "job_id": job_id,
            "status": "web_ready",
            "cache_hit": True,
            **_load_json(manifest_path),
        }
    if m4b:
        paragraphs = _load_jsonl(inputs[0])
        corrections = _load_jsonl(inputs[1])
        sections = _load_json(inputs[2])
        mindmap: Any = _load_json(inputs[4])
    else:
        paragraphs = [
            item.model_dump(mode="json")
            for item in (Paragraph.model_validate(value) for value in _load_jsonl(inputs[0]))
        ]
        corrections = [
            item.model_dump(mode="json")
            for item in (Correction.model_validate(value) for value in _load_jsonl(inputs[1]))
        ]
        sections = [
            item.model_dump(mode="json")
            for item in (Section.model_validate(value) for value in _load_json(inputs[2]))
        ]
        mindmap = MindMap.model_validate(_load_json(inputs[4])).model_dump(mode="json")
    frames = _load_jsonl(files.frames_jsonl)
    data = {
        "schema_version": "1.0",
        "job_id": job_id,
        "title": "P6 前 25 分钟 · 长序列高效架构" if m4b else "课程前五分钟",
        "source_video": manifest.source.bvid,
        "time_range": [manifest.requested_range.start, manifest.requested_range.end],
        "jump_granularity": "paragraph_start",
        "paragraphs": paragraphs,
        "corrections": corrections,
        "sections": sections,
        "highlights": _load_json(inputs[3]),
        "mindmap": mindmap,
        "evidence": _load_jsonl(inputs[5]),
        "formulas": _load_json(analysis_dir / "formulas.json") if m4b else [],
        "glossary": _load_json(analysis_dir / "glossary.json") if m4b else [],
        "frames": [
            {
                "frame_id": item["frame_id"],
                "timestamp": item["timestamp"],
                "url": f"/asset/frame/{item['frame_id']}",
            }
            for item in frames
        ],
        "external_network_required": False,
        "external_model_api_used": False,
        "packet_boundaries_exposed_in_body": False,
    }
    destination.mkdir(parents=True, exist_ok=True)
    atomic_write_text(destination / "index.html", INDEX_HTML)
    atomic_write_text(destination / "app.css", APP_CSS)
    atomic_write_text(destination / "app.js", APP_JS)
    atomic_write_json(destination / "data.json", data)
    stored = {
        "schema_version": "1.0",
        "input_hash": input_hash,
        "generated_marker": GENERATED_MARKER,
        "artifacts": {
            name: hash_file(destination / name)
            for name in ("index.html", "app.css", "app.js", "data.json")
        },
        "third_party_requests": [],
        "host": "127.0.0.1",
        "data_json_bytes": (destination / "data.json").stat().st_size,
        "duration_seconds": manifest.requested_range.end - manifest.requested_range.start,
        "external_model_api_used": False,
    }
    atomic_write_json(manifest_path, stored)
    return {"job_id": job_id, "status": "web_ready", "cache_hit": False, **stored}


def _build_m5_web(job_id: str, *, root: Path, force: bool) -> dict[str, Any]:
    merged = root / "merged/p6-complete"
    manifest = _load_json(merged / "manifest.json")
    inputs = [
        merged / "paragraphs/paragraphs.jsonl",
        merged / "sections/sections.json",
        merged / "glossary/glossary.json",
        merged / "formulas/formulas.json",
        merged / "evidence/evidence-ledger.jsonl",
        merged / "notes/mindmap.json",
        root / "transcript/corrections-incremental/correction-decisions.jsonl",
        root / "frames/canonical/legacy-frame-resolution.jsonl",
        root / "frames/canonical/active-frames.jsonl",
    ]
    if any(not path.is_file() for path in inputs):
        raise StateError("Complete P6 web inputs are incomplete; run lectureflow p6 merge.")
    input_hash = hash_object(
        {
            "inputs": {str(path.relative_to(root)): hash_file(path) for path in inputs},
            "html": hash_object(INDEX_HTML),
            "css": hash_object(APP_CSS),
            "javascript": hash_object(M5_APP_JS),
        }
    )
    destination = merged / "web"
    manifest_path = destination / "web-manifest.json"
    if (
        not force
        and manifest_path.is_file()
        and _load_json(manifest_path).get("input_hash") == input_hash
    ):
        return {
            "job_id": job_id,
            "status": "web_ready",
            "cache_hit": True,
            **_load_json(manifest_path),
        }
    frozen_job = manifest["frozen_job_id"]
    frozen_root = root.parent / frozen_job
    corrections = _load_jsonl(
        frozen_root / "transcript/corrections/correction-decisions.jsonl"
    ) + _load_jsonl(inputs[6])
    active_frozen_ids = {
        item["frame_id"] for item in _load_jsonl(inputs[7]) if item["active_for_analysis"]
    }
    frozen_frames = [
        item
        for item in _load_jsonl(frozen_root / "frames/frames.jsonl")
        if item["frame_id"] in active_frozen_ids
    ]
    incremental_frames = _load_jsonl(inputs[8])
    frames = [
        {
            "frame_id": item["frame_id"],
            "timestamp": item["timestamp"],
            "url": f"/asset/frame/{item['frame_id']}",
        }
        for item in [*frozen_frames, *incremental_frames]
    ]
    sections = _load_json(inputs[1])
    data = {
        "schema_version": "1.0",
        "job_id": manifest["merged_job_id"],
        "title": "P6 完整版 · 大模型前沿架构 Part 2",
        "source_video": "BV1pf421z757",
        "time_range": [0.0, manifest["duration_seconds"]],
        "jump_granularity": "paragraph_start",
        "media_segments": [
            {"timeline_start": 0.0, "timeline_end": 1500.0, "url": "/media/segment/0"},
            {
                "timeline_start": 1500.0,
                "timeline_end": manifest["duration_seconds"],
                "url": "/media/segment/1",
            },
        ],
        "paragraphs": _load_jsonl(inputs[0]),
        "corrections": corrections,
        "sections": sections,
        "highlights": [
            {"category": item["title"], "text": item["summary"], "timestamp": item["start"]}
            for item in sections
        ],
        "mindmap": _load_json(inputs[5]),
        "evidence": _load_jsonl(inputs[4]),
        "formulas": _load_json(inputs[3]),
        "glossary": _load_json(inputs[2])["terms"],
        "frames": frames,
        "human_review_queue": _load_jsonl(merged / "formulas/formula-review-queue.jsonl"),
        "external_network_required": False,
        "external_model_api_used": False,
        "packet_boundaries_exposed_in_body": False,
    }
    atomic_write_text(destination / "index.html", INDEX_HTML)
    atomic_write_text(destination / "app.css", APP_CSS)
    atomic_write_text(destination / "app.js", M5_APP_JS)
    atomic_write_json(destination / "data.json", data)
    stored = {
        "schema_version": "1.0",
        "input_hash": input_hash,
        "generated_marker": "LectureFlow generated complete P6 web",
        "artifacts": {
            name: hash_file(destination / name)
            for name in ("index.html", "app.css", "app.js", "data.json")
        },
        "third_party_requests": [],
        "host": "127.0.0.1",
        "data_json_bytes": (destination / "data.json").stat().st_size,
        "duration_seconds": manifest["duration_seconds"],
        "media_segment_count": 2,
        "external_model_api_used": False,
    }
    atomic_write_json(manifest_path, stored)
    return {"job_id": job_id, "status": "web_ready", "cache_hit": False, **stored}


def _send_bytes(handler: BaseHTTPRequestHandler, data: bytes, content_type: str) -> None:
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(data)


def _serve_range(handler: BaseHTTPRequestHandler, path: Path) -> None:
    size = path.stat().st_size
    start, end = 0, size - 1
    value = handler.headers.get("Range")
    if value:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
        if not match:
            handler.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            return
        left, right = match.groups()
        if not left and not right:
            handler.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            return
        if left:
            start = int(left)
            end = min(int(right), size - 1) if right else size - 1
        else:
            length = min(int(right), size)
            start, end = size - length, size - 1
        if start >= size or end < start:
            handler.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            return
        handler.send_response(HTTPStatus.PARTIAL_CONTENT)
        handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    else:
        handler.send_response(HTTPStatus.OK)
    handler.send_header(
        "Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    )
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Content-Length", str(end - start + 1))
    handler.send_header("Cache-Control", "private, max-age=3600")
    handler.end_headers()
    if handler.command == "HEAD":
        return
    with path.open("rb") as source:
        source.seek(start)
        remaining = end - start + 1
        while remaining:
            chunk = source.read(min(64 * 1024, remaining))
            if not chunk:
                break
            try:
                handler.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                # Browsers routinely cancel an in-flight Range response after seeking.
                return
            remaining -= len(chunk)


def create_server(job_id: str, *, config: AppConfig, host: str, port: int) -> ThreadingHTTPServer:
    if host != "127.0.0.1":
        raise StateError("LectureFlow serve only permits host 127.0.0.1.")
    files, _, _ = load_job_context(job_id, config=config)
    build_web(job_id, config=config)
    if (files.root / "merged/p6-complete/manifest.json").is_file():
        return _create_m5_server(job_id, files=files, config=config, host=host, port=port)
    media = MediaManifest.model_validate(_load_json(files.media_manifest))
    media_path = resolve_media_path(files, media)
    frames = {
        item["frame_id"]: files.root / item["path"] for item in _load_jsonl(files.frames_jsonl)
    }
    static = {
        "/": (files.root / "web/index.html", "text/html; charset=utf-8"),
        "/index.html": (files.root / "web/index.html", "text/html; charset=utf-8"),
        "/app.css": (files.root / "web/app.css", "text/css; charset=utf-8"),
        "/app.js": (files.root / "web/app.js", "text/javascript; charset=utf-8"),
        "/api/data": (files.root / "web/data.json", "application/json; charset=utf-8"),
    }

    class Handler(BaseHTTPRequestHandler):
        server_version = "LectureFlowLocal/0.4"

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            path = unquote(urlsplit(self.path).path)
            if ".." in Path(path).parts or "\\" in path or "\x00" in path:
                self.send_error(HTTPStatus.FORBIDDEN, "Path traversal is forbidden")
                return
            if path == "/media/video":
                _serve_range(self, media_path)
                return
            if path.startswith("/asset/frame/"):
                frame_id = path.removeprefix("/asset/frame/")
                target = frames.get(frame_id)
                if target is None or not target.is_file():
                    self.send_error(HTTPStatus.NOT_FOUND, "Frame not found")
                    return
                _serve_range(self, target)
                return
            item = static.get(path)
            if item is None or not item[0].is_file():
                self.send_error(HTTPStatus.NOT_FOUND, "LectureFlow resource not found")
                return
            _send_bytes(self, item[0].read_bytes(), item[1])

        def log_message(self, format: str, *args: object) -> None:
            return

    try:
        return ThreadingHTTPServer((host, port), Handler)
    except OSError as error:
        raise StateError(
            f"Cannot start LectureFlow at http://{host}:{port}; the port may be in use: {error}"
        ) from error


def _create_m5_server(
    job_id: str, *, files: Any, config: AppConfig, host: str, port: int
) -> ThreadingHTTPServer:
    manifest = _load_json(files.root / "merged/p6-complete/manifest.json")
    frozen_files, _, _ = load_job_context(manifest["frozen_job_id"], config=config)
    incremental_files, _, _ = load_job_context(manifest["incremental_job_id"], config=config)
    frozen_media = resolve_media_path(
        frozen_files, MediaManifest.model_validate(_load_json(frozen_files.media_manifest))
    )
    incremental_media = resolve_media_path(
        incremental_files,
        MediaManifest.model_validate(_load_json(incremental_files.media_manifest)),
    )
    frame_paths = {
        item["frame_id"]: frozen_files.root / item["path"]
        for item in _load_jsonl(frozen_files.frames_jsonl)
    }
    frame_paths.update(
        {
            item["frame_id"]: incremental_files.root / item["path"]
            for item in _load_jsonl(
                incremental_files.root / "frames/canonical/incremental-frames.jsonl"
            )
        }
    )
    web = files.root / "merged/p6-complete/web"
    static = {
        "/": (web / "index.html", "text/html; charset=utf-8"),
        "/index.html": (web / "index.html", "text/html; charset=utf-8"),
        "/app.css": (web / "app.css", "text/css; charset=utf-8"),
        "/app.js": (web / "app.js", "text/javascript; charset=utf-8"),
        "/api/data": (web / "data.json", "application/json; charset=utf-8"),
    }

    class Handler(BaseHTTPRequestHandler):
        server_version = "LectureFlowLocal/0.5"

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            path = unquote(urlsplit(self.path).path)
            if ".." in Path(path).parts or "\\" in path or "\x00" in path:
                self.send_error(HTTPStatus.FORBIDDEN, "Path traversal is forbidden")
                return
            if path in {"/media/video", "/media/segment/0"}:
                _serve_range(self, frozen_media)
                return
            if path == "/media/segment/1":
                _serve_range(self, incremental_media)
                return
            if path.startswith("/asset/frame/"):
                target = frame_paths.get(path.removeprefix("/asset/frame/"))
                if target is None or not target.is_file():
                    self.send_error(HTTPStatus.NOT_FOUND, "Frame not found")
                    return
                _serve_range(self, target)
                return
            item = static.get(path)
            if item is None or not item[0].is_file():
                self.send_error(HTTPStatus.NOT_FOUND, "LectureFlow resource not found")
                return
            _send_bytes(self, item[0].read_bytes(), item[1])

        def log_message(self, format: str, *args: object) -> None:
            return

    try:
        return ThreadingHTTPServer((host, port), Handler)
    except OSError as error:
        raise StateError(
            f"Cannot start complete P6 at http://{host}:{port}; port may be in use: {error}"
        ) from error
