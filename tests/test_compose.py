"""docker-compose.yml agrees with the bot shutdown and the GUI proxy settings."""

import importlib.util
import re
from pathlib import Path

import pytest
import yaml

from src import bot, worker

ROOT = Path(__file__).resolve().parents[1]


def compose_services():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    return yaml.safe_load(text)["services"]


def seconds(duration) -> float:
    """A Compose duration ("75s", "1m15s", "1h") or a number of seconds."""
    text = str(duration).strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    parts = re.findall(r"(\d+(?:\.\d+)?)(h|ms|m|s|us)", text)
    assert "".join(n + u for n, u in parts) == text, f"bad duration {duration!r}"
    scale = {"h": 3600, "m": 60, "s": 1, "ms": 1e-3, "us": 1e-6}
    return sum(float(number) * scale[unit] for number, unit in parts)


def default_shutdown_timeout(monkeypatch) -> int:
    monkeypatch.delenv("WORKER_SHUTDOWN_TIMEOUT", raising=False)
    spec = importlib.util.spec_from_file_location(
        "_config_defaults", ROOT / "src" / "config.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.WORKER_SHUTDOWN_TIMEOUT


@pytest.mark.parametrize(
    ("duration", "expected"), [("75s", 75), ("1m15s", 75), ("90", 90), ("1h", 3600)]
)
def test_seconds_parses_compose_durations(duration, expected):
    assert seconds(duration) == expected


def test_bot_gets_enough_time_to_stop_before_sigkill(monkeypatch):
    # Docker's default is 10 s: shorter than the graceful shutdown, so uploads
    # still running would be killed (and then failed, never resent).
    service = compose_services()["bot"]
    environment = service.get("environment") or {}
    assert "WORKER_SHUTDOWN_TIMEOUT" not in environment, "update this test"
    budget = (
        bot.HANDLER_DRAIN_SECONDS
        + default_shutdown_timeout(monkeypatch)
        + worker.CANCEL_GRACE_SECONDS
        + worker.NOTICE_GRACE_SECONDS
    )

    assert "stop_grace_period" in service
    assert seconds(service["stop_grace_period"]) >= budget


def test_gui_can_trust_the_reverse_proxy():
    # uvicorn reads FORWARDED_ALLOW_IPS (default 127.0.0.1): it must reach the
    # container, and the command must not override it.
    service = compose_services()["gui"]

    assert "FORWARDED_ALLOW_IPS" in service["environment"]
    assert "--forwarded-allow-ips" not in str(service["command"])
