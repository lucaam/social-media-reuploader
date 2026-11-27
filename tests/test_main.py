"""Entrypoints: ``python -m src``, ``python -m src.bot`` and the src.main shim."""

import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from src import bot, main

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def bot_main(monkeypatch):
    calls = []

    def fake_main(mode=None):
        calls.append(mode)
        return 7

    monkeypatch.setattr(bot, "main", fake_main)
    return calls


def test_main_shim_runs_webhook_mode_through_the_bot(bot_main):
    assert main.main() == 7
    assert bot_main == ["webhook"]


@pytest.mark.parametrize(("module", "mode"), [("src.main", "webhook"), ("src", None)])
def test_modules_exit_with_the_bot_exit_code(monkeypatch, bot_main, module, mode):
    # Run the module code fresh, as ``python -m`` does (``src`` runs src.__main__).
    executed = "src.__main__" if module == "src" else module
    monkeypatch.delitem(sys.modules, executed, raising=False)
    with pytest.raises(SystemExit) as raised:
        runpy.run_module(module, run_name="__main__", alter_sys=False)
    assert raised.value.code == 7
    assert bot_main == [mode]


@pytest.mark.parametrize("module", ["src", "src.bot"])
def test_missing_bot_token_exits_with_code_2(tmp_path, module):
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("BOT_TOKEN", "MODE", "WEBHOOK_URL", "LOG_TO_FILE")
    }
    env.update(
        REQUESTS_DB=str(tmp_path / "requests.db"),
        PYTHONPATH=str(ROOT),
        LOG_LEVEL="INFO",
    )
    result = subprocess.run(
        [sys.executable, "-m", module],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert "BOT_TOKEN is not set" in result.stderr
    assert "Traceback" not in result.stderr
