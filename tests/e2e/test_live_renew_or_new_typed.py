"""Live finding: while the bot waits for «📅 تمدید همان سرویس / ✨ سرویس جدید»,
a typed message got no answer at all."""

from __future__ import annotations

from bot.texts import texts
from tests.e2e._s08_helpers import buy_active
from tests.e2e.harness import App, browse_to_plan, plain, publish_store


async def test_typing_at_the_renew_or_new_question_gets_a_nudge_and_keeps_the_flow(
    app: App,
) -> None:
    store = await publish_store(app)
    ali = app.customer("ali")
    await buy_active(app, store, ali)
    await browse_to_plan(ali, store, "special_50")
    await ali.send("bobo")
    assert ali.last_text == plain(texts.purchase_renew_or_new)
    mark = ali.mark()

    await ali.send("تمدید کن")

    (nudge,) = ali.messages_since(mark)
    assert nudge.text != plain(
        texts.purchase_renew_or_new
    ), "say something new, not the same question"
    assert nudge.text != plain(texts.unknown_message)
    assert nudge.rows[:2] == [["📅 تمدید همان سرویس"], ["✨ سرویس جدید"]], nudge.rows
    # Still in the flow: the choice works and leads to the summary.
    await ali.tap("📅 تمدید همان سرویس")
    await ali.tap("بدون کد تخفیف")
    assert "ثبت سفارش" in " ".join(ali.buttons)
    assert len(app.panel.users) == 1
