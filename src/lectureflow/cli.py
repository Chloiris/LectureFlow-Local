from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from lectureflow.asr import (
    ASRProcessResult,
    asr_doctor,
    inspect_asr,
    preflight_job,
    transcribe_job,
)
from lectureflow.atomic import atomic_write_json
from lectureflow.config import load_config
from lectureflow.constants import MILESTONES, PIPELINE_VERSION
from lectureflow.doctor import run_doctor
from lectureflow.errors import LectureFlowError, StageNotImplementedError, StateError
from lectureflow.evidence import validate_evidence as validate_job_evidence
from lectureflow.frames import extract_frames as extract_job_frames
from lectureflow.frames import inspect_frames, rebuild_contact_sheets
from lectureflow.m3b_state import record_validated_artifact_stage
from lectureflow.m4a import build_corrections, build_paragraphs, render_m4a
from lectureflow.m4b import (
    build_technical_packets,
    build_technical_paragraphs,
    merge_m4b,
    validate_m4b,
)
from lectureflow.m4b.service import validate_all_packet_analyses, validate_technical_packets
from lectureflow.m4c import (
    apply_evidence_backed_fixes,
    audit_corrections,
    audit_dedup,
    audit_evidence,
    audit_formulas,
    audit_packets,
    audit_paragraphs,
    audit_rendering,
    build_audit_report,
)
from lectureflow.m5 import (
    analyze_incremental_packets,
    build_incremental_packets,
    build_incremental_paragraphs,
    canonicalize_incremental_frames,
    freeze_p6_baseline,
    materialize_incremental_transcript,
    merge_p6_complete,
    plan_p6_completion,
    validate_complete_p6_evidence,
    validate_incremental_packets,
)
from lectureflow.media import acquire_media, inspect_media
from lectureflow.obsidian import export_obsidian as export_job_obsidian
from lectureflow.packets import build_packet, validate_analysis, validate_packet
from lectureflow.pipeline import job_status, load_job_context, prepare_job
from lectureflow.sources import identify_source
from lectureflow.subtitles.service import (
    SUPPORTED_SUBTITLE_SUFFIXES,
    SubtitleProcessResult,
    export_job_subtitles,
    fetch_bilibili_subtitles,
    import_local_subtitle,
    inspect_subtitles,
    resume_to_transcript,
)
from lectureflow.web import build_web, create_server

app = typer.Typer(
    name="lectureflow",
    help="可信、可审计、可断点续跑的本地课程转知识库工作流。",
    no_args_is_help=True,
    invoke_without_command=True,
)
subtitles_app = typer.Typer(
    name="subtitles",
    help="获取、导入、检查和重新导出可审计字幕。",
    no_args_is_help=True,
)
app.add_typer(subtitles_app, name="subtitles")
media_app = typer.Typer(name="media", help="获取和检查本地可审计媒体。", no_args_is_help=True)
frames_app = typer.Typer(
    name="frames", help="确定性抽帧、去重、质量检查和联系表。", no_args_is_help=True
)
asr_app = typer.Typer(
    name="asr", help="本地 MLX-Whisper 能力、预检、转写和审计。", no_args_is_help=True
)
packet_app = typer.Typer(
    name="packet", help="构建和校验单一 M3B Agent Packet。", no_args_is_help=True
)
evidence_app = typer.Typer(name="evidence", help="校验证据账本与覆盖率。", no_args_is_help=True)
paragraphs_app = typer.Typer(
    name="paragraphs", help="构建和校验段落级时间轴。", no_args_is_help=True
)
corrections_app = typer.Typer(name="corrections", help="构建可审计纠错层。", no_args_is_help=True)
packets_app = typer.Typer(name="packets", help="构建和校验 M4B 多 Packet。", no_args_is_help=True)
audit_app = typer.Typer(name="audit", help="执行 M4C 证据约束内容质量审计。", no_args_is_help=True)
p6_app = typer.Typer(name="p6", help="冻结、增量处理并合并完整 P6。", no_args_is_help=True)
app.add_typer(media_app, name="media")
app.add_typer(frames_app, name="frames")
app.add_typer(asr_app, name="asr")
app.add_typer(packet_app, name="packet")
app.add_typer(evidence_app, name="evidence")
app.add_typer(paragraphs_app, name="paragraphs")
app.add_typer(corrections_app, name="corrections")
app.add_typer(packets_app, name="packets")
app.add_typer(audit_app, name="audit")
app.add_typer(p6_app, name="p6")

M5_FROZEN_JOB = "bili-bv1pf421z757-p6-ed032fd7"


def _is_m5_incremental(manifest: object) -> bool:
    source = getattr(manifest, "source", None)
    requested = getattr(manifest, "requested_range", None)
    return getattr(source, "page", None) == 6 and getattr(requested, "start", None) == 1500


ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", help="TOML 配置覆盖文件；相对 workspace 路径基于该文件。"),
]


def _emit_error(error: LectureFlowError) -> None:
    typer.echo(f"Error: {error}", err=True)
    raise typer.Exit(code=error.exit_code)


def _json(value: object) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _force_subtitle_stage(value: str | None) -> bool:
    if value not in {None, "subtitle_ready"}:
        raise StateError("Subtitle commands only support --force-stage subtitle_ready.")
    return value == "subtitle_ready"


def _emit_subtitle_result(result: SubtitleProcessResult, *, json_output: bool) -> None:
    payload = result.as_dict()
    if json_output:
        _json(payload)
        return
    typer.echo(f"Job: {payload['job_id']}")
    typer.echo(f"Status: {payload['status']}")
    typer.echo(f"Cache: {'hit' if payload['cache_hit'] else 'miss'}")
    sources = payload["selected_sources"]
    selected = ", ".join(f"{part}={source or 'none'}" for part, source in sources.items())
    typer.echo(f"Selected subtitles: {selected or 'none'}")
    typer.echo(f"Transcript: {payload['transcript_root']}")
    if payload.get("message"):
        typer.echo(str(payload["message"]))


def _emit_asr_result(result: ASRProcessResult, *, json_output: bool) -> None:
    payload = result.as_dict()
    if json_output:
        _json(payload)
        return
    typer.echo(f"Job: {payload['job_id']}")
    typer.echo(f"Status: {payload['status']}")
    typer.echo(f"Backend/model: {payload['backend']} / {payload['model']}")
    typer.echo(
        "Cache: "
        f"audio={payload['audio_cache_hit']} model={payload['model_cache_hit']} "
        f"transcript={payload['transcript_cache_hit']}"
    )
    typer.echo(f"Segments: {payload['segment_count']}")


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", help="显示版本并退出。", is_eager=True),
    ] = False,
) -> None:
    if version:
        typer.echo(PIPELINE_VERSION)
        raise typer.Exit()


@app.command()
def doctor(
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json", help="输出稳定 JSON。")] = False,
    strict: Annotated[bool, typer.Option("--strict", help="可选能力缺失也返回失败。")] = False,
) -> None:
    """只读检查环境；不联网、不安装依赖、不检查登录态。"""
    try:
        settings = load_config(config)
        report = run_doctor(workspace_root=settings.workspace_root)
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    if json_output:
        _json(report)
    else:
        for check in report["checks"]:
            marker = {"pass": "OK", "warn": "WARN", "fail": "FAIL"}[check["status"]]
            typer.echo(f"[{marker}] {check['name']}: {check['detail']}")
            if check["remediation"] and check["status"] != "pass":
                typer.echo(f"       {check['remediation']}")
        typer.echo(
            "Core environment is ready."
            if report["ok"]
            else "Core environment has blocking problems."
        )
    if not report["ok"] or (strict and not report["strict_ok"]):
        raise typer.Exit(code=1)


@p6_app.command("plan")
def p6_plan_command(
    source: Annotated[str, typer.Argument(help="必须为 BV1pf421z757 P6 URL。")],
    frozen_job: Annotated[str, typer.Option("--frozen-job")] = M5_FROZEN_JOB,
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """从已缓存的权威元数据制定 P6 增量完成计划；不下载媒体。"""
    try:
        identified = identify_source(source)
        if identified.bvid != "BV1pf421z757" or identified.page != 6:
            raise StateError("M5 plan is restricted to BV1pf421z757 P6.")
        result = plan_p6_completion(frozen_job, config=load_config(config))
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    if json_output:
        _json(result)
    else:
        typer.echo(f"P6 duration: {result['duration_seconds']:.2f}s")
        typer.echo(f"Incremental range: {result['incremental_range']}")
        typer.echo(f"Estimated incremental media: {result['estimated_incremental_download_bytes']}")


@app.command("freeze")
def freeze_command(
    job_id: Annotated[str, typer.Argument(help="已通过 M4C 的 P6 00:00–25:00 job ID。")],
    range_value: Annotated[str, typer.Option("--range")] = "0:1500",
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """冻结 M4C 基线哈希与 ID 范围；不修改任何上游产物。"""
    try:
        if range_value != "0:1500":
            raise StateError("M5 freeze is restricted to --range 0:1500.")
        result = freeze_p6_baseline(job_id, config=load_config(config))
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    if json_output:
        _json(result)
    else:
        typer.echo(f"Frozen: {result['baseline']}")
        typer.echo(f"Hash: {result['frozen_group_hash']}")


@p6_app.command("materialize-transcript")
def p6_materialize_transcript_command(
    incremental_job: Annotated[str, typer.Argument(help="P6 1500 秒至结尾任务 ID。")],
    frozen_job: Annotated[str, typer.Option("--frozen-job")] = M5_FROZEN_JOB,
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """将增量媒体局部 ASR 时间映射为完整 P6 绝对时间和追加 ID。"""
    try:
        result = materialize_incremental_transcript(
            incremental_job, frozen_job_id=frozen_job, config=load_config(config)
        )
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    _json(result) if json_output else typer.echo(
        f"Incremental transcript: {result['segment_count']} segments "
        f"({'cache hit' if result['cache_hit'] else 'ready'})"
    )


@p6_app.command("merge")
def p6_merge_command(
    incremental_job: Annotated[str, typer.Option("--incremental-job")],
    frozen_job: Annotated[str, typer.Option("--frozen-job")] = M5_FROZEN_JOB,
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """只在展示层合并冻结与增量产物，不重跑任何上游阶段。"""
    try:
        result = merge_p6_complete(
            incremental_job, frozen_job_id=frozen_job, config=load_config(config)
        )
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    _json(result) if json_output else typer.echo(
        f"Merged P6: {result['merged_job_id']} ({result['counts']['sections']} sections)"
    )


@app.command()
def prepare(
    source: Annotated[str, typer.Argument(help="Bilibili URL/BV/av ID 或本地视频路径。")],
    start: Annotated[float, typer.Option("--start", min=0, help="处理起点（秒）。")] = 0.0,
    end: Annotated[float | None, typer.Option("--end", min=0, help="处理终点（秒）。")] = None,
    config: ConfigOption = None,
    force_stage: Annotated[
        str | None,
        typer.Option("--force-stage", help="仅失效并重跑指定阶段；prepare 支持 metadata_ready。"),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="输出稳定 JSON。")] = False,
) -> None:
    """注册任务并获取元数据；重复输入默认命中缓存。"""
    try:
        result = prepare_job(
            source,
            config=load_config(config),
            start=start,
            end=end,
            force_stage=force_stage,
        )
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    payload = {
        "job_id": result.job_id,
        "job_dir": str(result.job_dir),
        "metadata_path": str(result.metadata_path) if result.metadata_path else None,
        "cache_hit": result.cache_hit,
        "current_milestone": result.current_milestone,
    }
    if json_output:
        _json(payload)
    else:
        typer.echo(f"Job: {result.job_id}")
        typer.echo(f"Metadata: {'cache hit' if result.cache_hit else 'ready'}")
        typer.echo(f"Workspace: {result.job_dir}")


@app.command()
def status(
    job_id: Annotated[str, typer.Argument(help="prepare 返回的安全任务 ID。")],
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json", help="输出稳定 JSON。")] = False,
) -> None:
    """只读显示任务阶段、尝试历史和恢复命令。"""
    try:
        report = job_status(job_id, config=load_config(config))
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    if json_output:
        _json(report)
        return
    typer.echo(f"Job: {report['job_id']}")
    typer.echo(f"Status: {report['overall_status']}")
    typer.echo(f"Last successful: {report['last_successful_milestone'] or '-'}")
    for name in MILESTONES:
        stage = report["stages"][name]
        if stage["status"] != "pending":
            typer.echo(f"  {name}: {stage['status']} ({stage['attempt_count']} attempt(s))")
            latest = stage["latest_attempt"]
            if latest and latest["error"]:
                typer.echo(f"    error: {latest['error']}")
    if report["suspected_interruption"]:
        typer.echo("Possible interrupted stage; run resume while no other process is active.")
    if report["subtitle_result"]:
        subtitle_result = report["subtitle_result"]
        typer.echo(f"Subtitle result: {subtitle_result['status']}")
        for part, source in subtitle_result.get("selected_sources", {}).items():
            typer.echo(f"  {part}: {source or 'none'}")
    if report["media_result"]:
        typer.echo(f"Media result: {report['media_result']['status']}")
    if report["frame_result"]:
        typer.echo(
            f"Frame result: {report['frame_result']['status']} "
            f"({report['frame_result'].get('final_count') or 0} final)"
        )
    typer.echo(f"Resume: {report['resume_command']}")


@app.command()
def resume(
    job_id: Annotated[str, typer.Argument(help="要恢复的任务 ID。")],
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json", help="输出稳定 JSON。")] = False,
) -> None:
    """从最近可恢复阶段继续；已开始抽帧的任务恢复到 frames_ready。"""
    try:
        settings = load_config(config)
        files, _, state = load_job_context(job_id, config=settings)
        m4a_plan_exists = (
            hasattr(files, "root") and (files.root / "analyses/m4a/paragraph-plan.json").is_file()
        )
        if m4a_plan_exists:
            m4a_result = render_m4a(job_id, config=settings)
            web_result = build_web(job_id, config=settings)
            payload = {"m4a": m4a_result, "web": web_result}
            if json_output:
                _json(payload)
            else:
                typer.echo(f"Job: {job_id}")
                typer.echo(f"M4A cache: {'hit' if m4a_result['cache_hit'] else 'miss'}")
                typer.echo(f"Web cache: {'hit' if web_result['cache_hit'] else 'miss'}")
            return
        asr_manifest_exists = (
            hasattr(files, "root") and (files.root / "transcript/asr/asr-manifest.json").is_file()
        )
        transcript_stage = state.stages.get("transcript_ready")
        latest_transcript_attempt = (
            transcript_stage.attempts[-1]
            if transcript_stage is not None and transcript_stage.attempts
            else None
        )
        interrupted_asr = (
            transcript_stage is not None
            and transcript_stage.status.value in {"running", "failed", "succeeded"}
            and latest_transcript_attempt is not None
            and latest_transcript_attempt.tool in {"mlx-whisper", "vibeasr-bitnet"}
        )
        if asr_manifest_exists or interrupted_asr:
            asr_result = transcribe_job(job_id, config=settings, offline=True)
            _emit_asr_result(asr_result, json_output=json_output)
            return
        if files.media_manifest.is_file() or state.stages["frames_ready"].status.value != "pending":
            frame_result = extract_job_frames(job_id, config=settings)
            if json_output:
                _json(frame_result.model_dump(mode="json"))
            else:
                typer.echo(f"Job: {frame_result.job_id}")
                typer.echo(f"Frames: {frame_result.final_count}")
                typer.echo(f"Cache: {'hit' if frame_result.cache_hit else 'miss'}")
            return
        result = resume_to_transcript(job_id, config=settings)
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    _emit_subtitle_result(result, json_output=json_output)


@asr_app.command("doctor")
def asr_doctor_command(
    backend: Annotated[str, typer.Option("--backend")] = "mlx-whisper",
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """只读检查所选本地 ASR 运行时与模型；不联网下载。"""
    try:
        report = asr_doctor(load_config(config), offline=True, backend=backend)
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    if json_output:
        _json(report)
    else:
        typer.echo(f"Backend: {report['backend']}")
        typer.echo(f"Available: {report['available']}")
        typer.echo(f"Python: {report['python']}")
        if report["backend"] == "mlx-whisper":
            typer.echo(f"MLX: {report['mlx_version']} (Metal={report['metal_available']})")
            typer.echo(f"MLX-Whisper: {report['mlx_whisper_version']}")
        else:
            typer.echo(f"Executable: {report['executable']}")
            typer.echo(f"Threads: {report['threads']}")
        typer.echo(f"Model: {report['model']}@{report['revision']}")
        typer.echo(f"Model cached: {report['model_cached']}")
        if report["error"]:
            typer.echo(f"Error: {report['error']}")
    if not report["available"]:
        raise typer.Exit(code=2)


@asr_app.command("preflight")
def asr_preflight_command(
    job_id: Annotated[str, typer.Argument()],
    backend: Annotated[str, typer.Option("--backend")] = "mlx-whisper",
    seconds: Annotated[float, typer.Option("--seconds", min=20, max=30)] = 30.0,
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """用固定本地模型转写 20–30 秒，成功后才允许执行五分钟。"""
    try:
        report = preflight_job(job_id, config=load_config(config), seconds=seconds, backend=backend)
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    if json_output:
        _json(report)
    else:
        typer.echo(f"Preflight: {report['status']} ({report['segment_count']} segments)")
        typer.echo(f"Elapsed: {report['elapsed_seconds']:.3f}s")
        typer.echo(f"Sample: {report['sample_text']}")


@asr_app.command("transcribe")
def asr_transcribe_command(
    job_id: Annotated[str, typer.Argument()],
    backend: Annotated[str, typer.Option("--backend")] = "mlx-whisper",
    start: Annotated[float, typer.Option("--start", min=0)] = 0.0,
    end: Annotated[float, typer.Option("--end", min=0)] = 300.0,
    offline: Annotated[bool, typer.Option("--offline", help="严格禁止模型下载。")] = False,
    force_stage: Annotated[str | None, typer.Option("--force-stage")] = None,
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """用显式选择的本地模型转写已注册媒体；绝不调用外部 ASR。"""
    try:
        if backend not in {"mlx-whisper", "vibeasr-bitnet"}:
            raise StateError("ASR backend must be mlx-whisper or vibeasr-bitnet.")
        settings = load_config(config)
        if force_stage not in {None, "transcript_ready"}:
            raise StateError("ASR only supports --force-stage transcript_ready.")
        result = transcribe_job(
            job_id,
            config=settings,
            backend=backend,
            offline=offline,
            force=force_stage == "transcript_ready",
            start=start,
            end=end,
        )
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    _emit_asr_result(result, json_output=json_output)


@asr_app.command("inspect")
def asr_inspect_command(
    job_id: Annotated[str, typer.Argument()],
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """只读复验音频、ASR manifest、原始转写和全部导出哈希。"""
    try:
        report = inspect_asr(job_id, config=load_config(config))
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    if json_output:
        _json(report)
    else:
        typer.echo(f"Job: {job_id}")
        typer.echo(f"Audio ready: {report['audio_ready']}")
        typer.echo(f"Transcript integrity: {report['transcript_integrity']}")


@media_app.command("acquire")
def media_acquire(
    job_id: Annotated[str, typer.Argument(help="prepare 返回的任务 ID。")],
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """复用本地媒体，或按已注册范围下载 B站媒体（上限 500 MiB）。"""
    try:
        result = acquire_media(job_id, config=load_config(config))
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    if json_output:
        _json(result.as_dict())
    else:
        typer.echo(f"Job: {result.job_id}")
        typer.echo(f"Media: {'cache hit' if result.cache_hit else 'ready'}")
        typer.echo(f"Manifest: {result.media_manifest_path}")


@media_app.command("inspect")
def media_inspect(
    job_id: Annotated[str, typer.Argument()],
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """只读验证媒体清单和完整文件哈希。"""
    try:
        report = inspect_media(job_id, config=load_config(config))
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    if json_output:
        _json(report)
    else:
        typer.echo(f"Job: {job_id}")
        typer.echo(f"Integrity: {'ok' if report['integrity']['sha256_matches'] else 'failed'}")


def _emit_frame_result(result: object, *, json_output: bool) -> None:
    payload = result.model_dump(mode="json")  # type: ignore[attr-defined]
    if json_output:
        _json(payload)
        return
    typer.echo(f"Job: {payload['job_id']}")
    typer.echo(f"Frames: {payload['final_count']}")
    typer.echo(f"Candidates: {payload['candidate_count']}")
    typer.echo(f"Cache: {'hit' if payload['cache_hit'] else 'miss'}")


@frames_app.command("extract")
def frames_extract(
    job_id: Annotated[str, typer.Argument()],
    config: ConfigOption = None,
    force_stage: Annotated[
        str | None, typer.Option("--force-stage", help="仅允许 frames_ready。")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """生成候选帧、全局去重结果、质量报告、最终帧和联系表。"""
    try:
        if force_stage not in {None, "frames_ready"}:
            raise StateError("frames extract only supports --force-stage frames_ready.")
        result = extract_job_frames(
            job_id, config=load_config(config), force=force_stage == "frames_ready"
        )
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    _emit_frame_result(result, json_output=json_output)


@frames_app.command("inspect")
def frames_inspect(
    job_id: Annotated[str, typer.Argument()],
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """只读检查抽帧状态、产物哈希和覆盖报告。"""
    try:
        report = inspect_frames(job_id, config=load_config(config))
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    if json_output:
        _json(report)
    else:
        typer.echo(f"Job: {job_id}")
        typer.echo(f"Integrity: {'ok' if report['integrity'] else 'failed'}")
        typer.echo(f"Frames: {(report.get('result') or {}).get('final_count', 0)}")


@frames_app.command("contact-sheet")
def frames_contact_sheet(
    job_id: Annotated[str, typer.Argument()],
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """只从现有最终帧重建本地联系表，不重新抽帧。"""
    try:
        report = rebuild_contact_sheets(job_id, config=load_config(config))
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    if json_output:
        _json(report)
    else:
        typer.echo(f"Contact sheets: {len(report['contact_sheet_paths'])}")


@frames_app.command("canonicalize")
def frames_canonicalize_command(
    job_id: Annotated[str, typer.Argument()],
    frozen_job: Annotated[str, typer.Option("--frozen-job")] = M5_FROZEN_JOB,
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """为 M5 新增帧分配稳定 ID，并建立非破坏式重复别名。"""
    try:
        result = canonicalize_incremental_frames(
            job_id, frozen_job_id=frozen_job, config=load_config(config)
        )
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    _json(result) if json_output else typer.echo(
        f"Active frames: {result['incremental_active_frame_count']}"
    )


@subtitles_app.command("fetch")
def subtitles_fetch(
    source: Annotated[str, typer.Argument(help="B站 URL、BV 号或 av 号。")],
    start: Annotated[float, typer.Option("--start", min=0)] = 0.0,
    end: Annotated[float | None, typer.Option("--end", min=0)] = None,
    config: ConfigOption = None,
    force_stage: Annotated[
        str | None,
        typer.Option("--force-stage", help="仅允许 subtitle_ready。"),
    ] = None,
    local_subtitle: Annotated[
        Path | None,
        typer.Option(
            "--local-subtitle",
            help="仅在远端无字幕时使用；多 P 必须先用 ?p=N 选定一 P。",
        ),
    ] = None,
    local_language: Annotated[
        str,
        typer.Option("--local-language", help="本地后备字幕语言标签。"),
    ] = "und",
    offline: Annotated[
        bool,
        typer.Option("--offline", help="只使用已验证缓存；缓存缺失时禁止联网并明确失败。"),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="输出稳定 JSON。")] = False,
) -> None:
    """获取公开 B站字幕；不读取 Cookie，已下载响应默认复用。"""
    try:
        result = fetch_bilibili_subtitles(
            source,
            config=load_config(config),
            start=start,
            end=end,
            force_stage=_force_subtitle_stage(force_stage),
            local_subtitle=local_subtitle,
            local_language=local_language,
            offline=offline,
        )
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    _emit_subtitle_result(result, json_output=json_output)


@subtitles_app.command("import")
def subtitles_import(
    path: Annotated[Path, typer.Argument(help="UTF-8 的 JSON/SRT/VTT/TXT 字幕。")],
    language: Annotated[str, typer.Option("--language", help="BCP-47 语言标签。")] = "und",
    config: ConfigOption = None,
    force_stage: Annotated[
        str | None,
        typer.Option("--force-stage", help="仅允许 subtitle_ready。"),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="输出稳定 JSON。")] = False,
) -> None:
    """导入本地字幕；无时间 TXT 保持 untimed，不伪造时间点。"""
    try:
        result = import_local_subtitle(
            path,
            config=load_config(config),
            language=language,
            force_stage=_force_subtitle_stage(force_stage),
        )
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    _emit_subtitle_result(result, json_output=json_output)


@subtitles_app.command("inspect")
def subtitles_inspect(
    job_id: Annotated[str, typer.Argument(help="字幕任务 ID。")],
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json", help="输出稳定 JSON。")] = False,
) -> None:
    """只读检查选择结果和阶段状态。"""
    try:
        report = inspect_subtitles(job_id, config=load_config(config))
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    if json_output:
        _json(report)
    else:
        typer.echo(f"Job: {job_id}")
        typer.echo(f"Subtitle stage: {report['subtitle_stage']}")
        typer.echo(f"Transcript stage: {report['transcript_stage']}")
        result = report["result"] or {}
        for part, source in result.get("selected_sources", {}).items():
            typer.echo(f"  {part}: {source or 'none'}")


@subtitles_app.command("export")
def subtitles_export(
    job_id: Annotated[str, typer.Argument(help="已有统一时间轴的任务 ID。")],
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json", help="输出稳定 JSON。")] = False,
) -> None:
    """从不可变 original.json 重新生成 JSONL/TXT/SRT/VTT 和质量报告。"""
    try:
        result = export_job_subtitles(job_id, config=load_config(config))
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    _emit_subtitle_result(result, json_output=json_output)


def _pending(stage: str) -> None:
    _emit_error(
        StageNotImplementedError(
            f"Stage '{stage}' is not implemented through M2; no data was changed. "
            "See PROJECT_STATUS.md for the active milestone."
        )
    )


@app.command()
def transcribe(
    job_id: Annotated[str, typer.Argument()],
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """获取/恢复字幕；本地媒体 ASR 未配置时给出明确错误。"""
    try:
        result = resume_to_transcript(job_id, config=load_config(config))
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    _emit_subtitle_result(result, json_output=json_output)


@app.command("extract-frames")
def extract_frames_alias(
    job_id: Annotated[str, typer.Argument()],
    config: ConfigOption = None,
    force_stage: Annotated[str | None, typer.Option("--force-stage")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """兼容入口：等价于 frames extract。"""
    frames_extract(job_id, config, force_stage, json_output)


@app.command("build-packets")
def build_packets(
    job_id: Annotated[str, typer.Argument()],
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """兼容入口：构建唯一的 M3B P0001 Packet。"""
    packet_build_command(job_id, 0.0, 300.0, config, json_output)


@packet_app.command("build")
def packet_build_command(
    job_id: Annotated[str, typer.Argument()],
    start: Annotated[float, typer.Option("--start", min=0)] = 0.0,
    end: Annotated[float, typer.Option("--end", min=0)] = 300.0,
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """从真实五分钟字幕、M2 帧与 M3A 观察构建 P0001。"""
    try:
        if start != 0.0 or end != 300.0:
            raise StateError("M3B packet is restricted to 0–300 seconds.")
        result = build_packet(job_id, config=load_config(config))
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    if json_output:
        _json(result)
    else:
        typer.echo(f"Packet: {result['packet_id']}")
        typer.echo(f"Cache: {'hit' if result['cache_hit'] else 'miss'}")


@packet_app.command("validate")
def packet_validate_command(
    job_id: Annotated[str, typer.Argument()],
    packet_id: Annotated[str, typer.Argument()] = "P0001",
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """校验 P0001 引用、图片和 Packet 哈希。"""
    try:
        files, _, _ = load_job_context(job_id, config=load_config(config))
        result = validate_packet(files.root, packet_id)
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    if json_output:
        _json(result)
    else:
        typer.echo(f"Packet {packet_id}: valid")


@app.command()
def validate(job_id: Annotated[str, typer.Argument()]) -> None:
    """校验 M3B 分析 JSON、图片 provenance 与证据引用。"""
    try:
        settings = load_config()
        files, _, _ = load_job_context(job_id, config=settings)
        result = validate_analysis(files.root)
        record_validated_artifact_stage(
            job_id,
            config=settings,
            stage="analysis_ready",
            artifact=Path("analyses/P0001-analysis.json"),
            tool="current-codex-vision",
        )
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    _json(result)


@evidence_app.command("validate")
def evidence_validate_command(
    job_id: Annotated[str, typer.Argument()],
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """校验证据来源、时间范围、联合证据形状与覆盖报告。"""
    try:
        settings = load_config(config)
        files, manifest, _ = load_job_context(job_id, config=settings)
        if (
            _is_m5_incremental(manifest)
            and (files.root / "merged/p6-complete/manifest.json").is_file()
        ):
            frozen_files, _, _ = load_job_context(M5_FROZEN_JOB, config=settings)
            result = validate_complete_p6_evidence(files.root, frozen_files.root)
            artifact = Path("merged/p6-complete/evidence/evidence-ledger.jsonl")
            note = files.root / "merged/p6-complete/notes/00-P6总览.md"
        elif (files.root / "analyses/m4b/m4b-summary.json").is_file():
            result = validate_m4b(files.root)
            artifact = Path("evidence/evidence-ledger.jsonl")
            note = files.root / "notes/P6-00-25-integrated-note.md"
        else:
            result = validate_job_evidence(files.root)
            artifact = Path("evidence/evidence-ledger.jsonl")
            note = files.root / "notes/P0001-five-minute-integrated-note.md"
        record_validated_artifact_stage(
            job_id,
            config=settings,
            stage="evidence_ready",
            artifact=artifact,
            tool="lectureflow-evidence-validator",
        )
        if note.is_file():
            record_validated_artifact_stage(
                job_id,
                config=settings,
                stage="notes_ready",
                artifact=note.relative_to(files.root),
                tool="current-codex-note-renderer",
            )
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    if json_output:
        _json(result)
    else:
        typer.echo(f"Evidence: valid ({result['evidence_count']} records)")


@paragraphs_app.command("build")
def paragraphs_build_command(
    job_id: Annotated[str, typer.Argument()],
    config: ConfigOption = None,
    force_stage: Annotated[str | None, typer.Option("--force-stage")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """从现有 ASR segment 与 Codex 分组计划构建段落级时间轴。"""
    try:
        if force_stage not in {None, "paragraphs_ready"}:
            raise StateError("paragraphs build only supports --force-stage paragraphs_ready.")
        settings = load_config(config)
        _, manifest, _ = load_job_context(job_id, config=settings)
        if _is_m5_incremental(manifest):
            result = build_incremental_paragraphs(
                job_id, frozen_job_id=M5_FROZEN_JOB, config=settings
            )
        elif manifest.requested_range.end == 1500 and manifest.source.page == 6:
            result = build_technical_paragraphs(
                job_id, config=settings, force=force_stage == "paragraphs_ready"
            )
        else:
            result = build_paragraphs(
                job_id, config=settings, force=force_stage == "paragraphs_ready"
            )
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    if json_output:
        _json(result)
    else:
        typer.echo(f"Paragraphs: {result['paragraph_count']}")
        typer.echo(f"Cache: {'hit' if result['cache_hit'] else 'miss'}")


@packets_app.command("build")
def packets_build_command(
    job_id: Annotated[str, typer.Argument()],
    config: ConfigOption = None,
    force_stage: Annotated[str | None, typer.Option("--force-stage")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """构建 M4B 五包或 M5 增量范围的公式感知 Packet。"""
    try:
        if force_stage not in {None, "packets_ready"}:
            raise StateError("packets build only supports --force-stage packets_ready.")
        settings = load_config(config)
        _, manifest, _ = load_job_context(job_id, config=settings)
        if _is_m5_incremental(manifest):
            result = build_incremental_packets(job_id, frozen_job_id=M5_FROZEN_JOB, config=settings)
        else:
            result = build_technical_packets(
                job_id,
                config=settings,
                force=force_stage == "packets_ready",
            )
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    if json_output:
        _json(result)
    else:
        typer.echo(f"Packets: {result['packet_count']}")
        typer.echo(f"Cache: {'hit' if result['cache_hit'] else 'miss'}")


@packets_app.command("validate")
def packets_validate_command(
    job_id: Annotated[str, typer.Argument()],
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """验证 Packet 哈希、ID、overlap 与 primary 唯一覆盖。"""
    try:
        settings = load_config(config)
        files, manifest, _ = load_job_context(job_id, config=settings)
        if _is_m5_incremental(manifest):
            frozen_files, _, _ = load_job_context(M5_FROZEN_JOB, config=settings)
            result = validate_incremental_packets(files.root, frozen_files.root)
            artifact = Path("packets/incremental/packet-manifest.json")
        else:
            result = validate_technical_packets(files.root)
            artifact = Path("packets/m4b-packet-manifest.json")
        record_validated_artifact_stage(
            job_id,
            config=settings,
            stage="packets_ready",
            artifact=artifact,
            tool="lectureflow-packet-validator",
        )
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    if json_output:
        _json(result)
    else:
        typer.echo(f"Packets: valid ({result['packet_count']})")


@app.command("analyze-packets")
def analyze_packets_command(
    job_id: Annotated[str, typer.Argument()],
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """校验当前 Codex 串行写入的多模态 Packet 分析。"""
    try:
        settings = load_config(config)
        files, manifest, _ = load_job_context(job_id, config=settings)
        if _is_m5_incremental(manifest):
            result = analyze_incremental_packets(
                job_id, frozen_job_id=M5_FROZEN_JOB, config=settings
            )
            artifact = Path("analyses/incremental-summary.json")
        else:
            result = validate_all_packet_analyses(files.root)
            artifact = Path("analyses/packets/analysis-state.json")
        record_validated_artifact_stage(
            job_id,
            config=settings,
            stage="analysis_ready",
            artifact=artifact,
            tool="current-codex-vision",
        )
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    if json_output:
        _json(result)
    else:
        typer.echo(
            f"Analyses: {result['packet_count']} packets; "
            f"{result.get('formula_count', result.get('formulas'))} formulas; "
            f"{result.get('image_count', result.get('images_opened'))} opened frames"
        )


@app.command("merge")
def merge_command(
    job_id: Annotated[str, typer.Argument()],
    config: ConfigOption = None,
    force: Annotated[bool, typer.Option("--force")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """合并已校验 Packet；不重读原图、不触发 ASR 或抽帧。"""
    try:
        result = merge_m4b(job_id, config=load_config(config), force=force)
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    if json_output:
        _json(result)
    else:
        typer.echo(
            f"Merge: {result['section_count']} sections; "
            f"{result['formula_count']} formulas; cache={'hit' if result['cache_hit'] else 'miss'}"
        )


def _run_m4c_audit(
    function: object,
    job_id: str,
    config: Path | None,
    json_output: bool,
) -> None:
    try:
        result = function(job_id, config=load_config(config))  # type: ignore[operator]
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    if json_output:
        _json(result)
    else:
        typer.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))


@audit_app.command("formulas")
def audit_formulas_command(
    job_id: Annotated[str, typer.Argument()],
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """核验全部公式的盲读记录、LaTeX、置信度与原图引用。"""
    _run_m4c_audit(audit_formulas, job_id, config, json_output)


@audit_app.command("corrections")
def audit_corrections_command(
    job_id: Annotated[str, typer.Argument()],
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """逐项核验 M4B 基线中的 32 条 accepted 纠错。"""
    _run_m4c_audit(audit_corrections, job_id, config, json_output)


@audit_app.command("paragraphs")
def audit_paragraphs_command(
    job_id: Annotated[str, typer.Argument()],
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """审计跨 Packet、公式密集和纠错密集的 15 个段落。"""
    _run_m4c_audit(audit_paragraphs, job_id, config, json_output)


@audit_app.command("packets")
def audit_packets_command(
    job_id: Annotated[str, typer.Argument()],
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """审计 4 个已知 Packet 边界的归属、重复与推导顺序。"""
    _run_m4c_audit(audit_packets, job_id, config, json_output)


@audit_app.command("dedup")
def audit_dedup_command(
    job_id: Annotated[str, typer.Argument()],
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """使用现有候选图片校验 16 组渐进画面和安全重复。"""
    _run_m4c_audit(audit_dedup, job_id, config, json_output)


@audit_app.command("apply-fixes")
def audit_apply_fixes_command(
    job_id: Annotated[str, typer.Argument()],
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """仅应用已写入结构化审计的证据支持修复并重建下游。"""
    _run_m4c_audit(apply_evidence_backed_fixes, job_id, config, json_output)


@audit_app.command("evidence")
def audit_evidence_command(
    job_id: Annotated[str, typer.Argument()],
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """校验证据、公式、段落和纠错引用的完整性。"""
    _run_m4c_audit(audit_evidence, job_id, config, json_output)


@audit_app.command("rendering")
def audit_rendering_command(
    job_id: Annotated[str, typer.Argument()],
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """校验由内置浏览器实际观察后写入的公式显示结果。"""
    _run_m4c_audit(audit_rendering, job_id, config, json_output)


@audit_app.command("report")
def audit_report_command(
    job_id: Annotated[str, typer.Argument()],
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """汇总结构化审计、人工队列、基线哈希和 M4C 判定。"""
    _run_m4c_audit(build_audit_report, job_id, config, json_output)


@corrections_app.command("build")
def corrections_build_command(
    job_id: Annotated[str, typer.Argument()],
    config: ConfigOption = None,
    force_stage: Annotated[str | None, typer.Option("--force-stage")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """校验纠错依据并生成 accepted/pending/rejected 三态结果。"""
    try:
        if force_stage not in {None, "corrections_ready"}:
            raise StateError("corrections build only supports --force-stage corrections_ready.")
        result = build_corrections(
            job_id, config=load_config(config), force=force_stage == "corrections_ready"
        )
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    if json_output:
        _json(result)
    else:
        typer.echo(
            f"Corrections: accepted={result['accepted']} pending={result['pending']} "
            f"rejected={result['rejected']}"
        )
        typer.echo(f"Cache: {'hit' if result['cache_hit'] else 'miss'}")


@app.command()
def render(
    job_id: Annotated[str, typer.Argument()],
    config: ConfigOption = None,
    force_stage: Annotated[str | None, typer.Option("--force-stage")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """渲染章节、重点、导图与本地三栏阅读器。"""
    try:
        if force_stage not in {None, "notes_ready", "web_ready"}:
            raise StateError("M4A render supports --force-stage notes_ready or web_ready.")
        settings = load_config(config)
        files, manifest, _ = load_job_context(job_id, config=settings)
        if (files.root / "merged/p6-complete/manifest.json").is_file():
            merged = json.loads(
                (files.root / "merged/p6-complete/manifest.json").read_text(encoding="utf-8")
            )
            result = {
                "section_count": merged["counts"]["sections"],
                "paragraph_count": merged["counts"]["paragraphs"],
                "cache_hit": True,
            }
        elif manifest.source.page == 6 and manifest.requested_range.end == 1500:
            result = merge_m4b(job_id, config=settings, force=force_stage == "notes_ready")
        else:
            result = render_m4a(job_id, config=settings, force=force_stage == "notes_ready")
        web_result = build_web(job_id, config=settings, force=force_stage == "web_ready")
        web_artifact = (
            Path("merged/p6-complete/web/web-manifest.json")
            if (files.root / "merged/p6-complete/manifest.json").is_file()
            else Path("web/web-manifest.json")
        )
        record_validated_artifact_stage(
            job_id,
            config=settings,
            stage="web_ready",
            artifact=web_artifact,
            tool="lectureflow-local-web-renderer",
        )
        payload = {"analysis": result, "web": web_result}
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    if json_output:
        _json(payload)
    else:
        typer.echo(f"Sections: {result['section_count']}; paragraphs: {result['paragraph_count']}")
        typer.echo(f"Web: {'cache hit' if web_result['cache_hit'] else 'ready'}")


@app.command("export-obsidian")
def export_obsidian(
    job_id: Annotated[str, typer.Argument()],
    vault: Annotated[Path, typer.Option("--vault", help="用户明确授权的输出目录。")],
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """安全导出 M4A 到显式目录；不复制视频或模型。"""
    try:
        settings = load_config(config)
        result = export_job_obsidian(job_id, config=settings, vault=vault)
        files, _, _ = load_job_context(job_id, config=settings)
        audit = {key: value for key, value in result.items() if key != "output"}
        audit_name = (
            "audits/m5/obsidian-export-manifest.json"
            if (files.root / "merged/p6-complete/manifest.json").is_file()
            else "obsidian/m4b-export-manifest.json"
            if (files.root / "analyses/m4b/m4b-summary.json").is_file()
            else "obsidian/m4a-export-manifest.json"
        )
        atomic_write_json(files.root / audit_name, audit)
        record_validated_artifact_stage(
            job_id,
            config=settings,
            stage="obsidian_ready",
            artifact=Path(audit_name),
            tool="lectureflow-obsidian-exporter",
        )
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    if json_output:
        _json(result)
    else:
        typer.echo(f"Obsidian: {result['output']}")
        typer.echo(f"Cache: {'hit' if result['cache_hit'] else 'miss'}")


@app.command()
def serve(
    job_id: Annotated[str, typer.Argument()],
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1024, max=65535)] = 8765,
    config: ConfigOption = None,
) -> None:
    """在 127.0.0.1 启动段落级三栏阅读器。"""
    try:
        server = create_server(job_id, config=load_config(config), host=host, port=port)
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    typer.echo(f"LectureFlow listening at http://{host}:{port} (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        typer.echo("LectureFlow server stopped.")
    finally:
        server.server_close()


@app.command()
def run(
    source: Annotated[str, typer.Argument()],
    start: Annotated[float, typer.Option("--start", min=0)] = 0.0,
    end: Annotated[float | None, typer.Option("--end", min=0)] = None,
    config: ConfigOption = None,
    until: Annotated[
        str,
        typer.Option("--until", help="支持 transcript_ready 或 frames_ready。"),
    ] = "transcript_ready",
    force_stage: Annotated[
        str | None,
        typer.Option("--force-stage", help="允许 subtitle_ready 或 frames_ready。"),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """一键运行到字幕或关键帧；不会自动下载 ASR 模型。"""
    try:
        if until not in {"transcript_ready", "frames_ready"}:
            raise StateError("run supports --until transcript_ready or frames_ready.")
        if force_stage not in {None, "subtitle_ready", "frames_ready"}:
            raise StateError("run --force-stage supports subtitle_ready or frames_ready.")
        settings = load_config(config)
        force = force_stage == "subtitle_ready"
        candidate = Path(source).expanduser()
        if (
            until == "frames_ready"
            and candidate.is_file()
            and candidate.suffix.lower() in SUPPORTED_SUBTITLE_SUFFIXES
        ):
            raise StateError("A subtitle-only input cannot produce frames; provide a video source.")
        if candidate.is_file() and candidate.suffix.lower() in SUPPORTED_SUBTITLE_SUFFIXES:
            result = import_local_subtitle(candidate, config=settings, force_stage=force)
        else:
            descriptor = identify_source(source)
            if descriptor.kind == "bilibili":
                subtitle_result = fetch_bilibili_subtitles(
                    source,
                    config=settings,
                    start=start,
                    end=end,
                    force_stage=force,
                )
                result = subtitle_result
                if until == "transcript_ready" and not any(
                    subtitle_result.selected_sources.values()
                ):
                    result = transcribe_job(subtitle_result.job_id, config=settings, offline=True)
            else:
                prepared = prepare_job(source, config=settings, start=start, end=end)
                if until == "transcript_ready":
                    result = resume_to_transcript(prepared.job_id, config=settings)
                else:
                    result = extract_job_frames(
                        prepared.job_id,
                        config=settings,
                        force=force_stage == "frames_ready",
                    )
        if until == "frames_ready" and not candidate.is_file():
            result = extract_job_frames(
                result.job_id,
                config=settings,
                force=force_stage == "frames_ready",
            )
    except LectureFlowError as exc:
        _emit_error(exc)
        return
    if until == "frames_ready":
        _emit_frame_result(result, json_output=json_output)
    elif isinstance(result, ASRProcessResult):
        _emit_asr_result(result, json_output=json_output)
    else:
        _emit_subtitle_result(result, json_output=json_output)
