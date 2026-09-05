from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from PIL import Image
from typer.testing import CliRunner

import lectureflow.cli as cli
import lectureflow.m4b.merge as m4b_merge
import lectureflow.m5.service as m5
import lectureflow.obsidian.service as obsidian
import lectureflow.web.service as web
from lectureflow.atomic import atomic_write_json, atomic_write_text
from lectureflow.cli import app
from lectureflow.errors import StateError
from lectureflow.hashing import hash_file
from lectureflow.paths import JobFiles, ensure_job_layout

from ..helpers_m4b import m4b_fixture, write_jsonl


def _incremental_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[object, JobFiles, object, JobFiles, object]:
    config, frozen_files, frozen_manifest, state = m4b_fixture(tmp_path, monkeypatch)
    frozen_frames = [
        json.loads(line) for line in frozen_files.frames_jsonl.read_text().splitlines() if line
    ]
    for frame in frozen_frames:
        frame["candidate_id"] = frame["frame_id"].replace("F", "C", 1)
    write_jsonl(frozen_files.frames_jsonl, frozen_frames)

    def frozen_context(*args: object, **kwargs: object) -> tuple[object, object, object]:
        return frozen_files, frozen_manifest, state

    monkeypatch.setattr(m4b_merge, "load_job_context", frozen_context)
    m4b_merge.merge_m4b("frozen", config=config)
    monkeypatch.setattr(web, "load_job_context", frozen_context)
    web.build_web("frozen", config=config)

    frozen_root = frozen_files.root
    (frozen_root / "media/audio").mkdir(parents=True, exist_ok=True)
    (frozen_root / "media/audio/asr-input-000-1500.wav").write_bytes(b"frozen audio")
    atomic_write_json(frozen_root / "media/audio/asr-input-000-1500.json", {"sha256": "fixture"})
    atomic_write_json(frozen_root / "obsidian/m4b-export-manifest.json", {"fixture": True})
    atomic_write_json(frozen_root / "audits/m4c/m4c-summary.json", {"result": "passed"})
    atomic_write_json(
        frozen_root / "audits/m4c/dedup-audit.json",
        {"groups": [], "safe_duplicate": 0, "false_negative": 0},
    )
    write_jsonl(
        frozen_root / "audits/m4c/human-review-queue.jsonl",
        [
            {"review_id": "M4C-1", "type": "term", "uncertainty": "稠密模型"},
            {"review_id": "M4C-2", "type": "formula", "uncertainty": "卷积系数口述"},
        ],
    )
    metadata = {
        "title": "P6 大模型前沿架构 Part 2",
        "duration": 3300.33,
        "formats": [
            {
                "height": 1080,
                "vcodec": "h264",
                "tbr": 800,
                "filesize_approx": 330_000_000,
            },
            {
                "height": None,
                "vcodec": "none",
                "tbr": 96,
                "filesize_approx": 40_000_000,
            },
        ],
    }
    atomic_write_json(frozen_files.metadata, metadata)

    incremental_root = ensure_job_layout(config.workspace_root, "incremental")
    incremental_files = JobFiles(incremental_root)
    incremental_manifest = SimpleNamespace(
        source=SimpleNamespace(page=6, bvid="BV1pf421z757"),
        requested_range=SimpleNamespace(start=1500.0, end=3300.33),
    )
    incremental_media = incremental_root / "media/video/source.mp4"
    incremental_media.write_bytes(bytes(range(128)) * 20)
    atomic_write_json(
        incremental_files.media_manifest,
        {
            "schema_version": "1.0.0",
            "source_kind": "bilibili",
            "source_id": "BV1pf421z757",
            "part_id": "P6",
            "selected_part": 6,
            "part_selection_reason": "fixture",
            "source_url": "https://www.bilibili.com/video/BV1pf421z757?p=6",
            "requested_start": 1500.0,
            "requested_end": 3300.33,
            "media_path": "media/video/source.mp4",
            "storage": "job_local",
            "container": "mp4",
            "duration": 1800.33,
            "source_duration": 3300.33,
            "width": 1920,
            "height": 1080,
            "file_size": incremental_media.stat().st_size,
            "sha256": hash_file(incremental_media),
            "video_codec": "h264",
            "audio_included": True,
            "format_selector": None,
            "tool_versions": {"ffprobe": "fixture", "yt-dlp": "fixture"},
            "input_hash": "incremental",
            "config_hash": "config",
            "generated_at": "2026-08-13T00:00:00Z",
        },
    )

    segment_count = 926
    span = 1800.32 / segment_count
    special = {
        6: "FoodTension 模型。",
        9: "Sparse的成显。",
        25: "LinearTension。",
        307: "Great Search。",
        469: "Scaling Load。",
        483: "秘律。",
        808: "pass until。",
        355: "Language学习率。",
        592: "Scaling Lobe。",
    }
    segments = []
    for index in range(segment_count):
        start = round(index * span, 3)
        end = round((index + 1) * span, 3)
        text = special.get(index, "新增技术课程内容。")
        segments.append(
            {
                "schema_version": "1.0.0",
                "segment_id": f"T{index + 1:06d}",
                "start": start,
                "end": end,
                "text_raw": text,
                "text_clean": text,
                "source": "mlx_whisper",
                "language": "zh",
                "confidence": None,
                "speaker": None,
                "chapter_id": None,
                "part_id": "P6",
                "source_ref": {"raw_segment_index": index},
            }
        )
    write_jsonl(incremental_root / "transcript/asr/original.jsonl", segments)
    atomic_write_json(incremental_root / "transcript/asr/raw-mlx-whisper.json", {"segments": []})
    atomic_write_json(
        incremental_root / "transcript/asr/asr-manifest.json",
        {
            "model": "mlx-community/whisper-medium-mlx",
            "model_revision": "revision",
            "elapsed_seconds": 210.0,
            "real_time_factor": 0.12,
        },
    )
    atomic_write_json(
        incremental_root / "transcript/asr/technical-term-candidates.json",
        {"candidates": []},
    )

    active_times = {
        99: 1520.0,
        100: 1600.0,
        102: 1700.0,
        103: 1810.0,
        104: 1900.0,
        106: 2000.0,
        107: 2110.0,
        109: 2200.0,
        110: 2300.0,
        111: 2403.366667,
        113: 2489.9,
        114: 2580.0,
        115: 2762.166667,
        118: 2972.3,
        119: 3091.9,
        120: 3200.0,
        121: 3280.0,
    }
    duplicate_of = {101: 100, 105: 104, 108: 107, 112: 111, 116: 115, 117: 115}
    local_frames = []
    for global_number in range(99, 122):
        timestamp = active_times.get(global_number)
        if timestamp is None:
            timestamp = active_times[duplicate_of[global_number]] + (global_number % 2 + 1) / 10
        local_number = global_number - 98
        path = incremental_root / f"frames/originals/F{local_number:06d}.png"
        color_number = duplicate_of.get(global_number, global_number)
        Image.new("RGB", (96, 54), (color_number % 255, 70, 140)).save(path)
        local_frames.append(
            {
                "frame_id": f"F{local_number:06d}",
                "candidate_id": f"C{local_number:06d}",
                "timestamp": timestamp,
                "path": str(path.relative_to(incremental_root)),
                "sha256": hash_file(path),
                "reason": ["scene_change"],
                "scene_score": 0.5,
                "cue_terms": [],
                "perceptual_hash": f"{color_number:016x}",
            }
        )
    write_jsonl(incremental_files.frames_jsonl, local_frames)

    contexts = {
        "frozen": (frozen_files, frozen_manifest, state),
        "m4b-fixture": (frozen_files, frozen_manifest, state),
        "incremental": (incremental_files, incremental_manifest, state),
    }

    def context(job_id: str, *args: object, **kwargs: object) -> tuple[object, object, object]:
        return contexts[job_id]

    monkeypatch.setattr(m5, "load_job_context", context)
    monkeypatch.setattr(web, "load_job_context", context)
    monkeypatch.setattr(obsidian, "load_job_context", context)
    monkeypatch.setattr(m5, "_git_revision", lambda args: "a" * 40)
    return config, frozen_files, frozen_manifest, incremental_files, incremental_manifest


def test_m5_incremental_freeze_analyze_merge_web_and_obsidian(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, frozen, _, incremental, _ = _incremental_fixture(tmp_path, monkeypatch)

    plan = m5.plan_p6_completion("m4b-fixture", config=config)
    frozen_result = m5.freeze_p6_baseline("m4b-fixture", config=config)
    allocation = json.loads((frozen.root / "metadata/p6-id-allocation.json").read_text())
    allocation["incremental_starts"].update(
        {
            "transcript": 778,
            "paragraph": 25,
            "packet": 6,
            "frame": 99,
            "formula": 9,
            "derivation": 3,
            "correction": 45,
            "evidence": 25,
            "section": 9,
        }
    )
    atomic_write_json(frozen.root / "metadata/p6-id-allocation.json", allocation)

    transcript = m5.materialize_incremental_transcript(
        "incremental", frozen_job_id="m4b-fixture", config=config
    )
    transcript_cache = m5.materialize_incremental_transcript(
        "incremental", frozen_job_id="m4b-fixture", config=config
    )
    canonical = m5.canonicalize_incremental_frames(
        "incremental", frozen_job_id="m4b-fixture", config=config
    )
    paragraphs = m5.build_incremental_paragraphs(
        "incremental", frozen_job_id="m4b-fixture", config=config
    )
    packets = m5.build_incremental_packets(
        "incremental", frozen_job_id="m4b-fixture", config=config
    )
    stale_packet = incremental.root / "packets/incremental/P0012"
    stale_packet.mkdir(parents=True)
    (stale_packet / "partial-output.json").write_text("{}")
    packet_cache = m5.build_incremental_packets(
        "incremental", frozen_job_id="m4b-fixture", config=config
    )
    analysis = m5.analyze_incremental_packets(
        "incremental", frozen_job_id="m4b-fixture", config=config
    )
    merged = m5.merge_p6_complete("incremental", frozen_job_id="m4b-fixture", config=config)
    evidence_validation = m5.validate_complete_p6_evidence(incremental.root, frozen.root)
    web_first = web.build_web("incremental", config=config)
    web_second = web.build_web("incremental", config=config)
    obsidian_first = m5.export_complete_p6_obsidian(
        "incremental", config=config, vault=tmp_path / "vault"
    )
    obsidian_second = m5.export_complete_p6_obsidian(
        "incremental", config=config, vault=tmp_path / "vault"
    )

    assert plan["duration_seconds"] == 3300.33
    assert frozen_result["incremental_starts"]["transcript"] > 750
    assert transcript["segment_count"] == 926 and transcript_cache["cache_hit"] is True
    assert canonical["incremental_canonical_duplicate_count"] == 6
    assert paragraphs["paragraph_count"] > 0
    assert packets["packet_count"] == packet_cache["packet_count"] == 6
    assert packet_cache["cache_hit"] is True
    assert not stale_packet.exists()
    assert packet_cache["archived_orphan_packet_directories"] == [
        "packets/incremental-orphans/P0012"
    ]
    assert (incremental.root / "packets/incremental-orphans/P0012/partial-output.json").is_file()
    assert analysis["formulas"] == 7 and analysis["images_opened"] == 17
    assert merged["frozen_integrity"]["before_hash_equals_after_hash"] is True
    assert merged["counts"]["segments"] == 1676
    assert evidence_validation["valid"] is True
    assert evidence_validation["dangling_reference_count"] == 0
    merged_evidence = [
        json.loads(line)
        for line in (incremental.root / "merged/p6-complete/evidence/evidence-ledger.jsonl")
        .read_text()
        .splitlines()
    ]
    assert all(item["source_range"] in {"frozen", "incremental"} for item in merged_evidence)
    assert all(
        len(item["canonical_frame_ids"]) == len(item["frame_ids"]) for item in merged_evidence
    )
    assert web_first["media_segment_count"] == 2 and web_second["cache_hit"] is True
    assert obsidian_first["formula_count"] > 0 and obsidian_second["cache_hit"] is True
    assert obsidian_first["formula_count"] == merged["counts"]["formulas"]
    assert (tmp_path / "vault/LectureFlow P6 Complete/04-P6公式与推导.md").read_text().count(
        "$$"
    ) > 0

    server = web.create_server("incremental", config=config, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        connection.request("GET", "/api/data")
        response = connection.getresponse()
        data = json.loads(response.read())
        assert response.status == 200 and data["time_range"] == [0.0, 3300.33]
        connection.request("GET", "/media/segment/1", headers={"Range": "bytes=0-9"})
        ranged = connection.getresponse()
        assert ranged.status == 206 and len(ranged.read()) == 10
        connection.request("GET", "/../manifest.json")
        assert connection.getresponse().status == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_m5_rejects_invalid_ranges_hashes_and_unmanaged_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, frozen, frozen_manifest, incremental, incremental_manifest = _incremental_fixture(
        tmp_path, monkeypatch
    )
    incremental_manifest.requested_range.start = 1499.0
    with pytest.raises(StateError, match="registered as P6"):
        m5.materialize_incremental_transcript(
            "incremental", frozen_job_id="m4b-fixture", config=config
        )
    incremental_manifest.requested_range.start = 1500.0
    frozen_manifest.requested_range.end = 1499.0
    with pytest.raises(StateError, match="frozen baseline"):
        m5.plan_p6_completion("m4b-fixture", config=config)
    frozen_manifest.requested_range.end = 1500.0
    m5.freeze_p6_baseline("m4b-fixture", config=config)
    m5.materialize_incremental_transcript("incremental", frozen_job_id="m4b-fixture", config=config)
    values = [
        json.loads(line)
        for line in (incremental.root / "transcript/incremental/original.jsonl")
        .read_text()
        .splitlines()
    ]
    values[0]["segment_id"] = "T999999"
    write_jsonl(incremental.root / "transcript/incremental/original.jsonl", values)
    with pytest.raises(StateError, match="stable append"):
        m5.validate_incremental_transcript(incremental.root, frozen.root)

    unmanaged = tmp_path / "unmanaged/LectureFlow P6 Complete"
    unmanaged.mkdir(parents=True)
    atomic_write_text(unmanaged / "user.md", "user")
    atomic_write_json(incremental.root / "merged/p6-complete/manifest.json", {"fixture": True})
    with pytest.raises(StateError, match="unmanaged user files"):
        m5.export_complete_p6_obsidian("incremental", config=config, vault=tmp_path / "unmanaged")


def test_m5_cli_exposes_plan_freeze_absolute_transcript_canonicalization_and_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "load_config", lambda value=None: object())
    monkeypatch.setattr(
        cli,
        "plan_p6_completion",
        lambda *args, **kwargs: {
            "duration_seconds": 3300.33,
            "incremental_range": [1500, 3300.33],
            "estimated_incremental_download_bytes": 123,
        },
    )
    monkeypatch.setattr(
        cli,
        "freeze_p6_baseline",
        lambda *args, **kwargs: {"baseline": "frozen.json", "frozen_group_hash": "abc"},
    )
    monkeypatch.setattr(
        cli,
        "materialize_incremental_transcript",
        lambda *args, **kwargs: {"segment_count": 926, "cache_hit": True},
    )
    monkeypatch.setattr(
        cli,
        "canonicalize_incremental_frames",
        lambda *args, **kwargs: {"incremental_active_frame_count": 24},
    )
    monkeypatch.setattr(
        cli,
        "merge_p6_complete",
        lambda *args, **kwargs: {
            "merged_job_id": "p6-complete",
            "counts": {"sections": 14},
        },
    )
    runner = CliRunner()
    results = [
        runner.invoke(app, ["p6", "plan", "https://bilibili.com/video/BV1pf421z757?p=6"]),
        runner.invoke(app, ["freeze", "frozen", "--range", "0:1500"]),
        runner.invoke(app, ["p6", "materialize-transcript", "incremental"]),
        runner.invoke(app, ["frames", "canonicalize", "incremental"]),
        runner.invoke(
            app,
            ["p6", "merge", "--incremental-job", "incremental", "--frozen-job", "frozen"],
        ),
    ]
    assert all(result.exit_code == 0 for result in results)
    assert "3300.33" in results[0].stdout
    assert "926 segments" in results[2].stdout
    assert "Active frames: 24" in results[3].stdout
    assert "p6-complete" in results[4].stdout
    with pytest.raises(typer.Exit):
        cli.p6_plan_command("https://bilibili.com/video/BV1pf421z757?p=5")
    with pytest.raises(typer.Exit):
        cli.freeze_command("frozen", "1:1500")


def test_m5_packet_namespace_and_cli_reject_stale_active_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packet_root = tmp_path / "packets/incremental"
    assert m5._archive_unregistered_packet_directories(packet_root, set()) == []
    stale = packet_root / "P0012"
    stale.mkdir(parents=True)
    existing_archive = tmp_path / "packets/incremental-orphans/P0012"
    existing_archive.mkdir(parents=True)
    archived = m5._archive_unregistered_packet_directories(packet_root, set())
    assert archived == ["packets/incremental-orphans/P0012-1"]
    assert (tmp_path / archived[0]).is_dir()

    atomic_write_json(packet_root / "packet-manifest.json", {"packet_ids": []})
    (packet_root / "P9999").mkdir()
    with pytest.raises(StateError, match="Unregistered Packet directories"):
        m5.validate_incremental_packets(tmp_path, tmp_path)

    manifest = SimpleNamespace(
        source=SimpleNamespace(page=6),
        requested_range=SimpleNamespace(start=1500.0, end=3300.33),
    )
    files = SimpleNamespace(root=tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda value=None: object())
    monkeypatch.setattr(cli, "load_job_context", lambda *args, **kwargs: (files, manifest, None))
    monkeypatch.setattr(cli, "_is_m5_incremental", lambda value: True)
    monkeypatch.setattr(
        cli,
        "build_incremental_packets",
        lambda *args, **kwargs: {"packet_count": 6, "cache_hit": True},
    )
    monkeypatch.setattr(
        cli,
        "validate_incremental_packets",
        lambda *args, **kwargs: {"valid": True, "packet_count": 6},
    )
    monkeypatch.setattr(cli, "record_validated_artifact_stage", lambda *args, **kwargs: None)
    runner = CliRunner()
    build = runner.invoke(app, ["packets", "build", "incremental", "--json"])
    validate = runner.invoke(app, ["packets", "validate", "incremental", "--json"])
    invalid = runner.invoke(
        app,
        ["packets", "build", "incremental", "--force-stage", "transcript_ready"],
    )
    assert build.exit_code == 0 and '"packet_count": 6' in build.stdout
    assert validate.exit_code == 0 and '"valid": true' in validate.stdout
    assert invalid.exit_code == 2 and "only supports" in invalid.output
