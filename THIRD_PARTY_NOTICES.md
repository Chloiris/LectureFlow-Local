# Third-Party Notices

LectureFlow Local is independently implemented. Its Python dependencies, optional local runtimes,
external command-line tools, and model checkpoints are governed by their respective upstream
licenses and terms; the project license does not replace those terms.

## Not distributed in this repository

- FFmpeg / FFprobe are user-installed system tools. FFmpeg licensing depends on the exact build
  configuration, including whether GPL components were enabled.
- MLX-Whisper, MLX, Faster-Whisper, VibeASR.cpp, yt-dlp, and their transitive dependencies are
  installed or configured separately. Consult the exact installed release for its license.
- Whisper, MLX, Hugging Face, and VibeASR model weights are not included. A converted checkpoint's
  code license does not automatically establish the license of its weights; users must verify the
  model card and upstream checkpoint terms before downloading or redistributing them.
- Course videos, subtitles, frames, transcripts, analyses, notes, and Obsidian exports are runtime
  content and are not part of the source repository.

## Audited integrations

The exact revisions and design-only references reviewed during development are documented in
[`docs/REFERENCE_AUDIT.md`](docs/REFERENCE_AUDIT.md). That audit currently records that no source
code was copied from the candidate course-note repositories, and that the optional Microsoft
VibeASR.cpp adapter invokes a separately installed upstream binary rather than vendoring it.
