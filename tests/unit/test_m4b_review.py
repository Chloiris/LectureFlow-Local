from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

import lectureflow.m4b.merge as merge
import lectureflow.m4b.service as service
import lectureflow.obsidian.service as obsidian
import lectureflow.web.service as web
from lectureflow.cli import app
from lectureflow.errors import StateError
from lectureflow.hashing import hash_file
from lectureflow.schemas.m4b import FormulaRecord, TechnicalCorrection, TechnicalParagraph

from ..helpers_m4b import m4b_fixture


def _context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[object, object, object, object]:
    config, files, manifest, state = m4b_fixture(tmp_path, monkeypatch)

    def context(*args: object, **kwargs: object) -> tuple[object, object, object]:
        return files, manifest, state

    monkeypatch.setattr(merge, "load_job_context", context)
    monkeypatch.setattr(web, "load_job_context", context)
    monkeypatch.setattr(obsidian, "load_job_context", context)
    return config, files, manifest, state


def _jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_m4b_schema_enforces_formula_paragraph_and_correction_rules() -> None:
    with pytest.raises(ValidationError, match="cannot exceed 120 seconds"):
        TechnicalParagraph(
            paragraph_id="PAR0001",
            start=0,
            end=121,
            title="too long",
            text_raw="raw",
            text_clean="clean",
            text_corrected="corrected",
            source_segment_ids=["T000001"],
            packet_ids=["P0001"],
            confidence="high",
        )
    with pytest.raises(ValidationError, match="verified formula requires LaTeX"):
        FormulaRecord(
            formula_id="FORM0001",
            frame_id="F000001",
            timestamp=1,
            visual_transcription="可见但未转写",
            latex="",
            spoken_context="context",
            transcript_ids=["T000001"],
            paragraph_ids=["PAR0001"],
            variables=[],
            assumptions=[],
            role="unknown",
            confidence="high",
            uncertain_symbols=[],
            verification_status="verified",
        )
    uncertain = FormulaRecord(
        formula_id="FORM0001",
        frame_id="F000001",
        timestamp=1,
        visual_transcription="符号不清楚",
        latex="",
        spoken_context="context",
        transcript_ids=["T000001"],
        paragraph_ids=["PAR0001"],
        variables=[],
        assumptions=[],
        role="unknown",
        confidence="low",
        uncertain_symbols=["下标"],
        verification_status="needs_review",
    )
    assert uncertain.latex == ""
    with pytest.raises(ValidationError, match="requires high-confidence visual evidence"):
        TechnicalCorrection(
            correction_id="C000001",
            paragraph_id="PAR0001",
            source_segment_ids=["T000001"],
            start=0,
            end=2,
            original_text="soft max",
            suggested_text="Softmax",
            reason="context only",
            transcript_evidence=["T000001"],
            visual_evidence=[],
            confidence="high",
            decision="accepted",
            applied=True,
        )


def test_m4b_paragraph_packets_merge_and_cache_are_auditable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, files, _, _ = _context(tmp_path, monkeypatch)
    root = files.root
    raw_before = hash_file(root / "transcript/asr/original.jsonl")

    paragraph_cache = service.build_technical_paragraphs("m4b-fixture", config=config)
    packet_cache = service.build_technical_packets("m4b-fixture", config=config)
    packet_validation = service.validate_technical_packets(root)
    first = merge.merge_m4b("m4b-fixture", config=config)
    second = merge.merge_m4b("m4b-fixture", config=config)
    post_merge_packet_cache = service.build_technical_packets("m4b-fixture", config=config)

    paragraphs = _jsonl(root / "transcript/paragraphs/paragraphs.jsonl")
    assigned = [value for item in paragraphs for value in item["source_segment_ids"]]
    corrections = _jsonl(root / "transcript/corrections/correction-decisions.jsonl")
    formulas = json.loads((root / "analyses/m4b/formulas.json").read_text())
    boundary = json.loads((root / "analyses/m4b/packet-boundary-report.json").read_text())
    analysis_state = json.loads((root / "analyses/packets/analysis-state.json").read_text())

    assert paragraph_cache["cache_hit"] is True
    assert packet_cache["cache_hit"] is True
    assert packet_validation["packet_count"] == 5
    assert packet_validation["duplicate_primary_transcript_ids"] == []
    assert first["cache_hit"] is False and second["cache_hit"] is True
    assert post_merge_packet_cache["cache_hit"] is True
    assert len(paragraphs) == 24 and len(set(assigned)) == len(assigned) == 750
    assert max(item["end"] - item["start"] for item in paragraphs) <= 120
    assert len({item["end"] - item["start"] for item in paragraphs}) > 1
    assert hash_file(root / "transcript/asr/original.jsonl") == raw_before
    assert any(item["decision"] == "accepted" and item["applied"] for item in corrections)
    assert any(item["decision"] == "pending" and not item["applied"] for item in corrections)
    first_paragraph = paragraphs[0]
    assert "Softmax" in first_paragraph["text_corrected"]
    assert "Torrent" not in first_paragraph["text_corrected"]
    assert formulas[0]["frame_id"] and formulas[0]["latex"] == "y = Ax"
    assert boundary["duplicate_paragraph_count"] == 0
    assert boundary["unresolved_boundary_issue_count"] == 0
    assert all(item["started_at"] for item in analysis_state["packets"])


def test_m4b_validation_rejects_formula_with_unknown_paragraph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, files, _, _ = _context(tmp_path, monkeypatch)
    merge.merge_m4b("m4b-fixture", config=config)
    path = files.root / "analyses/m4b/formulas.json"
    formulas = json.loads(path.read_text())
    formulas[0]["paragraph_ids"] = ["PAR9999"]
    path.write_text(json.dumps(formulas), encoding="utf-8")
    with pytest.raises(StateError, match="FORM0001 references unknown paragraph"):
        merge.validate_m4b(files.root)


@pytest.mark.parametrize(
    ("field", "unknown_id", "message"),
    [
        ("transcript_ids", "T999999", "references unknown transcript ID"),
        ("frame_ids", "F999999", "references unknown frame ID"),
    ],
)
def test_m4b_packet_validation_rejects_unknown_evidence_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    unknown_id: str,
    message: str,
) -> None:
    config, files, _, _ = _context(tmp_path, monkeypatch)
    service.build_technical_packets("m4b-fixture", config=config)
    path = files.root / "packets/P0001/packet.json"
    packet = json.loads(path.read_text())
    packet[field].append(unknown_id)
    path.write_text(json.dumps(packet), encoding="utf-8")

    with pytest.raises(StateError, match=message):
        service.validate_technical_packets(files.root)


def test_m4b_web_serves_25_minute_data_search_formula_and_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, files, _, _ = _context(tmp_path, monkeypatch)
    merge.merge_m4b("m4b-fixture", config=config)
    first = web.build_web("m4b-fixture", config=config)
    second = web.build_web("m4b-fixture", config=config)
    data = json.loads((files.root / "web/data.json").read_text())
    script = (files.root / "web/app.js").read_text()

    assert first["cache_hit"] is False and second["cache_hit"] is True
    assert first["duration_seconds"] == 1500
    assert len(data["paragraphs"]) == 24 and len(data["formulas"]) == 1
    assert data["glossary"][0]["term"] == "Softmax"
    assert "state.data.formulas" in script and "state.data.glossary" in script
    assert "innerHTML" not in script and first["third_party_requests"] == []
    assert first["data_json_bytes"] < 2_048_000

    server = web.create_server("m4b-fixture", config=config, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        connection.request("GET", "/api/data")
        response = connection.getresponse()
        assert response.status == 200 and json.loads(response.read())["time_range"] == [0, 1500]
        connection.request("GET", "/media/video", headers={"Range": "bytes=10-29"})
        ranged = connection.getresponse()
        assert ranged.status == 206 and len(ranged.read()) == 20
        connection.request("GET", "/%2e%2e/manifest.json")
        traversal = connection.getresponse()
        assert traversal.status == 403
        traversal.read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_m4b_obsidian_exports_formula_glossary_relative_assets_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, files, _, _ = _context(tmp_path, monkeypatch)
    merge.merge_m4b("m4b-fixture", config=config)
    vault = tmp_path / "P6 临时 Vault"
    first = obsidian.export_obsidian("m4b-fixture", config=config, vault=vault)
    second = obsidian.export_obsidian("m4b-fixture", config=config, vault=vault)
    destination = vault / "LectureFlow P6 Demo"
    formulas = (destination / "04-公式与推导.md").read_text()
    corrections = (destination / "05-纠错记录.md").read_text()
    mindmap = (destination / "06-思维导图.md").read_text()

    assert first["cache_hit"] is False and second["cache_hit"] is True
    assert "?p=6&t=1225" in formulas and "y = Ax" in formulas
    assert "![[assets/frames/F000025.png" in formulas
    assert (destination / "assets/frames/F000025.png").is_file()
    assert "accepted" in corrections and "pending" in corrections
    assert "```mermaid\nmindmap" in mindmap
    assert first["raw_asr_sha256"] == hash_file(files.root / "transcript/asr/original.jsonl")
    assert first["canvas_generated"] is False


def test_ascii_correction_does_not_replace_substring_inside_longer_term() -> None:
    corrected, count = merge._replacement("RNN and RN are distinct", "RN", "recurrent")
    assert corrected == "RNN and recurrent are distinct"
    assert count == 1


def test_m4b_evidence_cli_records_the_technical_note_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, files, _, _ = _context(tmp_path, monkeypatch)
    merge.merge_m4b("m4b-fixture", config=config)
    monkeypatch.setattr(
        "lectureflow.cli.load_job_context", lambda *args, **kwargs: (files, None, None)
    )
    monkeypatch.setattr("lectureflow.cli.load_config", lambda *args, **kwargs: config)
    recorded: list[Path] = []
    monkeypatch.setattr(
        "lectureflow.cli.record_validated_artifact_stage",
        lambda *args, artifact, **kwargs: recorded.append(artifact),
    )

    result = CliRunner().invoke(app, ["evidence", "validate", "m4b-fixture", "--json"])

    assert result.exit_code == 0
    assert Path("notes/P6-00-25-integrated-note.md") in recorded


def test_m4b_packet_cli_records_validated_pipeline_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, files, _, _ = _context(tmp_path, monkeypatch)
    service.build_technical_packets("m4b-fixture", config=config)
    monkeypatch.setattr(
        "lectureflow.cli.load_job_context", lambda *args, **kwargs: (files, None, None)
    )
    monkeypatch.setattr("lectureflow.cli.load_config", lambda *args, **kwargs: config)
    recorded: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        "lectureflow.cli.record_validated_artifact_stage",
        lambda *args, stage, artifact, **kwargs: recorded.append((stage, artifact)),
    )

    packet_result = CliRunner().invoke(app, ["packets", "validate", "m4b-fixture"])
    analysis_result = CliRunner().invoke(app, ["analyze-packets", "m4b-fixture"])

    assert packet_result.exit_code == analysis_result.exit_code == 0
    assert ("packets_ready", Path("packets/m4b-packet-manifest.json")) in recorded
    assert ("analysis_ready", Path("analyses/packets/analysis-state.json")) in recorded
