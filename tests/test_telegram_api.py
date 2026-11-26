"""Tests for src.telegram_api, src.http_client and src.telegram_client.

The shared aiohttp session is replaced by a fake: no network, no Telegram.
"""

import asyncio
import json
import logging

import aiohttp
import pytest
from aiohttp.client_reqrep import ConnectionKey

from src import config, http_client, telegram_api, telegram_client

TOKEN = "123456789:AAH" + "x" * 32
EYES = "\U0001f440"
TROPHY = "\U0001f3c6"
BANANA = "\U0001f34c"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status=200, payload=None, body=None):
        self.status = status
        self._payload = payload
        self._body = body

    async def json(self, content_type="application/json"):
        if self._body is not None:
            return json.loads(self._body)  # raises JSONDecodeError for HTML
        return self._payload


class FakeRequest:
    def __init__(self, outcome):
        self._outcome = outcome

    async def __aenter__(self):
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome

    async def __aexit__(self, *exc_info):
        return False


class FakeSession:
    """Replays ``outcomes`` (responses or exceptions); the last one repeats."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes) or [ok()]
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        return FakeRequest(outcome)

    @property
    def methods(self):
        return [call["url"].rsplit("/", 1)[1] for call in self.calls]


def ok(result=None):
    return FakeResponse(200, {"ok": True, "result": result or {"message_id": 42}})


def error(code, description, **parameters):
    payload = {"ok": False, "error_code": code, "description": description}
    if parameters:
        payload["parameters"] = parameters
    return FakeResponse(code, payload)


def connector_error():
    key = ConnectionKey("api.telegram.org", 443, True, True, None, None, None)
    return aiohttp.ClientConnectorError(key, OSError(111, "Connection refused"))


def form_fields(form):
    """{name: value} of an aiohttp.FormData; files become (filename, type, handle)."""
    fields = {}
    for options, headers, value in form._fields:
        if "filename" in options:
            fields[options["name"]] = (
                options["filename"],
                headers.get("Content-Type"),
                value,
            )
        else:
            fields[options["name"]] = value
    return fields


@pytest.fixture
def session(monkeypatch):
    holder = {"session": FakeSession()}

    async def get_session():
        return holder["session"]

    monkeypatch.setattr(http_client, "get_session", get_session)

    def install(*outcomes):
        holder["session"] = FakeSession(*outcomes)
        return holder["session"]

    return install


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_API_TIMEOUT", 20.0, raising=False)
    monkeypatch.setattr(config, "TELEGRAM_UPLOAD_TIMEOUT", 200.0, raising=False)
    monkeypatch.setattr(config, "TELEGRAM_MAX_RETRIES", 3, raising=False)
    monkeypatch.setattr(config, "REACTION_DISABLE_SECONDS", 300, raising=False)
    monkeypatch.setattr(config, "TELEGRAM_API_BASE", None, raising=False)
    monkeypatch.setattr(telegram_api, "_reaction_disabled_until", {})


@pytest.fixture
def sleeps(monkeypatch):
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(telegram_api, "_sleep", fake_sleep)
    return delays


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------
def test_send_message_payload(session):
    fake = session(ok())
    result = run(telegram_api.send_message(TOKEN, 123, "ciao", reply_to_message_id=7))
    assert result == {"ok": True, "result": {"message_id": 42}}
    (call,) = fake.calls
    assert call["url"] == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert call["json"] == {
        "chat_id": 123,
        "text": "ciao",
        "reply_parameters": {"message_id": 7, "allow_sending_without_reply": True},
        "link_preview_options": {"is_disabled": True},
    }
    assert call["timeout"].total == 20.0
    assert "data" not in call


def test_send_message_options(session):
    fake = session(ok())
    run(
        telegram_api.send_message(
            TOKEN, 1, "<b>x</b>", parse_mode="HTML", disable_web_page_preview=False
        )
    )
    assert fake.calls[0]["json"] == {
        "chat_id": 1,
        "text": "<b>x</b>",
        "parse_mode": "HTML",
    }


def test_send_message_positional_reply_and_invalid_reply_id(session):
    fake = session(ok())
    run(telegram_api.send_message(TOKEN, 1, "x", 9))
    run(telegram_api.send_message(TOKEN, 1, "x", reply_to_message_id="nope"))
    assert fake.calls[0]["json"]["reply_parameters"]["message_id"] == 9
    assert "reply_parameters" not in fake.calls[1]["json"]


def test_send_message_truncates_to_telegram_limit(session):
    fake = session(ok())
    run(telegram_api.send_message(TOKEN, 1, "a" * 5000))
    text = fake.calls[0]["json"]["text"]
    assert len(text) == 4096
    assert text.endswith("\u2026")
    run(telegram_api.send_message(TOKEN, 1, "\U0001f600" * 3000))  # 2 units each
    text = fake.calls[1]["json"]["text"]
    assert len(text.encode("utf-16-le")) // 2 <= 4096


def test_edit_message_text_not_modified_counts_as_success(session):
    fake = session(
        error(400, "Bad Request: message is not modified: specified new text")
    )
    result = run(telegram_api.edit_message_text(TOKEN, 1, 5, "same"))
    assert result["ok"] is True
    assert fake.calls[0]["json"]["message_id"] == 5
    session(error(400, "Bad Request: message to edit not found"))
    assert run(telegram_api.edit_message_text(TOKEN, 1, 5, "x"))["ok"] is False


def test_delete_message(session):
    fake = session(ok(True))
    assert run(telegram_api.delete_message(TOKEN, 1, 5))["ok"] is True
    assert fake.methods == ["deleteMessage"]
    assert fake.calls[0]["json"] == {"chat_id": 1, "message_id": 5}


# ---------------------------------------------------------------------------
# Response handling, retries, timeouts
# ---------------------------------------------------------------------------
def test_api_errors_are_returned_not_raised(session, sleeps):
    fake = session(error(400, "Bad Request: chat not found"))
    result = run(telegram_api.send_message(TOKEN, 1, "x"))
    assert result == {
        "ok": False,
        "error_code": 400,
        "description": "Bad Request: chat not found",
    }
    assert len(fake.calls) == 1
    assert sleeps == []


def test_non_json_response(session, sleeps):
    fake = session(FakeResponse(502, body="<html>Bad Gateway</html>"))
    result = run(telegram_api.send_message(TOKEN, 1, "x"))
    assert result["ok"] is False
    assert result["error_code"] == 502
    assert result["description"].startswith("HTTP 502")
    assert len(fake.calls) == 1  # sendMessage is not idempotent: no blind retry
    assert sleeps == []


def test_unexpected_json_shape(session):
    session(FakeResponse(200, ["not", "an", "object"]))
    result = run(telegram_api.get_me(TOKEN))
    assert result["ok"] is False
    assert result["error_code"] == 200


def test_429_waits_retry_after(session, sleeps):
    fake = session(
        error(429, "Too Many Requests: retry after 3", retry_after=3),
        ok(),
    )
    result = run(telegram_api.send_message(TOKEN, 1, "x"))
    assert result["ok"] is True
    assert sleeps == [3.0]
    assert len(fake.calls) == 2


def test_429_with_long_wait_is_returned(session, sleeps):
    fake = session(error(429, "Too Many Requests: retry after 3600", retry_after=3600))
    result = run(telegram_api.send_video(TOKEN, 1, "https://cdn.example.com/v.mp4"))
    assert result["error_code"] == 429
    assert result["parameters"]["retry_after"] == 3600
    assert sleeps == []
    assert len(fake.calls) == 1


def test_retries_are_bounded(session, sleeps, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_MAX_RETRIES", 2)
    fake = session(error(429, "Too Many Requests: retry after 1", retry_after=1))
    result = run(telegram_api.send_message(TOKEN, 1, "x"))
    assert result["error_code"] == 429
    assert len(fake.calls) == 3
    assert sleeps == [1.0, 1.0]


def test_zero_retries(session, sleeps, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_MAX_RETRIES", 0)
    fake = session(error(429, "Too Many Requests", retry_after=1), ok())
    assert run(telegram_api.send_message(TOKEN, 1, "x"))["ok"] is False
    assert len(fake.calls) == 1


def test_429_without_retry_after_uses_backoff(session, sleeps):
    fake = session(error(429, "Too Many Requests"), error(429, "Too Many"), ok())
    assert run(telegram_api.send_message(TOKEN, 1, "x"))["ok"] is True
    assert sleeps == [1.0, 2.0]
    assert len(fake.calls) == 3


def test_5xx_retried_only_for_idempotent_methods(session, sleeps):
    fake = session(error(502, "Bad Gateway"), ok(True))
    assert run(telegram_api.delete_message(TOKEN, 1, 2))["ok"] is True
    assert len(fake.calls) == 2
    fake = session(error(502, "Bad Gateway"), ok())
    assert run(telegram_api.send_message(TOKEN, 1, "x"))["ok"] is False
    assert len(fake.calls) == 1


def test_connection_errors_are_retried_for_every_method(session, sleeps):
    fake = session(connector_error(), aiohttp.ConnectionTimeoutError(), ok())
    assert run(telegram_api.send_message(TOKEN, 1, "x"))["ok"] is True
    assert len(fake.calls) == 3
    assert sleeps == [1.0, 2.0]


def test_ambiguous_failures_not_retried_for_sends(session, sleeps, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"\x00" * 10)
    fake = session(TimeoutError(), ok())
    result = run(telegram_api.send_video(TOKEN, 1, str(video)))
    assert result["ok"] is False
    assert "error_code" not in result
    assert result["description"].startswith("network error: TimeoutError")
    assert len(fake.calls) == 1
    fake = session(aiohttp.ServerDisconnectedError(), ok())
    assert run(telegram_api.send_message(TOKEN, 1, "x"))["ok"] is False
    assert len(fake.calls) == 1


def test_ambiguous_failures_retried_for_idempotent_methods(session, sleeps):
    fake = session(TimeoutError(), aiohttp.ServerDisconnectedError(), ok(True))
    assert run(telegram_api.edit_message_text(TOKEN, 1, 2, "x"))["ok"] is True
    assert len(fake.calls) == 3


def test_permanent_client_errors_are_not_retried(session, sleeps):
    fake = session(aiohttp.InvalidURL("bad"), ok())
    assert run(telegram_api.delete_message(TOKEN, 1, 2))["ok"] is False
    assert len(fake.calls) == 1


def test_unserializable_parameters_do_not_raise(session, sleeps):
    fake = session(TypeError("Object of type object is not JSON serializable"), ok())
    result = run(telegram_api.send_message(TOKEN, object(), "x"))
    assert result["ok"] is False
    assert result["description"].startswith("invalid parameters")
    assert len(fake.calls) == 1


def test_odd_error_codes_are_handled(session, sleeps):
    session(FakeResponse(200, {"ok": False, "error_code": "weird", "description": "?"}))
    result = run(telegram_api.get_me(TOKEN))
    assert result == {"ok": False, "error_code": "weird", "description": "?"}
    session(FakeResponse(400, {"ok": False}))
    result = run(telegram_api.get_me(TOKEN))
    assert result == {"ok": False, "error_code": 400, "description": "HTTP 400"}


def test_network_errors_never_expose_the_token(session, sleeps, caplog):
    leaky = aiohttp.ClientConnectionError(
        f"Cannot connect https://api.telegram.org/bot{TOKEN}/sendMessage"
    )
    session(leaky)
    with caplog.at_level(logging.DEBUG):
        result = run(telegram_api.send_message(TOKEN, 1, "x"))
    assert result["ok"] is False
    assert TOKEN not in result["description"]
    assert "<redacted>" in result["description"]
    assert TOKEN not in caplog.text


def test_custom_api_base_for_local_bot_api_server(session, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_API_BASE", "http://bot-api:8081/")
    fake = session(ok())
    run(telegram_api.get_me(TOKEN))
    assert fake.calls[0]["url"] == f"http://bot-api:8081/bot{TOKEN}/getMe"


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
def test_redact():
    text = f"GET https://api.telegram.org/bot{TOKEN}/getMe failed"
    assert telegram_api.redact(text) == (
        "GET https://api.telegram.org/bot<redacted>/getMe failed"
    )
    assert telegram_api.redact("token TOK here", token="TOK") == "token <redacted> here"
    assert telegram_api.redact(None) == ""
    assert telegram_api.redact("12:30 nothing secret") == "12:30 nothing secret"


def test_redact_filter_cleans_messages_and_tracebacks():
    record = logging.LogRecord(
        "aiohttp.client",
        logging.ERROR,
        __file__,
        1,
        "url=%s",
        (f"/bot{TOKEN}/x",),
        None,
    )
    try:
        raise RuntimeError(f"boom /bot{TOKEN}/x")
    except RuntimeError:
        import sys

        record.exc_info = sys.exc_info()
    assert telegram_api.RedactTokenFilter().filter(record) is True
    assert TOKEN not in record.getMessage()
    assert TOKEN not in record.exc_text
    formatted = logging.Formatter().format(record)
    assert TOKEN not in formatted


def test_install_log_redaction_is_idempotent():
    logger = logging.getLogger("test_install_log_redaction")
    handler = logging.StreamHandler()
    logger.addHandler(handler)
    try:
        telegram_api.install_log_redaction(logger)
        telegram_api.install_log_redaction(logger)
        assert (
            sum(isinstance(f, telegram_api.RedactTokenFilter) for f in handler.filters)
            == 1
        )
    finally:
        logger.removeHandler(handler)


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------
@pytest.fixture
def media(tmp_path):
    paths = {}
    for name in (
        "video.mp4",
        "thumb.jpg",
        "song.mp3",
        "photo.png",
        "file.txt",
        "v2.mp4",
    ):
        path = tmp_path / name
        path.write_bytes(b"\x00" * 16)
        paths[name] = str(path)
    return paths


def test_send_video_upload_multipart(session, media):
    fake = session(ok())
    result = run(
        telegram_api.send_video(
            TOKEN,
            -100,
            media["video.mp4"],
            caption="titolo",
            reply_to_message_id=5,
            duration=12.6,
            width=720,
            height=1280,
            thumbnail=media["thumb.jpg"],
        )
    )
    assert result["ok"] is True
    (call,) = fake.calls
    assert call["url"].endswith("/sendVideo")
    assert call["timeout"].total == 200.0
    fields = form_fields(call["data"])
    assert fields["chat_id"] == "-100"
    assert fields["caption"] == "titolo"
    assert json.loads(fields["reply_parameters"]) == {
        "message_id": 5,
        "allow_sending_without_reply": True,
    }
    assert (fields["duration"], fields["width"], fields["height"]) == (
        "13",
        "720",
        "1280",
    )
    assert fields["supports_streaming"] == "true"
    assert fields["thumbnail"] == "attach://thumbnail_file"
    filename, content_type, handle = fields["video"]
    assert (filename, content_type) == ("video.mp4", "video/mp4")
    assert handle.closed  # files are closed after the request
    assert fields["thumbnail_file"][:2] == ("thumb.jpg", "image/jpeg")


def test_send_video_by_url_uses_json(session):
    fake = session(ok())
    run(
        telegram_api.send_video(
            TOKEN, 1, "https://cdn.example.com/v.mp4", width=640, height=360
        )
    )
    payload = fake.calls[0]["json"]
    assert payload == {
        "chat_id": 1,
        "video": "https://cdn.example.com/v.mp4",
        "width": 640,
        "height": 360,
        "supports_streaming": True,
    }
    assert fake.calls[0]["timeout"].total == 200.0


def test_send_video_url_with_local_thumbnail_is_multipart(session, media):
    fake = session(ok())
    run(
        telegram_api.send_video(
            TOKEN, 1, "https://cdn.example.com/v.mp4", thumbnail=media["thumb.jpg"]
        )
    )
    fields = form_fields(fake.calls[0]["data"])
    assert fields["video"] == "https://cdn.example.com/v.mp4"
    assert fields["thumbnail"] == "attach://thumbnail_file"


def test_send_video_missing_file_makes_no_request(session):
    fake = session(ok())
    result = run(telegram_api.send_video(TOKEN, 1, "/nonexistent/video.mp4"))
    assert result == {"ok": False, "description": "file not found: video.mp4"}
    assert fake.calls == []


def test_send_audio(session, media):
    fake = session(ok())
    run(
        telegram_api.send_audio(
            TOKEN,
            1,
            media["song.mp3"],
            title="Titolo",
            performer="Artista",
            duration=200,
            reply_to_message_id=4,
        )
    )
    call = fake.calls[0]
    assert call["url"].endswith("/sendAudio")
    fields = form_fields(call["data"])
    assert fields["title"] == "Titolo"
    assert fields["performer"] == "Artista"
    assert fields["duration"] == "200"
    assert fields["audio"][:2] == ("song.mp3", "audio/mpeg")


def test_send_photo_and_document(session, media):
    fake = session(ok())
    run(telegram_api.send_photo(TOKEN, 1, media["photo.png"], caption="c"))
    run(telegram_api.send_document(TOKEN, 1, media["file.txt"], thumbnail=None))
    run(telegram_api.send_photo(TOKEN, 1, "https://cdn.example.com/p.jpg"))
    assert fake.methods == ["sendPhoto", "sendDocument", "sendPhoto"]
    assert form_fields(fake.calls[0]["data"])["photo"][:2] == ("photo.png", "image/png")
    assert form_fields(fake.calls[1]["data"])["document"][0] == "file.txt"
    assert fake.calls[2]["json"]["photo"] == "https://cdn.example.com/p.jpg"


def test_caption_is_truncated(session, media):
    fake = session(ok())
    run(telegram_api.send_photo(TOKEN, 1, media["photo.png"], caption="x" * 2000))
    assert len(form_fields(fake.calls[0]["data"])["caption"]) == 1024


@pytest.mark.parametrize(
    ("name", "method"),
    [
        ("video.mp4", "sendVideo"),
        ("song.mp3", "sendAudio"),
        ("photo.png", "sendPhoto"),
        ("file.txt", "sendDocument"),
    ],
)
def test_send_media_dispatch(session, media, name, method):
    fake = session(ok())
    assert run(telegram_api.send_media(TOKEN, 1, media[name]))["ok"] is True
    assert fake.methods == [method]


def test_send_media_group_local_files(session, media):
    fake = session(ok([{"message_id": 1}, {"message_id": 2}, {"message_id": 3}]))
    items = [
        {
            "type": "video",
            "path": media["video.mp4"],
            "caption": "uno",
            "duration": 5,
            "width": 640,
            "height": 360,
            "thumbnail": media["thumb.jpg"],
        },
        {"type": "video", "path": media["v2.mp4"]},
        {"type": "photo", "url": "https://cdn.example.com/p.jpg"},
    ]
    result = run(telegram_api.send_media_group(TOKEN, 1, items, reply_to_message_id=8))
    assert result["ok"] is True
    call = fake.calls[0]
    assert call["url"].endswith("/sendMediaGroup")
    fields = form_fields(call["data"])
    assert json.loads(fields["media"]) == [
        {
            "type": "video",
            "media": "attach://file0",
            "caption": "uno",
            "duration": 5,
            "width": 640,
            "height": 360,
            "supports_streaming": True,
            "thumbnail": "attach://thumb0",
        },
        {"type": "video", "media": "attach://file1", "supports_streaming": True},
        {"type": "photo", "media": "https://cdn.example.com/p.jpg"},
    ]
    assert {"file0", "file1", "thumb0"} <= set(fields)
    assert json.loads(fields["reply_parameters"])["message_id"] == 8


def test_send_media_group_of_urls_is_json(session):
    fake = session(ok([]))
    items = [
        {"type": "photo", "url": "https://cdn.example.com/1.jpg"},
        {"type": "photo", "url": "https://cdn.example.com/2.jpg", "caption": "due"},
    ]
    run(telegram_api.send_media_group(TOKEN, 1, items))
    assert fake.calls[0]["json"]["media"] == [
        {"type": "photo", "media": "https://cdn.example.com/1.jpg"},
        {"type": "photo", "media": "https://cdn.example.com/2.jpg", "caption": "due"},
    ]


@pytest.mark.parametrize(
    "items",
    [
        [],
        [{"type": "photo", "url": "https://cdn.example.com/1.jpg"}] * 11,
        [
            {"type": "audio", "url": "https://cdn.example.com/a.mp3"},
            {"type": "video", "url": "https://cdn.example.com/v.mp4"},
        ],
        [
            {"type": "document", "url": "https://cdn.example.com/a.pdf"},
            {"type": "photo", "url": "https://cdn.example.com/p.jpg"},
        ],
        [{"type": "sticker", "url": "https://cdn.example.com/s.webp"}] * 2,
        [
            {"type": "video", "path": "/nonexistent/a.mp4"},
            {"type": "video", "url": "https://cdn.example.com/v.mp4"},
        ],
        [{"type": "video"}, {"type": "video", "url": "https://cdn.example.com/v.mp4"}],
    ],
)
def test_send_media_group_validation(session, items):
    fake = session(ok())
    result = run(telegram_api.send_media_group(TOKEN, 1, items))
    assert result["ok"] is False
    assert result["description"]
    assert fake.calls == []


def test_send_media_group_single_item_uses_the_matching_method(session, media):
    fake = session(ok({"message_id": 9}))
    result = run(
        telegram_api.send_media_group(
            TOKEN, 1, [{"type": "video", "path": media["video.mp4"], "width": 10}]
        )
    )
    assert fake.methods == ["sendVideo"]
    assert result == {"ok": True, "result": [{"message_id": 9}]}


def test_unreadable_file_is_reported(session, media, monkeypatch):
    def broken_open(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("builtins.open", broken_open)
    fake = session(ok())
    result = run(telegram_api.send_document(TOKEN, 1, media["file.txt"]))
    assert result == {"ok": False, "description": "cannot read file: Permission denied"}
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------
def test_reaction_payload(session):
    fake = session(ok(True))
    result = run(telegram_api.set_message_reaction(TOKEN, 1, 5, EYES))
    assert result["ok"] is True
    assert fake.calls[0]["json"] == {
        "chat_id": 1,
        "message_id": 5,
        "reaction": [{"type": "emoji", "emoji": EYES}],
    }


def test_reaction_set_contains_the_project_emoji():
    for emoji in (EYES, TROPHY, BANANA, "\U0001f44d", "\U0001f44e"):
        assert emoji in telegram_api.ALLOWED_REACTIONS
    assert telegram_api.normalize_reaction("\u2764\ufe0f") == "\u2764"
    assert telegram_api.normalize_reaction("\U0001f600") is None
    assert telegram_api.normalize_reaction(None) is None


def test_reaction_set_matches_the_bot_api_documentation():
    import inspect
    import re

    from aiogram.types import reaction_type_emoji

    line = next(
        line
        for line in inspect.getsource(reaction_type_emoji).splitlines()
        if "Currently, it can be one of" in line
    )
    documented = set(re.findall(r'"([^"]+)"', line.split("one of", 1)[1]))
    assert documented >= telegram_api.ALLOWED_REACTIONS


def test_reaction_variation_selector_is_normalized(session):
    fake = session(ok(True))
    run(telegram_api.set_message_reaction(TOKEN, 1, 5, "\u2764\ufe0f"))
    assert fake.calls[0]["json"]["reaction"] == [{"type": "emoji", "emoji": "\u2764"}]


def test_invalid_reaction_emoji_makes_no_request(session):
    fake = session(ok(True))
    result = run(telegram_api.set_message_reaction(TOKEN, 1, 5, "\U0001f600"))
    assert result["ok"] is False
    assert fake.calls == []


def test_remove_reaction(session):
    fake = session(ok(True))
    run(telegram_api.set_message_reaction(TOKEN, 1, 5, None))
    assert [call["json"]["reaction"] for call in fake.calls] == [[]]


def test_reaction_invalid_disables_that_emoji_in_that_chat(session, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(telegram_api.time, "monotonic", lambda: clock[0])
    fake = session(error(400, "Bad Request: REACTION_INVALID"), ok(True))
    assert run(telegram_api.set_message_reaction(TOKEN, 1, 5, EYES))["ok"] is False
    suppressed = run(telegram_api.set_message_reaction(TOKEN, 1, 6, EYES))
    assert suppressed["ok"] is False
    assert len(fake.calls) == 1  # not attempted again
    assert run(telegram_api.set_message_reaction(TOKEN, 1, 6, TROPHY))["ok"] is True
    assert run(telegram_api.set_message_reaction(TOKEN, 2, 6, EYES))["ok"] is True
    assert len(fake.calls) == 3
    clock[0] += 301
    assert run(telegram_api.set_message_reaction(TOKEN, 1, 7, EYES))["ok"] is True
    assert len(fake.calls) == 4


def test_reactions_not_allowed_disable_the_whole_chat(session):
    fake = session(error(400, "Bad Request: CHAT_ADMIN_REQUIRED"), ok(True))
    run(telegram_api.set_message_reaction(TOKEN, 1, 5, EYES))
    run(telegram_api.set_message_reaction(TOKEN, 1, 5, TROPHY))
    run(telegram_api.set_message_reaction(TOKEN, 1, 5, None))
    assert len(fake.calls) == 1


@pytest.mark.parametrize(
    "description",
    [
        "Bad Request: message to react not found",
        "Bad Request: MESSAGE_ID_INVALID",
        "Forbidden: bot was kicked from the supergroup chat",
    ],
)
def test_other_reaction_errors_do_not_disable_reactions(session, description, caplog):
    fake = session(error(400, description), ok(True))
    with caplog.at_level(logging.INFO, logger="src.telegram_api"):
        run(telegram_api.set_message_reaction(TOKEN, 1, 5, EYES))
    assert all(record.exc_info is None for record in caplog.records)
    assert run(telegram_api.set_message_reaction(TOKEN, 1, 5, EYES))["ok"] is True
    assert len(fake.calls) == 2


def test_reaction_suppression_can_be_disabled(session, monkeypatch):
    monkeypatch.setattr(config, "REACTION_DISABLE_SECONDS", 0)
    fake = session(error(400, "Bad Request: REACTION_INVALID"))
    run(telegram_api.set_message_reaction(TOKEN, 1, 5, EYES))
    run(telegram_api.set_message_reaction(TOKEN, 1, 5, EYES))
    assert len(fake.calls) == 2


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------
def test_set_webhook_payload(session):
    fake = session(ok(True))
    run(
        telegram_api.set_webhook(
            TOKEN,
            "https://bot.example.com/webhook",
            secret_token="s3cret",
            drop_pending_updates=True,
            allowed_updates=["message", "channel_post"],
            max_connections=10,
        )
    )
    assert fake.calls[0]["json"] == {
        "url": "https://bot.example.com/webhook",
        "secret_token": "s3cret",
        "allowed_updates": ["message", "channel_post"],
        "drop_pending_updates": True,
        "max_connections": 10,
    }


def test_set_webhook_minimal_call_and_delete_webhook(session):
    fake = session(ok(True))
    run(
        telegram_api.set_webhook(
            TOKEN, "https://bot.example.com/w", allowed_updates=["message"]
        )
    )
    run(telegram_api.delete_webhook(TOKEN))
    run(telegram_api.delete_webhook(TOKEN, drop_pending_updates=True))
    run(telegram_api.get_webhook_info(TOKEN))
    assert fake.methods == [
        "setWebhook",
        "deleteWebhook",
        "deleteWebhook",
        "getWebhookInfo",
    ]
    assert fake.calls[0]["json"] == {
        "url": "https://bot.example.com/w",
        "allowed_updates": ["message"],
    }
    assert fake.calls[1]["json"] == {}
    assert fake.calls[2]["json"] == {"drop_pending_updates": True}


# ---------------------------------------------------------------------------
# http_client / telegram_client
# ---------------------------------------------------------------------------
def test_http_session_is_shared_per_event_loop():
    async def use_twice():
        first = await http_client.get_session()
        second = await http_client.get_session()
        assert first is second
        assert first.timeout.total == 60
        assert isinstance(first.cookie_jar, aiohttp.DummyCookieJar)
        await http_client.close_session()
        assert first.closed
        third = await http_client.get_session()
        assert third is not first
        await http_client.close_session()
        await http_client.close_session()  # idempotent
        return first

    run(use_twice())
    run(use_twice())  # a new loop gets a new session


def test_http_session_from_another_loop_is_replaced():
    async def create():
        return await http_client.get_session()

    old = run(create())
    new = run(create())
    assert new is not old
    run(http_client.close_session())


def test_telegram_client_caches_bots_per_token():
    other = "987654321:BBH" + "y" * 32

    async def scenario():
        first = telegram_client.get_bot(TOKEN)
        assert telegram_client.get_bot(TOKEN) is first
        second = telegram_client.get_bot(other)
        assert second is not first
        assert second.token == other
        await telegram_client.close_all_bots()
        assert telegram_client.get_bot(TOKEN) is not first
        await telegram_client.close_all_bots()

    run(scenario())


def test_telegram_client_set_bot_and_local_api_server(monkeypatch):
    from aiogram import Bot

    async def scenario():
        bot = Bot(token=TOKEN)
        telegram_client.set_bot(bot)
        assert telegram_client.get_bot(TOKEN) is bot
        await telegram_client.close_all_bots()
        monkeypatch.setattr(config, "TELEGRAM_API_BASE", "http://bot-api:8081")
        local = telegram_client.get_bot(TOKEN)
        assert local.session.api.base == "http://bot-api:8081/bot{token}/{method}"
        await telegram_client.close_all_bots()

    run(scenario())
