---
name: lectureflow-local
description: Operate the repository-local LectureFlow Local workflow to turn Bilibili or local course videos into auditable transcripts, key-frame analyses, evidence-led notes, Obsidian artifacts, and a local reader. Use when Codex is asked to process, resume, validate, review, or export a LectureFlow course job without external model APIs.
---

# LectureFlow Local

Operate the checked-out project; do not install unrelated skills or invoke an external model API.
Treat `AI_CONTEXT.md` and `AGENTS.md` as hard constraints and `PROJECT_STATUS.md` as the source of
implemented-stage truth.

## Establish safety and environment

1. Work through `uv run`; do not use a possibly outdated system `python3`.
2. Run `uv run lectureflow doctor` before creating or resuming a job.
3. Do not auto-install a global dependency, access browser/Keychain cookies, modify BiliNote, or write
   to an unspecified Obsidian Vault.
4. Stop for confirmation before downloading a model expected to exceed 2 GB, using Bilibili login
   state, exporting to a Vault, or running a real five-minute test without a supplied URL.
5. Never print a Cookie, token, identity header, or full credential-bearing command.

## Create or find a job

Run:

```bash
uv run lectureflow subtitles fetch "<Bilibili URL>" [--start SECONDS] [--end SECONDS]
uv run lectureflow subtitles import "<JSON/SRT/VTT/TXT path>" [--language zh-CN]
uv run lectureflow run "<source>" --until transcript_ready
```

Capture the returned job ID. Run the same command again only when verifying cache behavior; a
successful unchanged stage must report a cache hit. Inspect with:

```bash
uv run lectureflow status <job-id> --json
```

Do not delete a job after failure. Resume it with:

```bash
uv run lectureflow resume <job-id>
```

Use `--force-stage subtitle_ready` only for the smallest known-invalid stage. Input, configuration, model,
prompt, or Schema changes must invalidate that stage and its descendants, not unrelated upstream
work.

## Prepare deterministic material

M1 implements subtitles through `transcript_ready`:

```bash
uv run lectureflow subtitles inspect <job-id>
uv run lectureflow subtitles export <job-id>
```

Honor subtitle priority exactly: uploader human subtitles, Bilibili AI subtitles, explicit local
subtitle, MLX-Whisper, then Faster-Whisper. Stop after a reliable source; never repeat ASR merely to
fill a stage. Preserve raw subtitles and store any cleanup separately.

A single local fallback for a Bilibili source requires an explicit `?p=N`:

```bash
uv run lectureflow subtitles fetch "<Bilibili URL>?p=1" \
  --local-subtitle "<subtitle path>" --local-language zh-CN
```

Do not install an ASR extra or download a model during M1. The backend is only considered available
when its package and an explicit existing `model_path` are both present.

For an explicitly requested VibeASR comparison, use only the optional local backend after:

```bash
uv run lectureflow asr doctor --backend vibeasr-bitnet
uv run lectureflow asr preflight <job-id> --backend vibeasr-bitnet --seconds 30
```

It requires a user-configured official `asr_infer` plus the two pinned BitNet GGUF files and never
downloads or builds them. Do not switch an existing job from MLX to VibeASR: register a separate job
so raw transcripts remain immutable. Hotwords are recognition context, not independent evidence.

Before proceeding, verify each part contains immutable raw subtitle evidence, versioned original and
cleaned transcript JSONL, TXT/SRT/VTT, source/selection/quality reports, and a valid artifact manifest
as documented in `docs/DATA_SCHEMA.md`. Treat Markdown as a renderer output, not pipeline state.

`extract-frames` and later commands remain unimplemented at M1. Do not claim success or fabricate
their outputs; start them only after the corresponding milestone is implemented.

## Analyze Agent Packets

Process one Packet at a time; do not repeatedly load the complete course transcript.

1. Read `packet.json`, the Packet transcript, frame manifest, known terminology, overlap context,
   and output Schema.
2. View the contact sheet only to shortlist informative frames.
3. Mark selected `frame_id` values in the deterministic manifest as prescribed by the current
   command; avoid selecting duplicates, black screens, or decorative intros.
4. Open every selected original-resolution PNG/JPEG individually. Never infer formulas, code, or
   small slide text from contact-sheet thumbnails.
5. Write exactly one `analyses/<packet-id>.json` matching the supplied Schema.
6. Bind speech claims to transcript IDs and visual claims to frame IDs plus timestamps. Do not
   complete cropped equations or illegible code from general knowledge.
7. Record unclear terms, image ambiguity, and speech/slide conflicts explicitly. Preserve both sides
   of a conflict.

Do not use `codex exec` recursively. A future `codex-cli` provider stays disabled unless the user
explicitly enables it after current help/capability detection and Packet-count confirmation.

## Validate before rendering

Run:

```bash
uv run lectureflow validate <job-id>
```

Fix Schema failures and dangling segment/frame references in the smallest affected analysis file.
Do not render while any Packet fails validation. Review `coverage.json`: teaching content should
target at least 90% time coverage, while ads, silence, greetings, and intros may be explicitly
excluded. File count alone is not coverage.

Confirm `evidence-ledger.jsonl` exists before writing final notes. Each important claim needs at least
one valid transcript or visual reference. Formula uncertainty must retain the screenshot and say it
requires manual verification.

## Render and export

Run the implemented finalizers:

```bash
uv run lectureflow render <job-id>
uv run lectureflow export-obsidian <job-id> --vault "<explicit path>"
uv run lectureflow serve <job-id>
```

Before export, confirm the exact authorized destination and refuse to overwrite unrelated Vault
files. Verify Markdown links, timestamp links, Mermaid, Canvas JSON, critical screenshot assets, and
reader data. Confirm the server reports `127.0.0.1`; reject `0.0.0.0` and external CDN assets.

## Finish with evidence

Run `uv run ruff check .` and `uv run pytest`. Update `PROJECT_STATUS.md` with exact results and
remaining gaps. Do not claim the project complete until the real five-minute acceptance test proves
visual frames were opened, all required artifacts exist, resume/cache behavior works, the local page
opens, and no external model API was used.
