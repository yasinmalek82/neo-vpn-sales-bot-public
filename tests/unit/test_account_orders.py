"""«Is this service a free trial»: only trial orders reference it."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from database.models import OrderStatus
from services.account_orders import OrderRef, is_trial_by_refs


def ref(trial_kind: str | None, status: OrderStatus = OrderStatus.ACTIVE) -> OrderRef:
    now = datetime.now(UTC)
    return OrderRef(
        id=1,
        user_id=1,
        status=status,
        trial_kind=trial_kind,
        panel_username="x",
        renew_account_id=None,
        created_at=now,
        paid_at=now,
        activated_at=now,
        updated_at=now,
    )


@pytest.mark.parametrize(
    ("refs", "expected"),
    [
        ([], False),  # imported from the old bot: no order, not a trial
        ([ref("personal")], True),
        ([ref("reseller")], True),
        ([ref("personal"), ref("personal", OrderStatus.PROVISION_FAILED)], True),
        ([ref(None)], False),
        ([ref("personal"), ref(None)], False),  # bought into later
        ([ref("personal"), ref(None, OrderStatus.REJECTED)], False),  # any status counts
    ],
)
def test_a_trial_is_a_service_only_trial_orders_reference(
    refs: list[OrderRef], expected: bool
) -> None:
    assert is_trial_by_refs(refs) is expected


def test_r10_a_trial_an_admin_extended_or_gave_volume_is_no_longer_a_trial() -> None:
    assert is_trial_by_refs([ref("personal")], admin_touched=True) is False
    assert is_trial_by_refs([ref("personal")], admin_touched=False) is True
