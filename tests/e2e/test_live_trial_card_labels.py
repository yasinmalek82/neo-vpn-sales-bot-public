"""Live finding: the delivered trial card had two lines both labelled «سرویس:»
(the store group, then the config name)."""

from __future__ import annotations

from bot.keyboards.reply_menu import TRIAL
from tests.e2e._s01_helpers import set_group_trial
from tests.e2e.harness import App, publish_store


async def test_the_trial_card_labels_the_group_as_the_group(app: App) -> None:
    store = await publish_store(app, trial_gb=0)
    await set_group_trial(app, store, "special", 1, 1)
    ali = app.customer("ali")
    await ali.start()
    mark = ali.mark()

    await ali.tap_reply(TRIAL)

    card = ali.texts_since(mark)[0]
    labels = [line.split(":", 1)[0].strip() for line in card.splitlines() if ":" in line]
    assert len(labels) == len(set(labels)), card
    assert "📂 گروه: ویژه" in card.splitlines(), card
