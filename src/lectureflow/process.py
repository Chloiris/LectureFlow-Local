from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from lectureflow.errors import CommandError
from lectureflow.security import redact_text


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


def run_command(
    argv: Sequence[str | Path],
    *,
    timeout: float = 60,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    max_output_chars: int = 2_000_000,
) -> CommandResult:
    """Run a command without a shell; return bounded, redacted text on failure."""
    normalized = tuple(str(item) for item in argv)
    if not normalized:
        raise CommandError("Cannot execute an empty command.")
    try:
        completed = subprocess.run(
            normalized,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise CommandError(f"Required executable not found: {normalized[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"Command timed out after {timeout:g}s: {normalized[0]}") from exc
    stdout = completed.stdout[-max_output_chars:]
    stderr = completed.stderr[-max_output_chars:]
    result = CommandResult(normalized, completed.returncode, stdout, stderr)
    if completed.returncode != 0:
        detail = redact_text(stderr.strip() or stdout.strip() or "no diagnostic output")
        raise CommandError(f"{normalized[0]} exited with {completed.returncode}: {detail}")
    return result


def first_version_line(executable: str, *arguments: str) -> str | None:
    try:
        result = run_command((executable, *arguments), timeout=10, max_output_chars=10_000)
    except CommandError:
        return None
    output = result.stdout.strip() or result.stderr.strip()
    return output.splitlines()[0] if output else None
