from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import lectureflow.cli as cli
from lectureflow.cli import app

runner = CliRunner()


def test_m4a_build_render_export_and_serve_entrypoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = object()
    root = tmp_path / "job"
    (root / "analyses/m4a").mkdir(parents=True)
    (root / "analyses/m4a/paragraph-plan.json").write_text("{}")
    files = SimpleNamespace(root=root)
    monkeypatch.setattr(cli, "load_config", lambda value=None: settings)
    manifest = SimpleNamespace(
        source=SimpleNamespace(page=1), requested_range=SimpleNamespace(end=300.0)
    )
    monkeypatch.setattr(cli, "load_job_context", lambda *args, **kwargs: (files, manifest, None))
    monkeypatch.setattr(
        cli,
        "build_paragraphs",
        lambda *args, **kwargs: {"paragraph_count": 8, "cache_hit": True},
    )
    monkeypatch.setattr(
        cli,
        "build_corrections",
        lambda *args, **kwargs: {
            "accepted": 1,
            "pending": 2,
            "rejected": 0,
            "cache_hit": True,
        },
    )
    monkeypatch.setattr(
        cli,
        "render_m4a",
        lambda *args, **kwargs: {
            "section_count": 5,
            "paragraph_count": 8,
            "cache_hit": True,
        },
    )
    monkeypatch.setattr(cli, "build_web", lambda *args, **kwargs: {"cache_hit": True})
    monkeypatch.setattr(
        cli,
        "export_job_obsidian",
        lambda *args, **kwargs: {
            "output": str(tmp_path / "vault/LectureFlow Demo"),
            "cache_hit": True,
            "input_hash": "hash",
        },
    )
    monkeypatch.setattr(cli, "record_validated_artifact_stage", lambda *args, **kwargs: None)

    class Server:
        served = False
        closed = False

        def serve_forever(self) -> None:
            self.served = True

        def server_close(self) -> None:
            self.closed = True

    server = Server()
    monkeypatch.setattr(cli, "create_server", lambda *args, **kwargs: server)

    paragraphs = runner.invoke(app, ["paragraphs", "build", "job"])
    corrections = runner.invoke(app, ["corrections", "build", "job"])
    rendered = runner.invoke(app, ["render", "job"])
    exported = runner.invoke(
        app,
        ["export-obsidian", "job", "--vault", str(tmp_path / "vault")],
    )
    served = runner.invoke(app, ["serve", "job", "--host", "127.0.0.1", "--port", "8765"])

    assert paragraphs.exit_code == 0 and "Paragraphs: 8" in paragraphs.stdout
    assert corrections.exit_code == 0 and "accepted=1" in corrections.stdout
    assert rendered.exit_code == 0 and "Sections: 5" in rendered.stdout
    assert exported.exit_code == 0 and "Cache: hit" in exported.stdout
    assert served.exit_code == 0 and "127.0.0.1:8765" in served.stdout
    assert server.served and server.closed


@pytest.mark.parametrize(
    "arguments",
    [
        ["paragraphs", "build", "job", "--force-stage", "transcript_ready"],
        ["corrections", "build", "job", "--force-stage", "frames_ready"],
        ["render", "job", "--force-stage", "transcript_ready"],
    ],
)
def test_m4a_cli_rejects_cross_stage_force(arguments: list[str]) -> None:
    result = runner.invoke(app, arguments)
    assert result.exit_code == 2
    assert "Error:" in result.stderr


def test_resume_continues_m4a_without_asr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = object()
    root = tmp_path / "job"
    (root / "analyses/m4a").mkdir(parents=True)
    (root / "analyses/m4a/paragraph-plan.json").write_text("{}")
    files = SimpleNamespace(root=root)
    state = SimpleNamespace(stages={})
    monkeypatch.setattr(cli, "load_config", lambda value=None: settings)
    monkeypatch.setattr(cli, "load_job_context", lambda *args, **kwargs: (files, None, state))
    monkeypatch.setattr(
        cli,
        "render_m4a",
        lambda *args, **kwargs: {"cache_hit": True, "paragraph_count": 8},
    )
    monkeypatch.setattr(cli, "build_web", lambda *args, **kwargs: {"cache_hit": True})
    monkeypatch.setattr(
        cli,
        "transcribe_job",
        lambda *args, **kwargs: pytest.fail("resume must not rerun or inspect ASR"),
    )
    result = runner.invoke(app, ["resume", "job", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["m4a"]["cache_hit"] is True
    assert payload["web"]["cache_hit"] is True
