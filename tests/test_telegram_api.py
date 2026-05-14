import asyncio

import pytest

# Require aiohttp to be present for these integration-style tests; skip
# otherwise so local quick-runs don't fail during collection.
pytest.importorskip("aiohttp")

from src import http_client, telegram_api


class DummyResponse:
    def __init__(self, data, status=200):
        self._data = data
        self.status = status

    async def json(self):
        return self._data


class DummyCtx:
    def __init__(self, data, status=200):
        self._data = data
        self.status = status

    async def __aenter__(self):
        return DummyResponse(self._data, status=self.status)

    async def __aexit__(self, exc_type, exc, tb):
        return False


class MockSession:
    def __init__(self, data):
        self._data = data

    def post(self, *args, **kwargs):
        return DummyCtx(self._data)


async def fake_get_session():
    return MockSession({"ok": True, "result": {"message_id": 42}})


def test_send_message(monkeypatch):
    monkeypatch.setattr(http_client, "get_session", fake_get_session)
    res = asyncio.run(telegram_api.send_message("TOK", 123, "hello"))
    assert res["ok"] is True


def test_send_media_document(monkeypatch, tmp_path):
    f = tmp_path / "file.txt"
    f.write_bytes(b"hello")
    monkeypatch.setattr(http_client, "get_session", fake_get_session)
    res = asyncio.run(telegram_api.send_media("TOK", 123, str(f)))
    assert res["ok"] is True


def test_send_media_video(monkeypatch, tmp_path):
    f = tmp_path / "video.mp4"
    f.write_bytes(b"x" * 10)
    monkeypatch.setattr(http_client, "get_session", fake_get_session)
    res = asyncio.run(telegram_api.send_media("TOK", 123, str(f)))
    assert res["ok"] is True
