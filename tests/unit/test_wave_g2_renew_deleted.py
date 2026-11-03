"""Wave G2: renewing a service that was deleted on the panel.

The panel answers 404 to ``get_user``. The order must end PROVISION_FAILED, the
errors topic must say what happened, no new account may appear silently, and a
404 must count as a proven rejection so the money goes back exactly once.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api_client.exceptions import PasarGuardNotFoundError
from bot.handlers.admin import _sanitize_error_detail
from database.models import Order, OrderStatus, PasarGuardAccount, ReportOutbox
from services.finance import is_terminal_wallet_provision_rejection
from services.provisioning import provision_order
from tests.unit.test_audit_provisioning import (  # noqa: F401
    _owned_service,
    _renewal_order,
    session,
)


def _gone() -> PasarGuardNotFoundError:
    return PasarGuardNotFoundError("PasarGuard resource not found", status_code=404)


@pytest.mark.asyncio
async def test_renewing_a_panel_deleted_service_fails_loudly_and_creates_nothing(
    session: AsyncSession,  # noqa: F811
) -> None:
    customer, plan, _account, _first = await _owned_service(session, expires_in=timedelta(days=3))
    renewal = await _renewal_order(session, customer, plan)
    client = AsyncMock()
    client.get_user = AsyncMock(side_effect=_gone())

    with pytest.raises(PasarGuardNotFoundError):
        await provision_order(session, client, order_id=renewal.id, default_group_ids=[1])

    order = await session.get(Order, renewal.id)
    assert order is not None and order.status is OrderStatus.PROVISION_FAILED
    client.create_user.assert_not_called()
    accounts = (await session.scalars(select(PasarGuardAccount))).all()
    assert len(accounts) == 1
    body = (await session.scalars(select(ReportOutbox))).one().body
    assert "سرویس در پنل پیدا نشد" in body
    assert str(renewal.id).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")) in body
    assert str(customer.telegram_id).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")) in body


def test_a_404_is_a_proven_rejection_so_the_customer_is_refunded() -> None:
    assert is_terminal_wallet_provision_rejection(_gone())
    # A plain 5xx stays ambiguous.
    from api_client.exceptions import PasarGuardAPIError

    assert not is_terminal_wallet_provision_rejection(PasarGuardAPIError("x", status_code=500))


def test_the_admin_dm_names_the_missing_service() -> None:
    assert "سرویس در پنل پیدا نشد" in _sanitize_error_detail(_gone())
