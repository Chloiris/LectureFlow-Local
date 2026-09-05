from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from lectureflow.atomic import atomic_write_json
from lectureflow.hashing import hash_file
from lectureflow.subtitles.timecode import format_clock


def build_contact_sheets(
    root: Path,
    frames: list[dict[str, Any]],
    *,
    columns: int,
    sheet_width: int,
    max_frames: int,
) -> list[dict[str, Any]]:
    target = root / "frames/contact-sheets"
    target.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    for page, offset in enumerate(range(0, len(frames), max_frames), start=1):
        items = frames[offset : offset + max_frames]
        rows = math.ceil(len(items) / columns)
        cell_width = sheet_width // columns
        image_height = max(120, int(cell_width * 9 / 16))
        label_height = 34
        sheet = Image.new("RGB", (sheet_width, rows * (image_height + label_height)), "#16191f")
        draw = ImageDraw.Draw(sheet)
        font = ImageFont.load_default(size=18)
        for index, item in enumerate(items):
            source = root / item["path"]
            with Image.open(source) as opened:
                thumbnail = opened.convert("RGB")
                thumbnail.thumbnail((cell_width, image_height), Image.Resampling.LANCZOS)
            column, row = index % columns, index // columns
            x = column * cell_width + (cell_width - thumbnail.width) // 2
            y = row * (image_height + label_height) + (image_height - thumbnail.height) // 2
            sheet.paste(thumbnail, (x, y))
            label = f"{item['frame_id']}  {format_clock(float(item['timestamp']))}"
            draw.text(
                (column * cell_width + 8, y + image_height + 7), label, fill="white", font=font
            )
        path = target / f"contact-sheet-{page:03d}.jpg"
        sheet.save(path, "JPEG", quality=88, optimize=True)
        manifest.append(
            {
                "sheet_id": f"CS{page:03d}",
                "path": str(path.relative_to(root)),
                "frame_ids": [item["frame_id"] for item in items],
                "frame_count": len(items),
                "sha256": hash_file(path),
            }
        )
    atomic_write_json(
        root / "frames/contact-sheet-manifest.json",
        {"schema_version": "1.0.0", "sheets": manifest},
    )
    return manifest
