"""Durable delivery survives interruption without overlapping active senders."""

from datetime import UTC, datetime, timedelta

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import select

from database.models.delivery import DeliveryOutbox
from services.delivery_outbox import enqueue_delivery, flush_deliveries
from tests.e2e.harness import App
from tests.e2e.harness.actors import receipt_png


async def queue_receipts(app: App, count: int = 1) -> None:
    async with app.session() as session:
        for index in range(count):
            app.tg.files[f"receipt_{index}"] = receipt_png()
            await enqueue_delivery(
                session,
                event_key=f"queue_test:{index}",
                kind="receipt",
                chat_id=app.admin(0).user_id,
                payload={
                    "file_id": f"receipt_{index}",
                    "media_type": "photo",
                    "caption": "queued receipt",
                    "markup": {"inline_keyboard": []},
                },
            )
        await session.commit()


async def test_each_claim_gets_a_fresh_lease_in_a_slow_batch(app: App, monkeypatch) -> None:
    import services.delivery_outbox as delivery

    await queue_receipts(app, 4)
    current = datetime.now(UTC) + timedelta(seconds=1)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003 - datetime-compatible clock
            return current

    monkeypatch.setattr(delivery, "datetime", Clock)
    original = app.tg._m_SendPhoto
    overlapped = False

    async def slow_send(bot, method):
        nonlocal current, overlapped
        if method.photo == "receipt_3" and not overlapped:
            overlapped = True
            # Another worker arrives while this last item is actively sending.
            await flush_deliveries(app.session_factory, app.bot, app.settings)
        result = await original(bot, method)
        current += timedelta(seconds=50)
        return result

    monkeypatch.setattr(app.tg, "_m_SendPhoto", slow_send)
    await flush_deliveries(app.session_factory, app.bot, app.settings)
    photos = [m.file_id for m in app.admin(0).messages if m.file_id]
    assert photos.count("receipt_3") == 1
    assert len(photos) == 4


async def test_expired_claim_is_recovered_but_unexpired_claim_is_not(app: App) -> None:
    await queue_receipts(app, 2)
    async with app.session() as session:
        rows = list(
            (await session.scalars(select(DeliveryOutbox).order_by(DeliveryOutbox.id))).all()
        )
        for row in rows:
            row.claim_token = "interrupted_worker"
        rows[0].claimed_until = datetime.now(UTC) - timedelta(seconds=1)
        rows[1].claimed_until = datetime.now(UTC) + timedelta(minutes=1)
        await session.commit()
    assert await flush_deliveries(app.session_factory, app.bot, app.settings) == (1, 0)
    assert await flush_deliveries(app.session_factory, app.bot, app.settings) == (0, 0)
    assert [m.file_id for m in app.admin(0).messages if m.file_id] == ["receipt_0"]


@pytest.mark.parametrize("failure", ["rate_limit", "forbidden", "revoked"])
async def test_permanent_failure_and_retry_after_are_not_retried_immediately(
    app: App, monkeypatch, failure: str
) -> None:
    await queue_receipts(app)

    async def fail_send(bot, method):
        if failure == "rate_limit":
            raise TelegramRetryAfter(method=method, message="test flood limit", retry_after=60)
        raise TelegramForbiddenError(method=method, message="test blocked")

    monkeypatch.setattr(app.tg, "_m_SendPhoto", fail_send)
    if failure == "revoked":
        app.settings.admin_telegram_ids = []
    assert await flush_deliveries(app.session_factory, app.bot, app.settings) == (0, 1)
    assert await flush_deliveries(app.session_factory, app.bot, app.settings) == (0, 0)
    async with app.session() as session:
        row = await session.scalar(select(DeliveryOutbox))
        assert row is not None
        assert row.attempts == 1
        assert row.state == ("pending" if failure == "rate_limit" else "failed")
        assert row.sent_at is None
