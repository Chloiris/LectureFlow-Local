from __future__ import annotations

from pathlib import Path

import pytest

from lectureflow.config import load_config
from lectureflow.errors import ConfigurationError


def test_default_config_is_private_and_loopback(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path)
    assert config.workspace_root == tmp_path / "workspaces"
    assert config.web.host == "127.0.0.1"
    assert config.privacy.allow_external_model_api is False
    assert "这个公式" in config.frames.subtitle_cues


def test_config_relative_workspace_uses_config_directory(tmp_path: Path) -> None:
    config_path = tmp_path / "配置 文件.toml"
    config_path.write_text('workspace_root = "任务 空间"\n', encoding="utf-8")
    config = load_config(config_path)
    assert config.workspace_root == tmp_path / "任务 空间"


def test_missing_config_is_a_domain_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="does not exist"):
        load_config(tmp_path / "missing.toml")


@pytest.mark.parametrize(
    "body",
    [
        "[privacy]\nallow_external_model_api = true\n",
        "[privacy]\nallow_browser_cookie_access = true\n",
        '[web]\nhost = "0.0.0.0"\n',
    ],
)
def test_config_cannot_relax_security_boundaries(tmp_path: Path, body: str) -> None:
    config_path = tmp_path / "unsafe.toml"
    config_path.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_config(config_path)
