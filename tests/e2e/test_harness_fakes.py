"""The harness tests itself: its fakes must refuse what the real services refuse.

If a fake is permissive, every scenario above it proves nothing, so each rule the
fakes claim gets a test that sends the forbidden thing and expects the refusal.
These tests provoke strict rejections on purpose, so they opt in with ``lenient``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from api_client.client import PasarGuardClient
from api_client.exceptions import (
    PasarGuardAPIError,
    PasarGuardAuthError,
    PasarGuardNotFoundError,
)
from api_client.models import UserCreate, UserModify
from tests.e2e.harness import GB, App
from tests.e2e.harness.app import JOBS
from tests.e2e.harness.htmlcheck import HtmlParseError, parse_html
from tests.e2e.harness.telegram import STRICT_KINDS

CHAT = 900_001  # an admin: the chat exists from the start


@pytest.fixture
def lenient(app: App) -> App:
    app.tg.accept(*STRICT_KINDS)
    return app


def kb(*buttons: InlineKeyboardButton) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(buttons)])


# ------------------------------------------------------------------ HTML rules


@pytest.mark.parametrize(
    "text",
    [
        "Ali <3",
        "a < b",
        "<b>open",
        "<b>x</i>",
        "<script>x</script>",
        "</b>",
        "<a>no href</a>",
        "<span>x</span>",
        "unclosed <",
    ],
)
def test_html_that_telegram_refuses_is_refused(text: str) -> None:
    with pytest.raises(HtmlParseError):
        parse_html(text)


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("<b>x</b> &lt; y &amp; z", "x < y & z"),
        ("a > b & c", "a > b & c"),
        ('<a href="https://x.test">go</a>', "go"),
        ("<b><i>both</i></b>", "both"),
        ("<code>1 &lt; 2</code>", "1 < 2"),
        ("&copy; stays", "&copy; stays"),
    ],
)
def test_html_that_telegram_accepts_is_accepted(text: str, shown: str) -> None:
    assert parse_html(text) == shown


# ----------------------------------------------------------- what bot may send


async def _rejected(app: App, kind: str, call: Callable[[], Any]) -> None:
    before = len(app.tg.rejections)
    with pytest.raises(TelegramBadRequest):
        await call()
    assert [r.kind for r in app.tg.rejections[before:]] == [kind]


async def test_empty_and_overlong_text_are_refused(lenient: App) -> None:
    bot = lenient.bot
    await _rejected(lenient, "empty_text", lambda: bot.send_message(CHAT, ""))
    await _rejected(lenient, "empty_text", lambda: bot.send_message(CHAT, "  \n "))
    await _rejected(lenient, "empty_text", lambda: bot.send_message(CHAT, "<b></b>"))
    await _rejected(lenient, "too_long", lambda: bot.send_message(CHAT, "x" * 4097))
    await bot.send_message(CHAT, "x" * 4096)  # the limit itself is fine


async def test_a_long_caption_is_refused(lenient: App) -> None:
    photo = BufferedInputFile(
        __import__("tests.e2e.harness.actors", fromlist=["x"]).receipt_png(), "r.png"
    )
    await _rejected(
        lenient, "too_long", lambda: lenient.bot.send_photo(CHAT, photo, caption="x" * 1025)
    )
    await lenient.bot.send_photo(CHAT, photo, caption="x" * 1024)


async def test_html_errors_are_refused_by_the_default_parse_mode(lenient: App) -> None:
    await _rejected(lenient, "html", lambda: lenient.bot.send_message(CHAT, "Ali <3"))
    await lenient.bot.send_message(CHAT, "Ali &lt;3")


async def test_callback_data_must_be_one_to_sixty_four_bytes(lenient: App) -> None:
    send = lenient.bot.send_message
    ok = InlineKeyboardButton(text="ok", callback_data="x" * 64)
    await send(CHAT, "fine", reply_markup=kb(ok))
    for data in ("x" * 65, "ی" * 33, ""):  # 66 bytes of Persian; empty
        bad = InlineKeyboardButton(text="bad", callback_data=data)
        await _rejected(lenient, "button_data", lambda b=bad: send(CHAT, "t", reply_markup=kb(b)))


async def test_buttons_need_text_and_exactly_one_action(lenient: App) -> None:
    send = lenient.bot.send_message
    cases = [
        InlineKeyboardButton(text="", callback_data="a"),
        InlineKeyboardButton(text="no action"),
        InlineKeyboardButton(text="two", callback_data="a", url="https://x.test"),
        InlineKeyboardButton(text="bad url", url="ftp://x.test"),
    ]
    for button in cases:
        await _rejected(lenient, "button", lambda b=button: send(CHAT, "t", reply_markup=kb(b)))


async def test_editing_to_the_same_content_is_not_modified(lenient: App) -> None:
    sent = await lenient.bot.send_message(
        CHAT, "same", reply_markup=kb(InlineKeyboardButton(text="a", callback_data="a"))
    )
    markup = kb(InlineKeyboardButton(text="a", callback_data="a"))
    await _rejected(lenient, "not_modified", lambda: sent.edit_text("same", reply_markup=markup))
    await sent.edit_text("changed", reply_markup=markup)


async def test_edits_of_missing_or_wrong_messages_are_refused(lenient: App) -> None:
    bot = lenient.bot
    await _rejected(
        lenient, "message_gone", lambda: bot.edit_message_text("x", chat_id=CHAT, message_id=999)
    )
    photo = BufferedInputFile(
        __import__("tests.e2e.harness.actors", fromlist=["x"]).receipt_png(), "r.png"
    )
    shot = await bot.send_photo(CHAT, photo, caption="c")
    await _rejected(
        lenient,
        "wrong_media",
        lambda: bot.edit_message_text("x", chat_id=CHAT, message_id=shot.message_id),
    )
    text = await bot.send_message(CHAT, "t")
    await _rejected(
        lenient,
        "wrong_media",
        lambda: bot.edit_message_caption(chat_id=CHAT, message_id=text.message_id, caption="x"),
    )
    await text.delete()
    await _rejected(lenient, "message_gone", lambda: text.edit_text("again"))


async def test_a_photo_must_be_an_image_the_bot_holds(lenient: App) -> None:
    junk = BufferedInputFile(b"not an image", "x.png")
    await _rejected(lenient, "photo", lambda: lenient.bot.send_photo(CHAT, junk))
    await _rejected(lenient, "photo", lambda: lenient.bot.send_photo(CHAT, "AgACneverseen"))


async def test_a_blocked_user_and_an_unknown_chat_are_refused(lenient: App) -> None:
    ali = lenient.customer("ali")
    await ali.start()
    lenient.tg.block(ali.user_id)
    with pytest.raises(TelegramForbiddenError):
        await lenient.bot.send_message(ali.user_id, "hello")
    lenient.tg.unblock(ali.user_id)
    await lenient.bot.send_message(ali.user_id, "hello")
    with pytest.raises(TelegramBadRequest):
        await lenient.bot.send_message(424242, "who?")


async def test_callbacks_must_be_answered_once_and_briefly(lenient: App) -> None:
    ali = lenient.customer("ali")
    await ali.start()
    await ali.press("nothing:here")  # the fallback answers it once
    (state,) = lenient.tg.callbacks.values()
    with pytest.raises(TelegramBadRequest):
        await lenient.bot.answer_callback_query(state.id)  # a second answer
    fresh = lenient.tg.open_callback("cbx", ali.user_id, 1)
    with pytest.raises(TelegramBadRequest):
        await lenient.bot.answer_callback_query(fresh.id, text="x" * 201)


async def test_an_unanswered_callback_fails_the_scenario(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiogram.types import CallbackQuery

    from tests.e2e.harness import UnansweredCallback

    async def silent(self: CallbackQuery, *a: Any, **k: Any) -> bool:
        return True

    monkeypatch.setattr(CallbackQuery, "answer", silent)
    ali = app.customer("ali")
    await ali.start()
    with pytest.raises(UnansweredCallback):
        await ali.press("nothing:here")


# --------------------------------------------------------------------- forums


async def test_a_forum_hands_out_thread_ids_and_keeps_messages_per_topic(app: App) -> None:
    group = app.tg.add_forum(-1009)
    a = await app.bot.create_forum_topic(group.id, "sales")
    b = await app.bot.create_forum_topic(group.id, "payments")
    assert a.message_thread_id != b.message_thread_id
    await app.bot.send_message(group.id, "one", message_thread_id=a.message_thread_id)
    await app.bot.send_message(group.id, "two", message_thread_id=b.message_thread_id)
    assert [m.text for m in app.tg.thread(group.id, a.message_thread_id)] == ["one"]
    assert [m.text for m in app.tg.thread(group.id, b.message_thread_id)] == ["two"]


async def test_a_missing_thread_or_missing_rights_are_refused(lenient: App) -> None:
    group = lenient.tg.add_forum(-1009)
    await _rejected(
        lenient, "no_thread", lambda: lenient.bot.send_message(group.id, "x", message_thread_id=77)
    )
    weak = lenient.tg.add_forum(-1010, can_manage_topics=False)
    await _rejected(lenient, "rights", lambda: lenient.bot.create_forum_topic(weak.id, "t"))


async def test_topic_edits_answer_the_way_real_telegram_did_live(lenient: App) -> None:
    group = lenient.tg.add_forum(-1011)
    topic = await lenient.bot.create_forum_topic(group.id, "sales")
    thread = topic.message_thread_id

    with pytest.raises(TelegramBadRequest, match="TOPIC_NOT_MODIFIED"):
        await lenient.bot.edit_forum_topic(group.id, thread, name="sales")
    assert await lenient.bot.edit_forum_topic(group.id, thread, name="sales 2")
    assert group.topics[thread] == "sales 2"

    group.topics.pop(thread)  # deleted by hand in Telegram
    with pytest.raises(TelegramBadRequest, match="TOPIC_ID_INVALID"):
        await lenient.bot.edit_forum_topic(group.id, thread, name="sales")


async def test_flood_control_throttles_the_named_call_then_lets_it_through(lenient: App) -> None:
    group = lenient.tg.add_forum(-1012)
    lenient.tg.flood_next("CreateForumTopic", retry_after=7)

    with pytest.raises(TelegramRetryAfter) as caught:
        await lenient.bot.create_forum_topic(group.id, "sales")
    assert caught.value.retry_after == 7
    assert group.topics == {}

    await lenient.bot.create_forum_topic(group.id, "sales")
    assert list(group.topics.values()) == ["sales"]


# ----------------------------------------------------------------------- jobs


async def test_every_scheduler_job_can_be_run_by_name(app: App) -> None:
    for name, job_id in JOBS.items():
        assert job_id in app._jobs, f"the real scheduler no longer registers {job_id}"
        assert name
    with pytest.raises(KeyError):
        await app.run_job("nope")
    await app.run_job("expire")
    await app.run_job("cleanup")


# ---------------------------------------------------------------- fake panel


def client_for(app: App) -> PasarGuardClient:
    return PasarGuardClient(
        base_url=app.panel.base_url,
        api_key=app.panel.api_key,
        client=httpx.AsyncClient(base_url=app.panel.base_url + "/", transport=app.panel.transport),
    )


async def test_the_panel_creates_conflicts_404s_and_writes_absolute_values(app: App) -> None:
    client = client_for(app)
    made = await client.create_user(
        UserCreate(
            username="u1", data_limit=5 * GB, expire=2_000_000_000, group_ids=[1], hwid_limit=2
        )
    )
    assert made.username == "u1" and app.panel.user("u1").gb == 5
    with pytest.raises(PasarGuardAPIError) as conflict:
        await client.create_user(UserCreate(username="u1"))
    assert conflict.value.status_code == 409
    with pytest.raises(PasarGuardNotFoundError):
        await client.get_user("ghost")
    await client.modify_user("u1", UserModify(data_limit=9 * GB))
    await client.modify_user("u1", UserModify(data_limit=9 * GB))  # a retry changes nothing
    user = app.panel.user("u1")
    assert user.data_limit == 9 * GB and user.hwid_limit == 2 and user.group_ids == [1]
    await client.reset_user_data_usage("u1")
    await client.disable_user("u1")
    assert app.panel.user("u1").status == "disabled"


async def test_revoking_rotates_the_link(app: App) -> None:
    client = client_for(app)
    first = await client.create_user(UserCreate(username="u1"))
    second = await client.revoke_user_subscription("u1")
    assert first.subscription_url != second.subscription_url
    assert app.panel.user("u1").token in (second.subscription_url or "")


async def test_a_group_the_panel_does_not_have_is_refused(app: App) -> None:
    with pytest.raises(PasarGuardAPIError):
        await client_for(app).create_user(UserCreate(username="u1", group_ids=[99]))


async def test_requests_are_checked_against_the_openapi_file(app: App) -> None:
    app.panel.allow_violations()
    headers = {"X-Api-Key": app.panel.api_key}
    async with httpx.AsyncClient(base_url=app.panel.base_url, transport=app.panel.transport) as raw:
        bad_field = await raw.post(
            "/api/user", json={"username": "u", "data_limt": 1}, headers=headers
        )
        no_name = await raw.post("/api/user", json={"data_limit": 1}, headers=headers)
        negative = await raw.post(
            "/api/user", json={"username": "u", "data_limit": -1}, headers=headers
        )
        bad_enum = await raw.post(
            "/api/user", json={"username": "u", "status": "weird"}, headers=headers
        )
        invented = await raw.get("/api/user/u/invented", headers=headers)
        bad_param = await raw.get("/api/groups", params={"limit": "many"}, headers=headers)
    assert [r.status_code for r in (bad_field, no_name, negative, bad_enum, bad_param)] == [422] * 5
    assert invented.status_code == 404
    assert len(app.panel.violations) == 6
    assert "silently" in app.panel.violations[0]  # the unknown-field message names the risk


async def test_a_violation_fails_the_scenario_unless_expected(app: App) -> None:
    async with httpx.AsyncClient(base_url=app.panel.base_url, transport=app.panel.transport) as raw:
        await raw.post(
            "/api/user",
            json={"username": "u", "data_limt": 1},
            headers={"X-Api-Key": app.panel.api_key},
        )
    with pytest.raises(AssertionError, match="real PasarGuard refuses"):
        app.final_checks()
    app.panel.allow_violations()
    app.final_checks()


async def test_panel_faults(app: App) -> None:
    client = client_for(app)
    app.panel.fail_next("POST /api/user", status=500)
    with pytest.raises(PasarGuardAPIError) as boom:
        await client.create_user(UserCreate(username="a"))
    assert boom.value.status_code == 500 and "a" not in app.panel.users
    await client.create_user(UserCreate(username="a"))  # the next call is healthy

    app.panel.timeout_next("PUT /api/user/{username}", applied=True)
    with pytest.raises(httpx.ReadTimeout):
        await client.modify_user("a", UserModify(data_limit=GB))
    assert app.panel.user("a").data_limit == GB  # applied, reply lost

    app.panel.timeout_next("PUT /api/user/{username}")
    with pytest.raises(httpx.ReadTimeout):
        await client.modify_user("a", UserModify(data_limit=2 * GB))
    assert app.panel.user("a").data_limit == GB  # never arrived

    app.panel.forbid("GET /api/nodes")
    with pytest.raises(PasarGuardAuthError) as denied:
        await client.list_nodes()
    assert denied.value.status_code == 403
    app.panel.allow("GET /api/nodes")
    assert len(await client.list_nodes()) == 1

    app.panel.go_down()
    with pytest.raises(httpx.ConnectError):
        await client.get_current_admin()
    app.panel.go_up()
    app.panel.reject_key()
    with pytest.raises(PasarGuardAuthError):
        await client.get_current_admin()
    app.panel.reject_key(False)
    await client.get_current_admin()


async def test_the_panel_reads_hwids_updates_and_nodes(app: App) -> None:
    client = client_for(app)
    made = await client.create_user(UserCreate(username="a"))
    app.panel.add_hwid("a", "device-1")
    app.panel.add_sub_update("a", "Hiddify/2.0")
    assert (await client.get_user_hwids(made.id)).count == 1
    assert (await client.list_sub_updates("a")).updates[0].user_agent == "Hiddify/2.0"
    await client.reset_user_hwids(made.id)
    assert (await client.get_user_hwids(made.id)).count == 0
    assert [g.name for g in (await client.list_groups_simple()).groups] == [
        "direct",
        "tunnel",
        "premium",
    ]
    assert (await client.get_system_stats()).total_user == 1
    app.panel.set_node(1, "error", "boom")
    assert (await client.list_nodes_simple())[0].status == "error"
    assert (await client.get_user_usage("a")).stats
