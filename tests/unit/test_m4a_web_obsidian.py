from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

import lectureflow.m4a.service as m4a
import lectureflow.obsidian.service as obsidian
import lectureflow.web.service as web
from lectureflow.errors import JobNotFoundError, StateError
from lectureflow.hashing import hash_file

from ..helpers_m4a import m4a_fixture


def _prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[object, object, object]:
    config, files, manifest, state = m4a_fixture(tmp_path)

    def context(*args: object, **kwargs: object) -> tuple[object, object, object]:
        return files, manifest, state

    monkeypatch.setattr(m4a, "load_job_context", context)
    monkeypatch.setattr(web, "load_job_context", context)
    monkeypatch.setattr(obsidian, "load_job_context", context)
    m4a.render_m4a("m4a-fixture", config=config)
    return config, files, manifest


def test_web_build_contains_three_modes_search_and_safe_text_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, files, _ = _prepare(tmp_path, monkeypatch)
    first = web.build_web("m4a-fixture", config=config)
    second = web.build_web("m4a-fixture", config=config)
    data = json.loads((files.root / "web/data.json").read_text())
    script = (files.root / "web/app.js").read_text()
    html = (files.root / "web/index.html").read_text()
    assert first["cache_hit"] is False and second["cache_hit"] is True
    assert {"text_raw", "text_clean", "text_corrected"} <= set(data["paragraphs"][0])
    assert any("大语言模型" in item["text_corrected"] for item in data["paragraphs"])
    assert all(not item["applied"] for item in data["corrections"] if item["decision"] == "pending")
    assert "textContent" in script and "innerHTML" not in script
    assert "<script>alert(1)</script>" not in html
    assert data["external_network_required"] is False
    assert first["third_party_requests"] == []


def test_web_jump_data_uses_paragraph_and_section_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, files, _ = _prepare(tmp_path, monkeypatch)
    web.build_web("m4a-fixture", config=config)
    data = json.loads((files.root / "web/data.json").read_text())
    paragraphs = {item["paragraph_id"]: item for item in data["paragraphs"]}
    for section in data["sections"]:
        assert section["start"] == paragraphs[section["paragraph_ids"][0]]["start"]
    targets = {item["section_id"] for item in data["sections"]} | set(paragraphs)
    nodes = list(data["mindmap"]["children"])
    while nodes:
        node = nodes.pop()
        assert node["target_id"] in targets
        nodes.extend(node["children"])


def test_loopback_server_home_range_frames_and_path_safety(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _, _ = _prepare(tmp_path, monkeypatch)
    server = web.create_server("m4a-fixture", config=config, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", "/")
        home = connection.getresponse()
        assert home.status == 200 and b"LectureFlow" in home.read()
        connection.request("GET", "/media/video", headers={"Range": "bytes=10-29"})
        ranged = connection.getresponse()
        assert ranged.status == 206
        assert ranged.getheader("Content-Range") == "bytes 10-29/2048"
        assert len(ranged.read()) == 20
        connection.request("GET", "/asset/frame/F000002")
        frame = connection.getresponse()
        assert frame.status == 200 and frame.getheader("Content-Type") == "image/png"
        frame.read()
        connection.request("GET", "/%2e%2e/manifest.json")
        traversal = connection.getresponse()
        assert traversal.status == 403
        traversal.read()
        connection.request("GET", "/missing")
        missing = connection.getresponse()
        assert missing.status == 404
        missing.read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_web_rejects_non_loopback_and_reports_port_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _, _ = _prepare(tmp_path, monkeypatch)
    with pytest.raises(StateError, match="only permits host"):
        web.create_server("m4a-fixture", config=config, host="0.0.0.0", port=8765)
    first = web.create_server("m4a-fixture", config=config, host="127.0.0.1", port=0)
    try:
        port = first.server_address[1]
        with pytest.raises(StateError, match="port may be in use"):
            web.create_server("m4a-fixture", config=config, host="127.0.0.1", port=port)
    finally:
        first.server_close()


def test_missing_job_is_a_friendly_error(tmp_path: Path) -> None:
    config = m4a_fixture(tmp_path)[0]
    with pytest.raises(JobNotFoundError, match="Job not found"):
        web.create_server("missing-job", config=config, host="127.0.0.1", port=0)


def test_obsidian_export_relative_links_frames_time_and_mermaid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, files, _ = _prepare(tmp_path, monkeypatch)
    vault = tmp_path / "临时 Vault"
    first = obsidian.export_obsidian("m4a-fixture", config=config, vault=vault)
    second = obsidian.export_obsidian("m4a-fixture", config=config, vault=vault)
    destination = vault / "LectureFlow Demo"
    timeline = (destination / "02-段落级时间轴.md").read_text()
    chapters = (destination / "01-章节笔记.md").read_text()
    corrected = (destination / "transcript/corrected.md").read_text()
    mindmap = (destination / "04-思维导图.md").read_text()
    assert first["cache_hit"] is False and second["cache_hit"] is True
    assert "?p=1&t=0" in timeline and "?p=1&t=60" in timeline
    assert "[[02-段落级时间轴#PAR0001]]" in chapters
    assert "![[assets/frames/F000002.png" in timeline
    assert (destination / "assets/frames/F000002.png").is_file()
    assert "[^C000001]" in corrected
    assert "```mermaid\nmindmap" in mindmap
    assert first["canvas_generated"] is False
    assert first["raw_asr_sha256"] == hash_file(files.root / "transcript/asr/original.jsonl")


def test_obsidian_refuses_modified_generated_and_unmanaged_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _, _ = _prepare(tmp_path, monkeypatch)
    vault = tmp_path / "vault"
    obsidian.export_obsidian("m4a-fixture", config=config, vault=vault)
    overview = vault / "LectureFlow Demo/00-五分钟总览.md"
    overview.write_text("user edit")
    with pytest.raises(StateError, match="modified"):
        obsidian.export_obsidian("m4a-fixture", config=config, vault=vault)

    unmanaged = tmp_path / "unmanaged/LectureFlow Demo"
    unmanaged.mkdir(parents=True)
    (unmanaged / "manual.md").write_text("manual")
    with pytest.raises(StateError, match="without a LectureFlow export manifest"):
        obsidian.export_obsidian("m4a-fixture", config=config, vault=tmp_path / "unmanaged")
