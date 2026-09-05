from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageFilter, ImageStat, UnidentifiedImageError

from lectureflow.errors import FrameError
from lectureflow.hashing import hash_file


def hamming_distance(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()


def compare_images(left: Path, right: Path) -> dict[str, float]:
    """Measure small deterministic slide changes without OCR or a CV model."""
    try:
        with Image.open(left) as left_opened, Image.open(right) as right_opened:
            left_gray = left_opened.convert("L")
            right_gray = right_opened.convert("L")
            if right_gray.size != left_gray.size:
                right_gray = right_gray.resize(left_gray.size, Image.Resampling.LANCZOS)
            difference = ImageChops.difference(left_gray, right_gray)
            pixels = list(difference.get_flattened_data())
            total = max(1, len(pixels))
            local_ratio = sum(value >= 16 for value in pixels) / total
            left_edges = left_gray.filter(ImageFilter.FIND_EDGES)
            right_edges = right_gray.filter(ImageFilter.FIND_EDGES)
            edge_pixels = list(ImageChops.difference(left_edges, right_edges).get_flattened_data())
            edge_ratio = sum(value >= 24 for value in edge_pixels) / total
            high_contrast_ratio = sum(value >= 64 for value in pixels) / total
    except (OSError, UnidentifiedImageError) as exc:
        raise FrameError(f"Cannot compare frame images: {exc}") from exc
    return {
        "local_change_ratio": local_ratio,
        "edge_change_ratio": edge_ratio,
        "high_contrast_change_ratio": high_contrast_ratio,
    }


def inspect_image(path: Path, *, black_pixel_luma: int = 16) -> dict[str, Any]:
    try:
        with Image.open(path) as opened:
            opened.verify()
        with Image.open(path) as opened:
            image = opened.convert("RGB")
            width, height = image.size
            gray = image.convert("L")
            histogram = gray.histogram()
            total = max(1, width * height)
            black_ratio = sum(histogram[: black_pixel_luma + 1]) / total
            stats = ImageStat.Stat(gray)
            mean = float(stats.mean[0])
            stddev = float(stats.stddev[0])
            edges = gray.filter(ImageFilter.FIND_EDGES)
            edge_stats = ImageStat.Stat(edges)
            sharpness = float(edge_stats.var[0])
            tiny = gray.resize((9, 8), Image.Resampling.LANCZOS)
            pixels = list(tiny.get_flattened_data())
            bits = 0
            for row in range(8):
                for column in range(8):
                    bits = (bits << 1) | int(
                        pixels[row * 9 + column] > pixels[row * 9 + column + 1]
                    )
    except (OSError, UnidentifiedImageError) as exc:
        raise FrameError(f"Invalid frame image {path}: {exc}") from exc
    return {
        "width": width,
        "height": height,
        "sha256": hash_file(path),
        "perceptual_hash": f"{bits:016x}",
        "sharpness": sharpness,
        "black_ratio": black_ratio,
        "mean_luma": mean,
        "luma_stddev": stddev,
    }
