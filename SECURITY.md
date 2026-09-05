# Security Policy

## Supported version

LectureFlow Local is an alpha research prototype. Security fixes target the latest commit on
the default branch; older snapshots are not maintained separately.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository when available. Do not put
credentials, cookies, private media, transcripts, absolute user paths, or exploit details in a
public issue. If private reporting is unavailable, open a public issue containing only a request
for a private contact channel and no sensitive details.

Include the affected version, platform, minimal reproduction, expected impact, and whether the
issue could expose files outside a LectureFlow job directory. Please allow time for triage before
publishing details.

## Security boundaries

- No external model API is supported or permitted by project policy.
- The Web reader binds only to `127.0.0.1` and must not be exposed as a public service.
- Browser cookies and keychain data are not read. A user-supplied `cookies.txt`, if explicitly
  supported by a future workflow, must never be committed or printed.
- `workspaces/`, media, audio, frames, model files, caches, exports, and local configuration are
  runtime data and must stay outside Git.
- Obsidian export writes only to a user-specified directory and protects non-generated files.
