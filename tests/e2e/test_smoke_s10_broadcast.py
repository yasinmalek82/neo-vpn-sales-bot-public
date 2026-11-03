"""Section ۱۰: broadcast messages.

Rows ۱۰.۱ to ۱۰.۸.
MANUAL: none. ۱۰.۳ is a process kill in real life; here the sender is stopped between two
recipients (the worker's sleep raises) and a fresh run of the job resumes from the database.
STALE: ۱۰.۱ names «یک گروه مخاطب» only; there are four audiences. ۱۰.۶ shows the signature
with the bot's username, here ``@neo_test_bot``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from database.models import Broadcast, BroadcastTarget
from database.models.broadcast import BroadcastState, DeliveryState
from tests.e2e._s07b_helpers import tap_starting
from tests.e2e._s09_helpers import compose_cast, open_cast, people, send_cast
from tests.e2e.harness import App, smoke
from tests.e2e.harness.settings import BOT_USERNAME

SIGNATURE = f"─────\nنئو نتورک · @{BOT_USERNAME}"


@pytest.fixture(autouse=True)
def _fast(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    from services import broadcast

    monkeypatch.setattr(broadcast, "SEND_INTERVAL_SECONDS", 0)


def _received(customer, body: str) -> list[str]:  # type: ignore[no-untyped-def]
    return [m.text for m in customer.messages if body in m.text]


@smoke("۱۰.۱")
async def test_choosing_an_audience_shows_how_many_people_it_reaches(app: App) -> None:
    from tests.e2e._s08_helpers import buy_active
    from tests.e2e.harness import publish_store

    store = await publish_store(app)
    ali, sara, _bob = await people(app, 3)
    await buy_active(app, store, ali)  # one person has a service
    admin = app.admin()
    await open_cast(admin)
    await compose_cast(admin, "سلام")
    assert "این پیام برای چه کسانی برود" in admin.last_text
    assert admin.buttons[:4] == [
        "👥 همهٔ مشتری‌ها (۳)",
        "📱 دارندگان سرویس (۱)",
        "⏳ سرویس‌های رو به انقضا (۰)",
        "🕸 بدون سرویس (۲)",
    ], admin.buttons
    await tap_starting(admin, "🕸")
    assert "ارسال به 2 نفر" in admin.last_text
    assert "بله، ارسال کن" in " ".join(admin.buttons)
    assert sara and not await app.all(
        select(Broadcast)
    ), "nothing is queued before the owner agrees"


@smoke("۱۰.۲")
async def test_sending_starts_and_the_status_button_shows_progress(app: App) -> None:
    customers = await people(app, 3)
    admin = app.admin()
    await send_cast(admin, "تخفیف ویژه")
    assert "در حال ارسال" in admin.last_text and "باقی‌مانده: 3" in admin.last_text
    await tap_starting(admin, "⬅️ پیام همگانی")
    assert "📊 وضعیت ارسال جاری" in admin.buttons
    assert "ارسال #1 در جریان است — 0 از 3" in admin.last_text
    await app.run_job("broadcast")
    await tap_starting(admin, "📊 وضعیت ارسال جاری")
    assert "رسیده: 3" in admin.last_text and "باقی‌مانده: 0" in admin.last_text
    assert all(len(_received(c, "تخفیف ویژه")) == 1 for c in customers)
    await app.run_job("broadcast")  # a finished campaign is not sent again
    assert all(len(_received(c, "تخفیف ویژه")) == 1 for c in customers)
    cast = await app.one(select(Broadcast))
    assert cast.state is BroadcastState.done


@smoke("۱۰.۳")
async def test_a_sender_that_dies_midway_resumes_without_duplicates(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services import broadcast

    customers = await people(app, 5)
    admin = app.admin()
    await send_cast(admin, "اطلاعیه")

    class Killed(BaseException):
        pass

    done = {"n": 0}

    async def die_after_two(_seconds: float) -> None:
        done["n"] += 1
        if done["n"] == 2:
            raise Killed

    class FakeAsyncio:
        sleep = staticmethod(die_after_two)

    with monkeypatch.context() as patch:
        patch.setattr(broadcast, "asyncio", FakeAsyncio)
        with pytest.raises(Killed):
            await app.run_job("broadcast")
    got_first = [c for c in customers if _received(c, "اطلاعیه")]
    assert len(got_first) == 2, "two recipients were served before the process died"
    states = [t.state for t in await app.all(select(BroadcastTarget).order_by(BroadcastTarget.id))]
    assert states.count(DeliveryState.sent) == 2 and states.count(DeliveryState.pending) == 3
    await app.run_job("broadcast")  # the restarted bot's next tick
    for c in customers:
        assert len(_received(c, "اطلاعیه")) == 1, f"{c.name} got it {len(_received(c, 'اطلاعیه'))}x"
    assert (await app.one(select(Broadcast))).state is BroadcastState.sending
    await app.run_job("broadcast")  # sees nothing pending and closes the campaign
    assert (await app.one(select(Broadcast))).state is BroadcastState.done


@smoke("۱۰.۴")
async def test_stop_halts_the_campaign_and_later_ticks_send_nothing(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services import broadcast

    customers = await people(app, 4)
    admin = app.admin()
    await send_cast(admin, "اطلاعیه")
    await tap_starting(admin, "⛔️ توقف ارسال")
    assert "متوقف" in admin.last_text or "توقف" in admin.last_text, admin.last_text
    await app.run_job("broadcast")
    assert not any(_received(c, "اطلاعیه") for c in customers), "a stopped campaign still sent"
    cast = await app.one(select(Broadcast))
    assert cast.state is BroadcastState.cancelled
    await open_cast(admin)
    assert "ارسالی در جریان نیست" in admin.last_text
    assert broadcast


@smoke("۱۰.۴")
async def test_stop_in_the_middle_keeps_what_was_delivered(app: App) -> None:
    from services import broadcast

    customers = await people(app, 4)
    admin = app.admin()
    await send_cast(admin, "اطلاعیه")
    async with app.session() as session:
        await broadcast.run_pending_broadcasts(session, app.bot, batch=2)
    assert sum(1 for c in customers if _received(c, "اطلاعیه")) == 2
    await open_cast(admin)
    await tap_starting(admin, "📊")
    await tap_starting(admin, "⛔️ توقف ارسال")
    await app.run_job("broadcast")
    assert sum(1 for c in customers if _received(c, "اطلاعیه")) == 2, "it kept sending after stop"
    assert (await app.one(select(Broadcast))).state is BroadcastState.cancelled


@smoke("۱۰.۵")
async def test_a_customer_who_blocked_the_bot_is_recorded_once_and_not_retried(app: App) -> None:
    customers = await people(app, 3)
    blocker = customers[1]
    app.tg.block(blocker.user_id)
    admin = app.admin()
    await send_cast(admin, "اطلاعیه")
    await app.run_job("broadcast")
    chat_id = app.tg.chats[blocker.user_id].id
    attempts = [
        c for c in app.tg.calls if c.method == "SendMessage" and c.params["chat_id"] == chat_id
    ]
    target = await app.one(
        select(BroadcastTarget).where(BroadcastTarget.telegram_id == blocker.user_id)
    )
    assert target.state is DeliveryState.blocked and target.attempts == 1
    for _ in range(3):
        await app.run_job("broadcast")
    again = [
        c for c in app.tg.calls if c.method == "SendMessage" and c.params["chat_id"] == chat_id
    ]
    assert len(again) == len(attempts), "the bot kept trying someone who blocked it"
    assert not _received(blocker, "اطلاعیه")
    assert all(_received(c, "اطلاعیه") for c in customers if c is not blocker)


@smoke("۱۰.۶", "۱۰.۷")
async def test_the_preview_shows_the_signature_the_bot_added_and_the_delivery_is_identical(
    app: App,
) -> None:
    ali, sara = await people(app, 2)
    admin = app.admin()
    await open_cast(admin)
    await compose_cast(admin, "سلام دوستان\nتخفیف امشب")
    preview = admin.last_text
    assert SIGNATURE in preview, preview
    shown = preview.split("─────────────\n", 1)[1].rsplit("\n─────────────", 1)[0].strip()
    assert shown == f"سلام دوستان\nتخفیف امشب\n\n{SIGNATURE}"
    await tap_starting(admin, "👥 همهٔ")
    assert shown in admin.last_text, "the confirm screen shows another text than the preview"
    await tap_starting(admin, "📤")
    await app.run_job("broadcast")
    for c in (ali, sara):
        assert c.last_text == shown, f"delivered:\n{c.last_text}\npreview:\n{shown}"
    stored = await app.one(select(Broadcast))
    assert stored.body == shown, "the stored body is not what was previewed"


@smoke("۱۰.۸")
async def test_a_signature_the_owner_typed_himself_appears_once(app: App) -> None:
    ali = (await people(app, 1))[0]
    admin = app.admin()
    await open_cast(admin)
    typed = f"فروش ویژه\n\n{SIGNATURE}"
    await compose_cast(admin, typed)
    assert admin.last_text.count("نئو نتورک") == 1, admin.last_text
    await tap_starting(admin, "👥 همهٔ")
    assert admin.last_text.count("نئو نتورک") == 1
    await tap_starting(admin, "📤")
    await app.run_job("broadcast")
    assert ali.last_text == typed
    assert ali.last_text.count("نئو نتورک") == 1
