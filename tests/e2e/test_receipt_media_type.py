"""Receipt file IDs retain their Telegram media type after fresh DB reads."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from database.models import OrderStatus, Payment, WalletLedgerEntry
from tests.e2e.harness import App, order_by_card, publish_store
from tests.e2e.harness.actors import receipt_png


@pytest.fixture
async def strict_receipt_media(app: App, monkeypatch: pytest.MonkeyPatch):
    document_ids: set[str] = set()
    send_photo = app.tg._m_SendPhoto
    send_document = app.tg._m_SendDocument

    async def photo(bot, method):
        if isinstance(method.photo, str) and method.photo in document_ids:
            raise app.tg._reject(
                "wrong_media", method, "document ID cannot be sent as photo", method.chat_id
            )
        return await send_photo(bot, method)

    async def document(bot, method):
        if isinstance(method.document, str) and method.document not in document_ids:
            raise app.tg._reject(
                "wrong_media", method, "photo ID cannot be sent as document", method.chat_id
            )
        return await send_document(bot, method)

    monkeypatch.setattr(app.tg, "_m_SendPhoto", photo)
    monkeypatch.setattr(app.tg, "_m_SendDocument", document)
    return document_ids


@pytest.mark.parametrize("kind", ["photo", "document"])
async def test_receipt_media_survives_fresh_session_and_admin_reopen(
    app: App, strict_receipt_media, kind: str
):
    store = await publish_store(app)
    buyer = app.customer("ali")
    await buyer.start()
    order = await order_by_card(buyer, store)
    if kind == "photo":
        file_id = await buyer.send_photo()
    else:
        file_id = app.tg.register_file(receipt_png())
        strict_receipt_media.add(file_id)
        await buyer._send_message(
            document={
                "file_id": file_id,
                "file_unique_id": "receipt-doc",
                "mime_type": "image/png",
                "file_name": "receipt.png",
            }
        )
    for index in range(2):
        received = [m for m in app.admin(index).messages if m.file_id == file_id]
        assert len(received) == 1, "each admin receives the actual receipt"
        assert received[0].kind == kind
    async with app.session() as session:
        stored = await session.scalar(select(Payment).where(Payment.order_id == order.id))
        assert stored.receipt_file_id == file_id
        assert stored.receipt_media_type == kind
        assert await session.scalar(select(func.count()).select_from(Payment)) == 1
        assert await session.scalar(select(func.count()).select_from(WalletLedgerEntry)) == 0
    admin = app.admin()
    await admin.send("/admin")
    await admin.tap("پرداخت‌ها")
    await admin.tap(f"#{order.id}")
    assert admin.last_message.kind == kind
    assert admin.last_message.file_id == file_id
    await admin.tap("نمایش مجدد")
    assert admin.last_message.kind == kind
    assert admin.last_message.file_id == file_id
    await admin.tap("✅ تایید")
    assert (await app.latest_order(buyer)).status == OrderStatus.ACTIVE
    assert len(app.panel.users) == 1
    await admin.tap("✅ تایید")
    assert len(app.panel.users) == 1
