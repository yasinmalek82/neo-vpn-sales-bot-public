"""Business deduplication must not suppress retry of an undelivered receipt."""

import pytest

from tests.e2e.harness import App, publish_store
from tests.e2e.test_smoke_s15_late_receipt import _expired_order, _late_photos


async def test_late_receipt_retries_only_failed_admin_and_keeps_original_attachment(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await publish_store(app)
    buyer = app.customer("ali")
    await _expired_order(app, store, buyer)
    first_admin, second_admin = app.admin(0), app.admin(1)
    original_send = app.tg._m_SendPhoto
    unavailable = True

    async def fail_first_admin(bot, method):
        if unavailable and method.chat_id == first_admin.user_id:
            raise TimeoutError("test transport unavailable")
        return await original_send(bot, method)

    monkeypatch.setattr(app.tg, "_m_SendPhoto", fail_first_admin)
    file_id = await buyer.send_photo()
    assert _late_photos(first_admin) == []
    assert len(_late_photos(second_admin)) == 1
    unavailable = False

    # A repeat must repair delivery, not create another payment or replace
    # the evidence that was originally acknowledged.
    await buyer.send_photo()
    assert [m.file_id for m in _late_photos(first_admin)] == [file_id]
    assert [m.file_id for m in _late_photos(second_admin)] == [file_id]
    assert await app.wallet_balance(buyer) == 0


async def test_scheduler_recovers_late_receipt_without_another_customer_message(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await publish_store(app)
    buyer = app.customer("ali")
    await _expired_order(app, store, buyer)
    original_send = app.tg._m_SendPhoto

    async def unavailable(bot, method):
        raise TimeoutError("test transport unavailable")

    monkeypatch.setattr(app.tg, "_m_SendPhoto", unavailable)
    file_id = await buyer.send_photo()
    assert _late_photos(app.admin(0)) == []
    monkeypatch.setattr(app.tg, "_m_SendPhoto", original_send)
    await app.run_job("delivery")
    await app.run_job("delivery")
    for index in range(2):
        assert [m.file_id for m in _late_photos(app.admin(index))] == [file_id]
    assert await app.wallet_balance(buyer) == 0


@pytest.mark.parametrize("interruption", ["transport", "after_commit"])
async def test_activated_service_is_delivered_by_worker_after_telegram_failure(
    app: App, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    from database.models import OrderStatus
    from tests.e2e.harness import order_by_card

    store = await publish_store(app)
    buyer = app.customer("ali")
    await buyer.start()
    order = await order_by_card(buyer, store)
    await buyer.send_photo()
    admin = app.admin()
    await admin.send("/admin")
    await admin.tap("پرداخت‌ها")
    await admin.tap(f"#{order.id}")
    photo_send, text_send = app.tg._m_SendPhoto, app.tg._m_SendMessage

    async def fail_photo(bot, method):
        if method.chat_id == buyer.user_id:
            raise TimeoutError("test buyer unavailable")
        return await photo_send(bot, method)

    async def fail_text(bot, method):
        if method.chat_id == buyer.user_id:
            raise TimeoutError("test buyer unavailable")
        return await text_send(bot, method)

    monkeypatch.setattr(app.tg, "_m_SendPhoto", fail_photo)
    monkeypatch.setattr(app.tg, "_m_SendMessage", fail_text)
    if interruption == "after_commit":

        async def interrupted(*args, **kwargs):
            raise RuntimeError("test process stopped after activation commit")

        monkeypatch.setattr("bot.handlers.admin.deliver_provisioned_service_card", interrupted)
    mark = buyer.mark()
    await admin.tap("✅ تایید")
    assert (await app.latest_order(buyer)).status == OrderStatus.ACTIVE
    assert buyer.messages_since(mark) == []
    monkeypatch.setattr(app.tg, "_m_SendPhoto", photo_send)
    monkeypatch.setattr(app.tg, "_m_SendMessage", text_send)
    await app.run_job("delivery")
    await app.run_job("delivery")
    assert [m.kind for m in buyer.messages_since(mark)] == ["photo"]
    assert len(app.panel.requests_to("POST /api/user")) == 1
    assert await app.wallet_balance(buyer) == 0
