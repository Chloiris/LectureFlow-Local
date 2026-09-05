from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _slide(path: Path, background: str, title: str, body: list[str]) -> None:
    image = Image.new("RGB", (640, 360), background)
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.load_default(size=34)
    body_font = ImageFont.load_default(size=24)
    draw.text((38, 35), title, fill="white" if background != "white" else "black", font=title_font)
    for index, line in enumerate(body):
        draw.text(
            (52, 115 + index * 48),
            line,
            fill="#f2f2f2" if background != "white" else "#111111",
            font=body_font,
        )
    image.save(path)


def generate(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lectureflow-m2-fixture-") as directory:
        root = Path(directory)
        definitions = [
            ("#7a1830", "LECTUREFLOW", ["Offline deterministic fixture"]),
            ("#164a70", "PPT: OBJECTIVE", ["Scene detection", "Periodic safety sample"]),
            ("white", "FORMULA", ["E = m * c^2", "source: slide"]),
            ("#17212b", "CODE", ["def square(x):", "    return x * x"]),
            ("black", "", []),
            ("white", "FORMULA", ["E = m * c^2", "source: slide"]),
            ("#31572c", "SLOW TOPIC", ["A long static slide", "must survive periodic sampling"]),
            ("#473269", "SUMMARY", ["Evidence before notes"]),
        ]
        paths: list[Path] = []
        for index, (background, title, body) in enumerate(definitions):
            path = root / f"slide-{index:02d}.png"
            _slide(path, background, title, body)
            paths.append(path)
        concat = root / "concat.txt"
        rows: list[str] = []
        for path in paths:
            rows.extend([f"file '{path}'", "duration 4"])
        rows.append(f"file '{paths[-1]}'")
        concat.write_text("\n".join(rows) + "\n", encoding="utf-8")
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat),
                "-vf",
                "fps=12,format=yuv420p",
                "-c:v",
                "libx264",
                "-movflags",
                "+faststart",
                "-y",
                str(output),
            ],
            check=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    generate(parser.parse_args().output)
