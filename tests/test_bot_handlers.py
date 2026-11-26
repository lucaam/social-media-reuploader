"""Handler behaviour (src.handlers through src.bot.create_dispatcher).

Updates are built as the raw JSON Telegram sends (UTF-16 entity offsets) and
fed to the real aiogram dispatcher. The worker pool is a fake; Telegram
replies (telegram_api) are recorded. Link checks use the real providers module
(offline: the installed yt-dlp extractor list, no network).
"""

import asyncio
import logging
import time
from enum import StrEnum

import pytest
from aiogram import Bot
from aiogram.types import Update

from src import config, handlers, providers, telegram_api
from src.bot import create_dispatcher
from src.providers import LinkCheck, ProviderMatch, Rejection

TOKEN = "123456789:" + "A" * 35
YT_ID = "dQw4w9WgXcQ"
YT = f"https://youtu.be/{YT_ID}"
VIMEO = "https://vimeo.com/76979871"
SITES = "YouTube, TikTok e oltre 800 altri siti"
FIRE = "\U0001f525"  # 2 UTF-16 code units
CLAPPER = "\U0001f3ac"


class Status(StrEnum):  # stands in for src.worker.SubmitStatus
    QUEUED = "queued"


class FakePool:
    """Records submit() calls with the contract's keyword-only signature."""

    def __init__(self, *, fail_urls=(), delay=0.0):
        self.calls = []
        self.fail_urls = set(fail_urls)
        self.delay = delay

    async def submit(
        self,
        chat_id,
        url,
        *,
        chat_type=None,
        user_id=None,
        original_message_id=None,
        description=None,
        provider=None,
    ):
        self.calls.append(
            {
                "chat_id": chat_id,
                "url": url,
                "chat_type": chat_type,
                "user_id": user_id,
                "original_message_id": original_message_id,
                "description": description,
                "provider": provider,
            }
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        if url in self.fail_urls:
            raise RuntimeError("database is locked")
        return type("Result", (), {"status": Status.QUEUED, "request_id": 1})()


@pytest.fixture(scope="module", autouse=True)
def _warm_registry():
    providers.warm_up()


@pytest.fixture(autouse=True)
def settings(monkeypatch, tmp_path):
    monkeypatch.setenv("REQUESTS_DB", str(tmp_path / "requests.db"))
    monkeypatch.setattr(config, "UNSUPPORTED_LINK_REPLY", "private")
    monkeypatch.setattr(config, "DUPLICATE_WINDOW_SECONDS", 5400)
    monkeypatch.setattr(config, "ALLOWED_PROVIDERS", ())
    monkeypatch.setattr(config, "BLOCKED_PROVIDERS", ())
    monkeypatch.setattr(config, "ALLOW_COLLECTION_LINKS", False)
    monkeypatch.setattr(providers, "supported_sites_text", lambda: SITES)
    providers._reset_for_tests()
    yield
    providers._reset_for_tests()


@pytest.fixture(autouse=True)
def telegram(monkeypatch):
    """Recorded Telegram calls made by the handlers."""
    record = {"messages": [], "reactions": [], "message_result": {"ok": True}}

    async def send_message(token, chat_id, text, reply_to_message_id=None, **kw):
        assert token == TOKEN
        record["messages"].append(
            {"chat_id": chat_id, "text": text, "reply_to": reply_to_message_id}
        )
        return record["message_result"]

    async def set_message_reaction(token, chat_id, message_id, emoji=None, **kw):
        assert token == TOKEN
        record["reactions"].append((chat_id, message_id, emoji))
        return {"ok": True, "result": True}

    monkeypatch.setattr(telegram_api, "send_message", send_message)
    monkeypatch.setattr(telegram_api, "set_message_reaction", set_message_reaction)
    return record


# ---------------------------------------------------------------------------
# Update builders (raw Bot API JSON)
# ---------------------------------------------------------------------------
def utf16_len(text):
    return len(text.encode("utf-16-le")) // 2


def entity(text, part, etype="url", **extra):
    """Entity covering ``part`` of ``text`` with UTF-16 offset/length."""
    start = text.index(part)
    return {
        "type": etype,
        "offset": utf16_len(text[:start]),
        "length": utf16_len(part),
        **extra,
    }


def chat(chat_id=111, chat_type="private"):
    data = {"id": chat_id, "type": chat_type}
    if chat_type != "private":
        data["title"] = "Chat"
    return data


def message(
    text=None,
    *,
    entities=None,
    caption=None,
    caption_entities=None,
    chat_id=111,
    chat_type="private",
    message_id=7,
    user_id=42,
    age=0,
):
    msg = {
        "message_id": message_id,
        "date": int(time.time() - age),
        "chat": chat(chat_id, chat_type),
    }
    if chat_type == "channel":
        msg["sender_chat"] = chat(chat_id, chat_type)
    else:
        msg["from"] = {"id": user_id, "is_bot": False, "first_name": "Ada"}
    if text is not None:
        msg["text"] = text
    if entities is not None:
        msg["entities"] = entities
    if caption is not None:
        msg["caption"] = caption
        msg["photo"] = [
            {"file_id": "f", "file_unique_id": "u", "width": 90, "height": 90}
        ]
    if caption_entities is not None:
        msg["caption_entities"] = caption_entities
    return msg


def update(kind="message", update_id=1, **kwargs):
    msg = message(**kwargs)
    if kind.startswith("edited"):
        msg["edit_date"] = int(time.time())
    return {"update_id": update_id, kind: msg}


def text_update(text, *urls, kind="message", **kwargs):
    """Message whose ``urls`` carry url entities, as Telegram linkifies them."""
    entities = [entity(text, url) for url in urls]
    return update(kind, text=text, entities=entities or None, **kwargs)


def member_update(chat_type="group", old="left", new="member", chat_id=-100):
    bot_user = {"id": 123456789, "is_bot": True, "first_name": "Bot"}

    def member(status):
        data = {"status": status, "user": bot_user}
        if status == "administrator":
            data.update(
                {
                    "can_be_edited": False,
                    "is_anonymous": False,
                    "can_manage_chat": True,
                    "can_delete_messages": True,
                    "can_manage_video_chats": False,
                    "can_restrict_members": False,
                    "can_promote_members": False,
                    "can_change_info": False,
                    "can_invite_users": True,
                    "can_post_stories": False,
                    "can_edit_stories": False,
                    "can_delete_stories": False,
                }
            )
        if status == "kicked":
            data["until_date"] = 0
        return data

    return {
        "update_id": 9,
        "my_chat_member": {
            "chat": chat(chat_id, chat_type),
            "from": {"id": 42, "is_bot": False, "first_name": "Ada"},
            "date": int(time.time()),
            "old_chat_member": member(old),
            "new_chat_member": member(new),
        },
    }


def feed(*raw_updates, pool=None, bot_info=None):
    """Feed updates through a real dispatcher; returns the pool."""
    pool = pool if pool is not None else FakePool()

    async def go():
        bot = Bot(TOKEN)
        dispatcher = create_dispatcher(pool, bot_info=bot_info)
        for raw in raw_updates:
            parsed = Update.model_validate(raw, context={"bot": bot})
            await dispatcher.feed_update(bot, parsed)

    asyncio.run(go())
    return pool


# ---------------------------------------------------------------------------
# Supported links
# ---------------------------------------------------------------------------
def test_private_link_is_submitted_with_the_message_context(telegram):
    text = f"guarda questo {YT} bellissimo"
    pool = feed(text_update(text, YT))

    assert len(pool.calls) == 1
    call = pool.calls[0]
    assert call["chat_id"] == 111
    assert call["url"] == YT
    assert call["chat_type"] == "private"
    assert call["user_id"] == 42
    assert call["original_message_id"] == 7
    assert call["description"] == "guarda questo bellissimo"
    assert isinstance(call["provider"], ProviderMatch)
    assert call["provider"].key == "Youtube"
    assert call["provider"].url == call["url"]
    assert telegram["messages"] == [] and telegram["reactions"] == []


def test_message_with_only_a_link_has_no_description():
    pool = feed(text_update(YT, YT))
    assert pool.calls[0]["description"] is None


def test_links_are_submitted_in_text_order_and_deduplicated():
    same_video = f"https://www.youtube.com/watch?v={YT_ID}&si=tracking"
    text = f"{YT} poi {same_video} e infine {VIMEO}"
    pool = feed(text_update(text, YT, same_video, VIMEO))

    assert [c["url"] for c in pool.calls] == [YT, VIMEO]
    assert [c["provider"].key for c in pool.calls] == ["Youtube", "Vimeo"]


def test_each_media_of_a_post_is_submitted(telegram):
    post = "https://x.com/someuser/status/1234567890123456789"
    first, second = f"{post}/video/1", f"{post}/video/2"
    pool = feed(text_update(f"{first} {second}", first, second))
    assert [c["url"] for c in pool.calls] == [first, second]
    assert len({c["provider"].normalized_url for c in pool.calls}) == 2


@pytest.mark.parametrize(
    "template",
    ["guarda {url}.", "guarda ({url})", "«{url}»!", "{url}, {url}"],
)
def test_trailing_punctuation_and_brackets_are_not_part_of_the_url(template):
    text = template.format(url=YT)
    pool = feed(text_update(text, YT))  # Telegram's entity excludes punctuation
    assert [c["url"] for c in pool.calls] == [YT]


def test_trailing_punctuation_without_entities():
    pool = feed(update(text=f"guarda ({YT})."))
    assert [c["url"] for c in pool.calls] == [YT]


def test_utf16_entity_offsets_end_to_end():
    text = f"{FIRE}{FIRE} {YT} {CLAPPER} vedi qui e {VIMEO}"
    entities = [
        entity(text, YT),
        entity(text, "vedi qui", "text_link", url="https://vimeo.com/1084537"),
        entity(text, VIMEO),
    ]
    assert entities[0]["offset"] == 5  # two surrogate pairs + space
    pool = feed(update(text=text, entities=entities))

    assert [c["url"] for c in pool.calls] == [
        YT,
        "https://vimeo.com/1084537",
        VIMEO,
    ]
    # The visible text of the text_link is kept, written URLs are removed.
    assert pool.calls[0]["description"] == f"{FIRE}{FIRE} {CLAPPER} vedi qui e"


def test_scheme_less_url_entity_gets_https():
    text = f"youtube.com/watch?v={YT_ID}"
    pool = feed(text_update(text, text))
    assert [c["url"] for c in pool.calls] == [f"https://youtube.com/watch?v={YT_ID}"]


def test_caption_and_caption_entities_of_media_messages():
    caption = f"{CLAPPER} {VIMEO}"
    raw = update(caption=caption, caption_entities=[entity(caption, VIMEO)])
    pool = feed(raw)

    assert [c["url"] for c in pool.calls] == [VIMEO]
    assert pool.calls[0]["description"] == CLAPPER


def test_text_link_entity_uses_its_target():
    text = "clicca qui per il video"
    raw = update(
        text=text, entities=[entity(text, "qui", "text_link", url=VIMEO + "?share=1")]
    )
    pool = feed(raw)

    assert [c["url"] for c in pool.calls] == [VIMEO]
    assert pool.calls[0]["description"] == text


def test_group_links_are_submitted_with_the_group_chat_type():
    pool = feed(text_update(YT, YT, chat_id=-1001, chat_type="supergroup"))
    assert pool.calls[0]["chat_id"] == -1001
    assert pool.calls[0]["chat_type"] == "supergroup"


def test_domain_rules_accept_the_short_domain_of_an_allowed_site(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_PROVIDERS", ("youtube.com",))
    pool = feed(text_update(YT, YT))
    assert [c["provider"].key for c in pool.calls] == ["Youtube"]

    monkeypatch.setattr(config, "ALLOWED_PROVIDERS", ())
    monkeypatch.setattr(config, "BLOCKED_PROVIDERS", ("youtube.com",))
    assert feed(text_update(YT, YT)).calls == []


def test_max_links_per_message(monkeypatch):
    monkeypatch.setattr(config, "MAX_LINKS_PER_MESSAGE", 2, raising=False)
    urls = [YT, VIMEO, "https://vimeo.com/1084537"]
    pool = feed(text_update(" ".join(urls), *urls))
    assert [c["url"] for c in pool.calls] == urls[:2]


def test_submit_failure_does_not_stop_the_other_links(caplog, telegram):
    pool = FakePool(fail_urls={YT})
    with caplog.at_level(logging.ERROR, logger="src.handlers"):
        feed(text_update(f"{YT} {VIMEO}", YT, VIMEO), pool=pool)

    assert [c["url"] for c in pool.calls] == [YT, VIMEO]
    assert "Could not submit a Youtube link of chat=111 message=7" in caplog.text
    assert telegram["messages"] == []


# ---------------------------------------------------------------------------
# Telegram links and refused links
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("chat_type", ["private", "group"])
def test_telegram_links_are_ignored(chat_type, telegram):
    text = "https://t.me/somechannel/123 e t.me/joinchat/abc"
    pool = feed(text_update(text, "https://t.me/somechannel/123", chat_type=chat_type))

    assert pool.calls == []
    assert telegram["messages"] == [] and telegram["reactions"] == []


def test_unsupported_links_get_one_reply_and_a_reaction_in_private(telegram):
    first, second = "https://example.com/video/1", "https://github.com/yt-dlp/yt-dlp"
    pool = feed(text_update(f"{first} e anche {second}", first, second))

    assert pool.calls == []
    assert telegram["messages"] == [
        {
            "chat_id": 111,
            "text": f"Link non supportato. Supporto {SITES}.",
            "reply_to": 7,
        }
    ]
    assert telegram["reactions"] == [(111, 7, handlers.REJECT_REACTION)]


@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
def test_unsupported_links_are_silent_outside_private_chats(chat_type, telegram):
    url = "https://example.com/video/1"
    pool = feed(text_update(url, url, chat_id=-5, chat_type=chat_type))

    assert pool.calls == []
    assert telegram["messages"] == [] and telegram["reactions"] == []


@pytest.mark.parametrize(
    ("policy", "chat_type", "replied"),
    [
        ("always", "supergroup", True),
        ("always", "private", True),
        ("never", "private", False),
        ("never", "group", False),
        ("private", "private", True),
        ("private", "group", False),
    ],
)
def test_unsupported_link_reply_policy(
    monkeypatch, telegram, policy, chat_type, replied
):
    monkeypatch.setattr(config, "UNSUPPORTED_LINK_REPLY", policy)
    url = "https://example.com/video/1"
    feed(text_update(url, url, chat_id=-5, chat_type=chat_type))

    assert len(telegram["messages"]) == int(replied)
    assert len(telegram["reactions"]) == int(replied)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.youtube.com/@MrBeast", "profilo, di un canale o di una playlist"),
        ("https://www.instagram.com/someuser/", "temporaneamente fuori servizio"),
        (f"https://www.youtube.com/watch?v={YT_ID[:3]}", "Link non valido"),
        ("https://www.tiktok.com/@user/video/7234567890123456789", "disattivati"),
    ],
)
def test_refusal_reason_texts(monkeypatch, telegram, url, expected):
    monkeypatch.setattr(config, "BLOCKED_PROVIDERS", ("tiktok",))
    feed(text_update(url, url))

    assert len(telegram["messages"]) == 1
    assert expected in telegram["messages"][0]["text"]
    assert SITES not in telegram["messages"][0]["text"]


def test_different_reasons_share_one_reply(telegram):
    collection, unsupported = (
        "https://www.youtube.com/@MrBeast",
        "https://example.com/a",
    )
    feed(text_update(f"{collection} {unsupported}", collection, unsupported))

    assert len(telegram["messages"]) == 1
    lines = telegram["messages"][0]["text"].splitlines()
    assert len(lines) == 2
    assert "playlist" in lines[0]
    assert lines[1] == f"Link non supportato. Supporto {SITES}."


def test_mixed_message_submits_and_replies_without_reaction(telegram):
    unsupported = "https://example.com/video/1"
    pool = feed(text_update(f"{YT} {unsupported}", YT, unsupported))

    assert [c["url"] for c in pool.calls] == [YT]
    assert len(telegram["messages"]) == 1
    # The worker uses the (single) bot reaction of this message for progress.
    assert telegram["reactions"] == []


def test_failed_reply_is_logged(caplog, telegram):
    telegram["message_result"] = {
        "ok": False,
        "error_code": 403,
        "description": f"Forbidden: bot was blocked by the user {TOKEN}",
    }
    url = "https://example.com/video/1"
    with caplog.at_level(logging.WARNING, logger="src.handlers"):
        feed(text_update(url, url))

    assert "Could not answer refused links in chat=111" in caplog.text
    assert TOKEN not in caplog.text


def test_refused_links_from_a_fake_checker(monkeypatch, telegram):
    async def check(url, *, resolve=True):
        return LinkCheck(url=url, match=None, reason=Rejection.BROKEN)

    monkeypatch.setattr(providers, "check_link_async", check)
    url = "https://example.com/video/1"
    feed(text_update(url, url))
    assert "Riprova più avanti" in telegram["messages"][0]["text"]


# ---------------------------------------------------------------------------
# Edits and channels
# ---------------------------------------------------------------------------
def test_recent_edit_is_submitted_again_for_the_worker_dedupe(telegram):
    pool = feed(
        text_update("guarda", kind="message", update_id=1),
        text_update(f"guarda {YT}", YT, kind="edited_message", update_id=2, age=60),
    )
    assert len(pool.calls) == 1
    assert pool.calls[0]["original_message_id"] == 7
    assert pool.calls[0]["url"] == YT


def test_edit_of_an_old_message_is_ignored(monkeypatch):
    monkeypatch.setattr(config, "DUPLICATE_WINDOW_SECONDS", 600)
    pool = feed(text_update(YT, YT, kind="edited_message", age=601))
    assert pool.calls == []


def test_edits_never_trigger_refusal_replies(telegram):
    url = "https://example.com/video/1"
    pool = feed(text_update(url, url, kind="edited_message", age=5))
    assert pool.calls == []
    assert telegram["messages"] == [] and telegram["reactions"] == []


def test_channel_post_is_submitted_without_user():
    pool = feed(text_update(VIMEO, VIMEO, kind="channel_post", chat_type="channel"))
    assert pool.calls[0]["chat_type"] == "channel"
    assert pool.calls[0]["user_id"] is None
    assert pool.calls[0]["chat_id"] == 111


def test_edited_channel_post_is_handled_like_an_edit(monkeypatch):
    raw = text_update(
        VIMEO, VIMEO, kind="edited_channel_post", chat_type="channel", age=30
    )
    assert feed(raw).calls[0]["url"] == VIMEO
    monkeypatch.setattr(config, "DUPLICATE_WINDOW_SECONDS", 10)
    assert feed(raw).calls == []


def test_without_duplicate_window_edits_have_no_age_limit(monkeypatch):
    monkeypatch.setattr(config, "DUPLICATE_WINDOW_SECONDS", 0)
    pool = feed(text_update(YT, YT, kind="edited_message", age=30 * 86400))
    assert [c["url"] for c in pool.calls] == [YT]


# ---------------------------------------------------------------------------
# Privacy of logs
# ---------------------------------------------------------------------------
def test_messages_without_links_do_nothing_and_are_not_logged(caplog, telegram):
    with caplog.at_level(logging.DEBUG):
        pool = feed(update(text="ciao a tutti, il codice segreto è 1234"))

    assert pool.calls == [] and telegram["messages"] == []
    assert "segreto" not in caplog.text
    assert not [r for r in caplog.records if r.name.startswith("src.")]


def test_message_text_is_never_logged(caplog):
    text = f"password segreta 1234 {YT}"
    with caplog.at_level(logging.DEBUG):
        feed(text_update(text, YT, chat_id=555, message_id=99))

    assert "segreta" not in caplog.text
    info = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
    assert any("chat=555 message=99: 1 link(s)" in line for line in info)
    assert not any(YT in line for line in info)


def test_handler_errors_are_logged_not_raised(monkeypatch, caplog):
    async def broken(url, *, resolve=True):
        raise RuntimeError("boom")

    monkeypatch.setattr(providers, "check_link_async", broken)
    with caplog.at_level(logging.ERROR, logger="src.handlers"):
        pool = feed(text_update(YT, YT))

    assert pool.calls == []
    assert "Error while handling update 1: RuntimeError" in caplog.text


# ---------------------------------------------------------------------------
# /start, /help and the group welcome
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("command", ["/start", "/help", "/start ref123"])
def test_start_and_help_in_private(command, telegram):
    pool = feed(
        update(
            text=command, entities=[entity(command, command.split()[0], "bot_command")]
        )
    )

    assert pool.calls == []
    assert len(telegram["messages"]) == 1
    sent = telegram["messages"][0]
    assert sent["chat_id"] == 111 and sent["reply_to"] is None
    for part in (SITES, "Limiti", "Privacy", "Gruppi", "90 minuti"):
        assert part in sent["text"]


@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
def test_help_in_groups_is_ignored(chat_type, telegram):
    feed(update(text="/help", chat_type=chat_type, chat_id=-7))
    assert telegram["messages"] == []


@pytest.mark.parametrize(
    ("chat_type", "old", "new", "bot_info", "hint"),
    [
        ("group", "left", "member", None, True),
        (
            "supergroup",
            "kicked",
            "member",
            {"can_read_all_group_messages": False},
            True,
        ),
        ("supergroup", "left", "member", {"can_read_all_group_messages": True}, False),
        ("group", "left", "administrator", None, False),
    ],
)
def test_welcome_when_added_to_a_group(telegram, chat_type, old, new, bot_info, hint):
    feed(member_update(chat_type, old, new), bot_info=bot_info)

    assert len(telegram["messages"]) == 1
    text = telegram["messages"][0]["text"]
    assert telegram["messages"][0]["chat_id"] == -100
    assert SITES in text
    assert ("amministratore del gruppo" in text) is hint


@pytest.mark.parametrize(
    ("chat_type", "old", "new"),
    [
        ("channel", "left", "administrator"),  # added to a channel
        ("private", "kicked", "member"),  # user unblocked the bot
        ("group", "member", "administrator"),  # promoted, already a member
        ("supergroup", "member", "left"),  # removed
    ],
)
def test_no_welcome_elsewhere(telegram, chat_type, old, new):
    feed(member_update(chat_type, old, new))
    assert telegram["messages"] == []


# ---------------------------------------------------------------------------
# Texts and policy helpers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (5400, "90 minuti"),
        (3600, "1 ora"),
        (7200, "2 ore"),
        (86400, "1 giorno"),
        (60, "1 minuto"),
        (45, "45 secondi"),
        (1, "1 secondo"),
    ],
)
def test_duration_text(seconds, text):
    assert handlers.duration_text(seconds) == text


def test_rate_limits_text():
    limits = ((1, 10), (3, 60), (5, 3600), (20, 30 * 86400))
    assert handlers.rate_limits_text(limits) == (
        "1 link ogni 10 secondi, 3 al minuto, 5 all'ora e 20 ogni 30 giorni"
    )
    assert handlers.rate_limits_text([(2, 86400)]) == "2 link al giorno"
    assert handlers.rate_limits_text(()) is None


def test_unsupported_reply_text_orders_and_dedupes_reasons():
    reasons = [Rejection.UNSUPPORTED, Rejection.INVALID, Rejection.UNSUPPORTED]
    lines = handlers.unsupported_reply_text(reasons, "X").splitlines()
    assert lines == [
        "Link non valido: controlla di averlo copiato per intero.",
        "Link non supportato. Supporto X.",
    ]


def test_help_text_follows_the_configuration(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMITS", ())
    monkeypatch.setattr(config, "DUPLICATE_WINDOW_SECONDS", 0)
    monkeypatch.setattr(config, "MAX_PENDING_PER_CHAT", 2)
    monkeypatch.setattr(config, "TELEGRAM_MAX_FILE_SIZE", 2000 * 1024 * 1024)
    monkeypatch.setattr(config, "MAX_DURATION_SECONDS", 0)
    monkeypatch.setattr(config, "KEEP_DOWNLOADED_FILES", True)
    text = handlers.help_text("YouTube")

    assert "Al massimo 2 link in lavorazione" in text
    assert "2000 MB" in text
    assert "ogni 10 secondi" not in text and "ripubblicato" not in text
    assert "video più lunghi" not in text
    assert "cancellati" not in text
    assert "Siti supportati: YouTube." in text


@pytest.mark.parametrize(
    ("policy", "chat_type", "expected"),
    [
        ("private", "private", True),
        ("private", "group", False),
        ("private", None, False),
        ("always", "channel", True),
        ("never", "private", False),
        ("ALWAYS", "group", True),
    ],
)
def test_should_reply_unsupported(monkeypatch, policy, chat_type, expected):
    monkeypatch.setattr(config, "UNSUPPORTED_LINK_REPLY", policy)
    assert handlers.should_reply_unsupported(chat_type) is expected


def test_allowed_updates_cover_every_registered_handler():
    dispatcher = create_dispatcher(FakePool())
    used = set(dispatcher.resolve_used_update_types())
    assert used == set(handlers.ALLOWED_UPDATES)
