# Contributing

LectureFlow Local welcomes focused bug reports and design proposals. It is currently an alpha,
macOS/Apple Silicon-first project with strict local-processing and auditability rules. Because the
repository is presently proprietary, discuss code contributions with the maintainer before opening
a pull request; publishing a fork or patch does not change either party's copyright.

## Development setup

Install Python 3.11 or 3.12, uv, FFmpeg, and FFprobe. Then run:

```bash
uv sync --dev --locked
uv run lectureflow doctor
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=lectureflow --cov-fail-under=87.86
```

Tests must run offline. Media smoke fixtures must be generated locally by the repository's FFmpeg
fixture scripts rather than committed as binaries.

## Non-negotiable boundaries

- Do not add external model APIs, API-key requirements, cloud OCR, or cloud ASR.
- Never commit `workspaces/`, media, audio, frames, model weights, caches, cookies, credentials,
  logs, exported Vaults, user notes, or absolute personal paths.
- Preserve immutable raw transcripts and original frames. Derived content must retain evidence IDs.
- Use argv arrays with `shell=False`, bounded timeouts, and redacted errors for subprocesses.
- Keep the reader loopback-only and free of CDN or third-party telemetry.
- Record any source-code reuse, exact revision, license, changes, and rationale in
  `docs/REFERENCE_AUDIT.md` before introducing it.

When a code contribution has been agreed, keep the pull request small, update tests and relevant
documentation, and describe cache invalidation or migration effects. Any contribution terms must
be agreed separately in writing.
