"""Historical amounts keep their source unit and never affect the live ledger."""

from dataclasses import replace

from sqlalchemy import select

from database.models import MirzaBatch, User, WalletLedgerEntry
from services.mirza_history import history_page
from services.mirza_import import apply_migration, plan_migration
from tests.integration.test_mirza_import import policy, source


async def imported(factory, *, unit="toman"):
    plan = plan_migration(source(), replace(policy(), source_unit=unit))
    async with factory() as session:
        await apply_migration(session, plan, confirm_sha256=plan.sha256)
        await session.commit()
        return (await session.scalar(select(User).where(User.telegram_id == 1001))).id


async def test_history_converts_rial_exactly_and_keeps_unpaid_review_state(two_sessions):
    factory, _ = two_sessions
    user_id = await imported(factory, unit="rial")
    async with factory() as session:
        batch = await session.get(MirzaBatch, "mirza-cutover")
        assert batch.source_unit == "rial"
        page = await history_page(session, user_id=user_id)
        assert page.total == 3
        invoice = next(row for row in page.rows if row.source_table == "invoice")
        assert invoice.amount_toman == 9000
        assert invoice.disposition == "quarantined"
        payment = next(row for row in page.rows if row.source_table == "Payment_report")
        assert payment.amount_toman == 5000
        assert payment.status == "Unpaid"
        review = await history_page(session, user_id=None, review_only=True)
        assert review.total == 2
        assert all(row.disposition == "quarantined" for row in review.rows)
        assert len(list((await session.scalars(select(WalletLedgerEntry))).all())) == 1
        assert (
            await session.scalar(select(User.wallet_balance_toman).where(User.id == user_id))
            == 12500
        )


async def test_old_batch_with_unknown_unit_never_claims_a_toman_amount(two_sessions):
    factory, _ = two_sessions
    user_id = await imported(factory)
    async with factory() as session:
        batch = await session.get(MirzaBatch, "mirza-cutover")
        batch.source_unit = None
        await session.commit()
        page = await history_page(session, user_id=user_id)
        assert page.total == 3
        assert all(row.amount_toman is None for row in page.rows)
        assert all(row.source_unit is None for row in page.rows)
        assert (
            await session.scalar(select(User.wallet_balance_toman).where(User.id == user_id))
            == 125000
        )


async def test_customer_history_cannot_include_another_customers_records(two_sessions):
    factory, _ = two_sessions
    user_id = await imported(factory)
    async with factory() as session:
        first = await history_page(session, user_id=user_id)
        other_id = await session.scalar(select(User.id).where(User.telegram_id == 1002))
        other = await history_page(session, user_id=other_id)
        assert other.total == 2
        assert all(row.user_id == other_id for row in other.rows)
        assert {(row.namespace, row.source_table, row.source_key) for row in other.rows}.isdisjoint(
            {(row.namespace, row.source_table, row.source_key) for row in first.rows}
        )
        beyond = await history_page(session, user_id=other_id, offset=100000)
        assert beyond.offset == 0 and len(beyond.rows) == 2


async def test_rerun_refuses_currency_metadata_that_conflicts_with_its_reviewed_plan(two_sessions):
    import pytest

    from services.mirza_import import MigrationError

    factory, _ = two_sessions
    await imported(factory, unit="toman")
    plan = plan_migration(source(), policy())
    async with factory() as session:
        batch = await session.get(MirzaBatch, "mirza-cutover")
        batch.source_unit = "rial"
        await session.commit()
        with pytest.raises(MigrationError, match="source_unit_conflict"):
            await apply_migration(session, plan, confirm_sha256=plan.sha256)
        await session.rollback()
