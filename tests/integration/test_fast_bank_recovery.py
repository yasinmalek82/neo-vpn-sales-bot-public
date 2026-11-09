"""Fresh bank recovery with independent DB sessions and a simulated panel."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from sqlalchemy import select

from api_client.exceptions import PasarGuardNotFoundError
from database.models import Order, OrderStatus, Payment, WalletLedgerEntry
from database.models.provision_recovery import ProvisionRecovery
from services.bank_sms import receive_bank_sms
from services.provision_recovery import recover_pending_provisions
from tests.integration.test_bank_sms import NOW, accept, config, reserve, seed, sms
from tests.integration.test_sh_durability import _panel_user, _seed_approved_order


async def _committed_bank_order(factory):
    _customer, _admin, order_id = await seed(factory)
    invoice = await reserve(factory, order_id)
    assert (await accept(factory, invoice.amount_toman)).outcome == "approved"
    return order_id


def _panel():
    client = AsyncMock()
    client.get_user.side_effect = PasarGuardNotFoundError("missing", status_code=404)

    async def create_user(payload):
        return _panel_user(str(payload.username))

    client.create_user.side_effect = create_user
    client.subscription_url_from_response = lambda user: user.subscription_url
    return client


async def test_uncommitted_bank_confirmation_is_not_visible_to_the_worker(
    two_sessions, monkeypatch
):
    import services.provision_recovery as recovery

    factory, _url = two_sessions
    _customer, _admin, order_id = await seed(factory)
    invoice = await reserve(factory, order_id)
    panel = _panel()
    monkeypatch.setattr(recovery, "get_pasarguard_client", AsyncMock(return_value=panel))

    async with factory() as session:
        result = await receive_bank_sms(
            session, config(), text=sms(invoice.amount_toman), sender="BluBank", now=NOW
        )
        assert result.outcome == "approved"
        assert await recover_pending_provisions(factory, config()) == (0, 0)
        panel.get_user.assert_not_awaited()
        await session.commit()
    assert await recover_pending_provisions(factory, config()) == (1, 0)
    assert panel.create_user.await_count == 1


async def test_two_workers_cannot_create_two_services_for_a_fresh_bank_approval(
    two_sessions, monkeypatch
):
    import services.provision_recovery as recovery

    factory, _url = two_sessions
    order_id = await _committed_bank_order(factory)
    panel = _panel()
    monkeypatch.setattr(recovery, "get_pasarguard_client", AsyncMock(return_value=panel))

    results = await asyncio.gather(
        recover_pending_provisions(factory, config()),
        recover_pending_provisions(factory, config()),
    )
    assert sum(done for done, _failed in results) == 1
    assert sum(failed for _done, failed in results) == 0
    assert panel.create_user.await_count == 1
    async with factory() as session:
        order = await session.get(Order, order_id)
        assert order is not None and order.status == OrderStatus.ACTIVE
        assert (
            len((await session.scalars(select(Payment).where(Payment.order_id == order_id))).all())
            == 1
        )
        suffix = select(WalletLedgerEntry).where(
            WalletLedgerEntry.order_id == order_id,
            WalletLedgerEntry.reason == "bank_payment_suffix",
        )
        assert len((await session.scalars(suffix)).all()) == 1


async def test_fresh_bank_approval_still_respects_retry_backoff(two_sessions, monkeypatch):
    import services.provision_recovery as recovery

    factory, _url = two_sessions
    order_id = await _committed_bank_order(factory)
    open_panel = AsyncMock(side_effect=TimeoutError("simulated panel outage"))
    monkeypatch.setattr(recovery, "get_pasarguard_client", open_panel)

    assert await recover_pending_provisions(factory, config()) == (0, 1)
    assert await recover_pending_provisions(factory, config()) == (0, 0)
    assert open_panel.await_count == 1
    async with factory() as session:
        order = await session.get(Order, order_id)
        retry = await session.get(ProvisionRecovery, order_id)
        assert order is not None and order.status == OrderStatus.PAYMENT_APPROVED
        assert retry is not None and retry.attempts == 1


async def test_manual_approval_keeps_the_existing_one_minute_guard(two_sessions, monkeypatch):
    import services.provision_recovery as recovery

    factory, _url = two_sessions
    order_id = await _seed_approved_order(factory)
    panel = _panel()
    monkeypatch.setattr(recovery, "get_pasarguard_client", AsyncMock(return_value=panel))

    assert await recover_pending_provisions(factory, config()) == (0, 0)
    panel.get_user.assert_not_awaited()
    async with factory() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        order.updated_at = datetime.now(UTC) - timedelta(minutes=2)
        await session.commit()
    assert await recover_pending_provisions(factory, config()) == (1, 0)
    assert panel.create_user.await_count == 1
