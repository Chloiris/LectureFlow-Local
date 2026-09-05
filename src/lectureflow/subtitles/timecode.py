from __future__ import annotations

import re

from lectureflow.errors import SubtitleParseError

_SRT = re.compile(r"^(?P<h>\d{1,3}):(?P<m>\d{2}):(?P<s>\d{2})[,.](?P<ms>\d{3})$")
_VTT = re.compile(r"^(?:(?P<h>\d{1,3}):)?(?P<m>\d{2}):(?P<s>\d{2})\.(?P<ms>\d{3})$")


def parse_timecode(value: str) -> float:
    candidate = value.strip()
    match = _SRT.fullmatch(candidate) or _VTT.fullmatch(candidate)
    if match is None:
        raise SubtitleParseError(f"Invalid subtitle timecode: {value!r}")
    hours = int(match.group("h") or 0)
    minutes = int(match.group("m"))
    seconds = int(match.group("s"))
    milliseconds = int(match.group("ms"))
    if minutes >= 60 or seconds >= 60:
        raise SubtitleParseError(f"Invalid subtitle timecode: {value!r}")
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000


def format_clock(seconds: float, *, separator: str = ".") -> str:
    if seconds < 0:
        raise ValueError("subtitle time cannot be negative")
    total_ms = round(seconds * 1000)
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}{separator}{milliseconds:03d}"


def format_srt_time(seconds: float) -> str:
    return format_clock(seconds, separator=",")


def format_vtt_time(seconds: float) -> str:
    return format_clock(seconds, separator=".")
