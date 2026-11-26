"""src/config.py: tolerant parsing of the settings validated against a fixed set."""

import importlib.util
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "src" / "config.py"


def load_config(monkeypatch, **env):
    """A fresh copy of src/config.py parsed with ``env`` (src.config is untouched)."""
    for name, value in env.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    spec = importlib.util.spec_from_file_location("_config_under_test", CONFIG_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def warnings_about(module, name):
    return [w for w in module.CONFIG_WARNINGS if w.startswith(f"{name}=")]


# ---------------------------------------------------------------------------
# LOG_LEVEL
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["verbose", "trace", "10", "LOUD"])
def test_unknown_log_level_falls_back_to_info_with_a_warning(monkeypatch, value):
    config = load_config(monkeypatch, LOG_LEVEL=value)

    assert config.LOG_LEVEL == "INFO"
    [warning] = warnings_about(config, "LOG_LEVEL")
    assert value.upper() in warning
    assert "using INFO" in warning
    # Whatever the input, logging accepts the effective level.
    logging.getLogger("smr-test-config").setLevel(config.LOG_LEVEL)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "INFO"), ("", "INFO"), ("debug", "DEBUG"), (" Warning ", "WARNING")],
)
def test_valid_log_levels_are_kept(monkeypatch, value, expected):
    config = load_config(monkeypatch, LOG_LEVEL=value)

    assert config.LOG_LEVEL == expected
    assert warnings_about(config, "LOG_LEVEL") == []


def test_gui_starts_with_an_unknown_log_level(tmp_path):
    # Plain uvicorn: the root logger has no handlers, so the GUI lifespan calls
    # logging.basicConfig(level=config.LOG_LEVEL), which raises on unknown
    # names. The GUI must start and report the problem instead.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OAUTH_", "SESSION_", "GUI_"))
    }
    env.update(
        PYTHONPATH=str(ROOT),
        REQUESTS_DB=str(tmp_path / "requests.db"),
        LOG_LEVEL="verbose",
        SECRET_KEY="test-secret-key-0123456789abcdef0123456789abcdef",
        ADMIN_TOKEN="test-admin-token-0123456789abcdef",
    )
    code = (
        "from fastapi.testclient import TestClient\n"
        "from src import config, gui\n"
        "with TestClient(gui.app) as client:\n"
        "    print(client.get('/health').status_code, config.LOG_LEVEL)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["200", "INFO"]
    assert "Unknown level" not in result.stderr


# ---------------------------------------------------------------------------
# TRANSCODE_PRESET
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["very_fast", "fastest", "x264", "-crf"])
def test_unknown_transcode_preset_falls_back_to_veryfast(monkeypatch, value):
    config = load_config(monkeypatch, TRANSCODE_PRESET=value)

    assert config.TRANSCODE_PRESET == "veryfast"
    [warning] = warnings_about(config, "TRANSCODE_PRESET")
    assert value.lower() in warning
    assert "using veryfast" in warning


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "veryfast"),
        ("", "veryfast"),
        ("ultrafast", "ultrafast"),
        ("Medium", "medium"),
        (" veryslow ", "veryslow"),
        ("placebo", "placebo"),
    ],
)
def test_x264_presets_are_kept(monkeypatch, value, expected):
    config = load_config(monkeypatch, TRANSCODE_PRESET=value)

    assert config.TRANSCODE_PRESET == expected
    assert warnings_about(config, "TRANSCODE_PRESET") == []
