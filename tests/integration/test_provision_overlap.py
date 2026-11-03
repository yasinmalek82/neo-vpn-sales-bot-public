"""Foreground and autonomous recovery cannot apply one intent concurrently."""

import pytest

from services.provisioning import ProvisionError, provision_order
from tests.integration.test_provision_snapshot import RacingPanel
from tests.integration.test_sh_durability import _seed_approved_order


async def test_an_overlapping_attempt_is_refused_before_any_panel_call(two_sessions) -> None:
    factory, _ = two_sessions
    order_id = await _seed_approved_order(factory)
    overlap_attempted = False
    busy = False

    async def overlap() -> None:
        nonlocal overlap_attempted, busy
        if overlap_attempted:
            return
        overlap_attempted = True
        async with factory() as second:
            try:
                await provision_order(second, panel, order_id=order_id, default_group_ids=[9])
            except ProvisionError as exc:
                busy = str(exc) == "provision_busy"
            finally:
                await second.rollback()

    panel = RacingPanel(overlap)
    async with factory() as first:
        await provision_order(first, panel, order_id=order_id, default_group_ids=[9])
        await first.commit()
    assert busy, "another attempt must not enter the panel path while this attempt owns it"
    assert panel.created == 1


async def test_claim_covers_caller_commit_and_is_released_after_commit(two_sessions) -> None:
    factory, _ = two_sessions
    order_id = await _seed_approved_order(factory)

    async def idle() -> None:
        pass

    panel = RacingPanel(idle)
    async with factory() as first:
        await provision_order(first, panel, order_id=order_id, default_group_ids=[9])
        async with factory() as second:
            with pytest.raises(ProvisionError, match="provision_busy"):
                await provision_order(second, panel, order_id=order_id, default_group_ids=[9])
        await first.commit()
    async with factory() as later:
        account, _ = await provision_order(later, panel, order_id=order_id, default_group_ids=[9])
        assert account.panel_username in panel.users
        await later.commit()
    assert panel.created == 1


async def test_rollback_releases_claim_and_resume_reads_existing_panel_user(two_sessions) -> None:
    factory, _ = two_sessions
    order_id = await _seed_approved_order(factory)

    async def idle() -> None:
        pass

    panel = RacingPanel(idle)
    async with factory() as first:
        await provision_order(first, panel, order_id=order_id, default_group_ids=[9])
        await first.rollback()
    async with factory() as later:
        await provision_order(later, panel, order_id=order_id, default_group_ids=[9])
        await later.commit()
    assert panel.created == 1
