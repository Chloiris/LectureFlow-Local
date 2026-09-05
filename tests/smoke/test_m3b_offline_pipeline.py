from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

import lectureflow.asr.service as asr_service
from lectureflow.asr.service import transcribe_job
from lectureflow.atomic import atomic_write_json, atomic_write_text
from lectureflow.config import load_config
from lectureflow.hashing import hash_file
from lectureflow.media.service import acquire_media
from lectureflow.packets.service import build_packet, validate_packet
from lectureflow.pipeline import prepare_job
from lectureflow.schemas.asr import ASRTranscriptResult
from lectureflow.schemas.transcript import TranscriptSegment

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg suite is required for the deterministic fixture",
)


def test_offline_m3b_transcript_cache_and_packet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "中文 空格/五分钟 fixture.mp4"
    source.parent.mkdir(parents=True)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x64:r=1:d=300",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000:duration=300",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-y",
            str(source),
        ],
        check=True,
    )
    monkeypatch.chdir(tmp_path)
    config = load_config(cwd=tmp_path)
    prepared = prepare_job(str(source), config=config, start=0, end=300)
    root = prepared.job_dir
    acquire_media(prepared.job_id, config=config)
    audio, audio_manifest, first_audio_hit = asr_service.ensure_asr_audio(
        prepared.job_id, config=config
    )
    cached_audio, cached_manifest, second_audio_hit = asr_service.ensure_asr_audio(
        prepared.job_id, config=config
    )
    assert first_audio_hit is False
    assert second_audio_hit is True
    assert cached_audio == audio
    assert cached_manifest["sha256"] == audio_manifest["sha256"] == hash_file(audio)
    model = tmp_path / "model"
    model.mkdir()

    monkeypatch.setattr(
        asr_service,
        "resolve_mlx_model",
        lambda config, offline: (model, True),
    )

    class FakeBackend:
        def __init__(self, **kwargs: object) -> None:
            pass

        def is_available(self) -> bool:
            return True

        def transcribe(self, request: object) -> ASRTranscriptResult:
            request.raw_output_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(
                request.raw_output_path,
                {"segments": [{"start": 0, "end": 300, "text": "本地转写夹具"}]},
            )
            return ASRTranscriptResult(
                backend="mlx-whisper",
                model=config.asr.mlx_model,
                model_revision=config.asr.mlx_revision,
                language="zh",
                duration_seconds=300,
                segments=[
                    TranscriptSegment(
                        segment_id="T000001",
                        start=0,
                        end=300,
                        text_raw="本地转写夹具",
                        text_clean="本地转写夹具",
                        source="mlx_whisper",
                        language="zh",
                        part_id="P1",
                    )
                ],
                raw_output_path=str(request.raw_output_path),
                elapsed_seconds=1,
                real_time_factor=1 / 300,
            )

    monkeypatch.setattr(asr_service, "MLXWhisperBackend", FakeBackend)
    first = transcribe_job(prepared.job_id, config=config, offline=True)
    second = transcribe_job(prepared.job_id, config=config, offline=True)
    with pytest.raises(asr_service.ASRUnavailableError, match="cannot overwrite raw"):
        transcribe_job(prepared.job_id, config=config, offline=True, force=True)
    with pytest.raises(
        asr_service.ASRUnavailableError, match=r"locked backend|immutable transcript"
    ):
        transcribe_job(
            prepared.job_id,
            config=config,
            backend="vibeasr-bitnet",
            offline=True,
        )
    assert first.transcript_cache_hit is False
    assert second.transcript_cache_hit is True
    assert (root / "transcript/asr/raw-mlx-whisper.json").is_file()

    frame_path = root / "frames/originals/F000001.png"
    frame_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), "blue").save(frame_path)
    frame = {
        "schema_version": "1.0.0",
        "frame_id": "F000001",
        "candidate_id": "C000001",
        "timestamp": 0,
        "path": "frames/originals/F000001.png",
        "reason": ["periodic"],
        "scene_score": None,
        "cue_terms": [],
        "nearby_segment_ids": ["T000001"],
        "width": 64,
        "height": 64,
        "sha256": hash_file(frame_path),
        "perceptual_hash": "0" * 16,
        "selected": True,
        "quality_flags": [],
    }
    atomic_write_text(root / "frames/frames.jsonl", json.dumps(frame) + "\n")
    sheet = root / "frames/contact-sheets/contact-sheet-001.jpg"
    Image.new("RGB", (64, 64), "blue").save(sheet)
    atomic_write_json(
        root / "frames/contact-sheet-manifest.json",
        {
            "sheets": [
                {
                    "sheet_id": "CS001",
                    "path": "frames/contact-sheets/contact-sheet-001.jpg",
                    "sha256": hash_file(sheet),
                }
            ]
        },
    )
    observation = {
        "frame_id": "F000001",
        "image_opened": True,
        "source": "current_codex_vision",
    }
    atomic_write_text(
        root / "analyses/vision-smoke/visual-observations.jsonl",
        json.dumps(observation) + "\n",
    )
    atomic_write_json(root / "media/media-manifest.json", {"fixture": True})
    atomic_write_json(root / "media/audio/asr-input-000-300.json", audio_manifest)
    packet = build_packet(prepared.job_id, config=config)
    assert packet["packet_id"] == "P0001"
    assert validate_packet(root, "P0001")["valid"] is True
