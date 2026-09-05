from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from lectureflow.config import load_config
from lectureflow.frames.image import compare_images, hamming_distance, inspect_image
from lectureflow.frames.service import (
    _cue_matches,
    _progressive_protection,
    _stratified_final_selection,
)


def test_image_metrics_and_dhash_are_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    Image.new("RGB", (64, 32), "white").save(first)
    Image.new("RGB", (64, 32), "white").save(second)
    left = inspect_image(first)
    right = inspect_image(second)
    assert left["sha256"] == right["sha256"]
    assert hamming_distance(left["perceptual_hash"], right["perceptual_hash"]) == 0
    assert left["width"] == 64


def test_black_frame_metric(tmp_path: Path) -> None:
    path = tmp_path / "black.png"
    Image.new("RGB", (32, 32), "black").save(path)
    metrics = inspect_image(path)
    assert metrics["black_ratio"] == 1.0
    assert metrics["mean_luma"] == 0.0


def test_progressive_slide_change_is_measured_and_protected(tmp_path: Path) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    Image.new("RGB", (320, 180), "white").save(first)
    changed = Image.new("RGB", (320, 180), "white")
    ImageDraw.Draw(changed).text((40, 80), "x + y = z", fill="black")
    changed.save(second)
    metrics = compare_images(first, second)
    assert metrics["local_change_ratio"] > 0
    config = load_config(cwd=tmp_path).frames
    reasons = _progressive_protection(
        {"timestamp": 10, "cue_terms": ["这个公式"], "scene_score": 0.1},
        {"timestamp": 8},
        config=config,
        metrics=metrics,
    )
    assert "protected_subtitle_cue" in reasons
    assert {"local_pixel_change", "edge_change", "high_contrast_text_change"} & set(reasons)


def test_asr_jsonl_formula_cue_triggers_configured_offsets(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript/asr/original.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        '{"segment_id":"T000001","start":10,"end":12,'
        '"text_raw":"看这个公式","text_clean":"看这个公式"}\n'
    )
    config = load_config(cwd=tmp_path).frames.model_copy(
        update={"subtitle_cues": ["公式"], "cue_offsets_seconds": [-2, 0, 2, 5]}
    )
    matches, moments = _cue_matches(tmp_path, config, start=0, end=30)
    assert matches[0]["segment_id"] == "T000001"
    assert [item["timestamp"] for item in moments] == [8, 10, 12, 15]


def test_final_frame_budget_is_stratified_across_five_packets() -> None:
    frames = [
        {
            "candidate_id": f"C{index:06d}",
            "timestamp": float(index * 10),
            "reason": ["periodic"],
            "cue_terms": [],
            "cue_offsets": [],
            "scene_score": None,
            "sharpness": float(index),
        }
        for index in range(150)
    ]
    selected = _stratified_final_selection(frames, start=0, end=1500, maximum=100)
    assert len(selected) == 100
    counts = [
        sum(left <= item["timestamp"] < left + 300 for item in selected)
        for left in range(0, 1500, 300)
    ]
    assert counts == [20, 20, 20, 20, 20]
