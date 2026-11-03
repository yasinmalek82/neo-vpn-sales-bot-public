"""Migration opens balances once and archives history without replaying sales."""

from copy import deepcopy
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from database.models import Order, PasarGuardAccount, Payment, ReportOutbox, User, WalletLedgerEntry
from services.mirza_import import MigrationError, MigrationPolicy, apply_migration, plan_migration
from services.mirza_source import MirzaDump


def source() -> MirzaDump:
    return MirzaDump(
        "a" * 64,
        {
            "user": [
                {
                    "id": "1001",
                    "Balance": "125000",
                    "username": "ali_user",
                    "agent": "f",
                    "User_Status": "Active",
                    "limit_usertest": "1",
                },
                {
                    "id": "1002",
                    "Balance": "0",
                    "username": "shop_user",
                    "agent": "n",
                    "User_Status": "block",
                    "limit_usertest": "1",
                },
            ],
            "invoice": [
                {
                    "id_invoice": "old-1",
                    "id_user": "1001",
                    "username": "legacy_exact",
                    "Status": "active",
                    "price_product": "90000",
                    "Service_location": "gone",
                },
                {
                    "id_invoice": "old-2",
                    "id_user": "1002",
                    "username": "shop_exact",
                    "Status": "active",
                    "price_product": "80000",
                    "Service_location": "old",
                },
            ],
            "Payment_report": [
                {"id": "p-1", "id_user": "1001", "price": "50000", "payment_Status": "Unpaid"}
            ],
            "service_other": [],
        },
    )


def policy() -> MigrationPolicy:
    return MigrationPolicy(
        namespace="mirza-cutover",
        source_unit="toman",
        archive_invoice_ids=frozenset({"old-2"}),
        accept_quarantine=frozenset({"invoice:old-1", "Payment_report:p-1"}),
    )


async def test_import_twice_keeps_one_opening_credit_and_all_history(two_sessions):
    from database.models.mirza_import import MirzaRecord

    factory, _ = two_sessions
    plan = plan_migration(source(), policy())
    assert plan.opening_total_toman == 125000
    async with factory() as session:
        assert await apply_migration(session, plan, confirm_sha256=plan.sha256) is True
        await session.commit()
    async with factory() as session:
        assert await apply_migration(session, plan, confirm_sha256=plan.sha256) is False
        await session.commit()
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 2
        entries = list((await session.scalars(select(WalletLedgerEntry))).all())
        assert len(entries) == 1 and entries[0].amount_toman == 125000
        assert (
            await session.scalar(select(User.wallet_balance_toman).where(User.telegram_id == 1001))
            == 125000
        )
        shop = await session.scalar(select(User).where(User.telegram_id == 1002))
        assert shop.is_reseller is True and shop.is_banned is True
        assert await session.scalar(select(func.count()).select_from(MirzaRecord)) == 5
        archived = await session.get(MirzaRecord, ("mirza-cutover", "invoice", "old-2"))
        assert archived.disposition == "archived"
        assert archived.payload["username"] == "shop_exact"
        assert await session.scalar(select(func.count()).select_from(Order)) == 0
        assert await session.scalar(select(func.count()).select_from(Payment)) == 0
        assert await session.scalar(select(func.count()).select_from(PasarGuardAccount)) == 0
        assert await session.scalar(select(func.count()).select_from(ReportOutbox)) == 1


async def test_failure_rolls_back_opening_money_and_source_maps(two_sessions):
    from database.models.mirza_import import MirzaBatch, MirzaRecord

    factory, _ = two_sessions
    plan = plan_migration(source(), policy())
    async with factory() as session:
        await apply_migration(session, plan, confirm_sha256=plan.sha256)
        await session.rollback()
    async with factory() as check:
        for model in (User, WalletLedgerEntry, MirzaRecord, MirzaBatch, ReportOutbox):
            assert await check.scalar(select(func.count()).select_from(model)) == 0


async def test_existing_customer_is_not_credited_or_overwritten_without_reconciliation(
    two_sessions,
):
    factory, _ = two_sessions
    async with factory() as session:
        session.add(
            User(
                telegram_id=1001,
                referral_code="existing",
                wallet_balance_toman=77,
                is_reseller=True,
            )
        )
        await session.commit()
    plan = plan_migration(source(), policy())
    async with factory() as session:
        with pytest.raises(MigrationError, match="target_user_exists"):
            await apply_migration(session, plan, confirm_sha256=plan.sha256)
        await session.rollback()
    async with factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == 1001))
        assert user.wallet_balance_toman == 77 and user.is_reseller is True
        assert await session.scalar(select(func.count()).select_from(WalletLedgerEntry)) == 0


def test_review_is_exact_and_no_unpaid_payment_is_silently_credited():
    incomplete = MigrationPolicy(namespace="mirza-cutover", source_unit="toman")
    plan = plan_migration(source(), incomplete)
    assert set(plan.blockers) == {
        "review:invoice:old-1",
        "review:invoice:old-2",
        "review:Payment_report:p-1",
    }
    assert plan.opening_total_toman == 125000


@pytest.mark.parametrize("change", ["unit", "invalid_balance", "duplicate", "unknown_role"])
def test_ambiguous_or_invalid_input_blocks_apply(change):
    raw = deepcopy(source().tables)
    rules = policy()
    if change == "unit":
        rules = MigrationPolicy(namespace="mirza-cutover", source_unit=None)
    elif change == "invalid_balance":
        raw["user"][0]["Balance"] = "125000.5"
    elif change == "duplicate":
        raw["user"].append(dict(raw["user"][0]))
    else:
        raw["user"][0]["agent"] = "n2"
    assert plan_migration(MirzaDump("b" * 64, raw), rules).blockers


async def test_preview_digest_and_source_binding_are_enforced(two_sessions):
    factory, _ = two_sessions
    plan = plan_migration(source(), policy())
    async with factory() as session:
        with pytest.raises(MigrationError, match="confirmation_mismatch"):
            await apply_migration(session, plan, confirm_sha256="0" * 64)
        await apply_migration(session, plan, confirm_sha256=plan.sha256)
        await session.commit()
    changed = plan_migration(MirzaDump("b" * 64, deepcopy(source().tables)), policy())
    async with factory() as session:
        with pytest.raises(MigrationError, match="namespace_already_bound"):
            await apply_migration(session, changed, confirm_sha256=changed.sha256)


async def test_reviewed_panel_identity_is_mapped_once_with_exact_name(two_sessions):
    from services.mirza_import import PanelBinding

    factory, _ = two_sessions
    binding = PanelBinding(
        invoice_id="old-1",
        panel_user_id=41,
        panel_username="legacy_exact",
        subscription_url="https://panel.invalid/sub/unchanged",
        delete_exempt=True,
    )
    rules = MigrationPolicy(
        namespace="mirza-cutover",
        source_unit="toman",
        archive_invoice_ids=frozenset({"old-2"}),
        accept_quarantine=frozenset({"Payment_report:p-1"}),
        panel_bindings=(binding,),
    )
    plan = plan_migration(source(), rules)
    assert plan.blockers == ()
    async with factory() as session:
        await apply_migration(session, plan, confirm_sha256=plan.sha256)
        await session.commit()
    async with factory() as session:
        await apply_migration(session, plan, confirm_sha256=plan.sha256)
        await session.commit()
        account = await session.scalar(select(PasarGuardAccount))
        owner = await session.get(User, account.user_id)
        assert owner.telegram_id == 1001
        assert account.panel_user_id == 41 and account.panel_username == "legacy_exact"
        assert account.subscription_url == "https://panel.invalid/sub/unchanged"
        assert account.delete_exempt is True
        assert await session.scalar(select(func.count()).select_from(PasarGuardAccount)) == 1


def test_a_binding_cannot_rename_the_source_service_or_capture_infrastructure():
    from services.mirza_import import PanelBinding

    for username in ("other_name", "mirza_sample"):
        rules = MigrationPolicy(
            namespace="mirza-cutover",
            source_unit="toman",
            panel_bindings=(
                PanelBinding(invoice_id="old-1", panel_user_id=41, panel_username=username),
            ),
        )
        assert plan_migration(source(), rules).blockers


@pytest.mark.parametrize("deleted", [False, True])
async def test_import_cannot_take_a_panel_identity_from_another_owner(two_sessions, deleted):
    from services.mirza_import import PanelBinding

    factory, _ = two_sessions
    async with factory() as session:
        owner = User(telegram_id=8888, referral_code="other-owner", wallet_balance_toman=91)
        session.add(owner)
        await session.flush()
        session.add(
            PasarGuardAccount(
                user_id=owner.id,
                panel_user_id=41,
                panel_username="legacy_exact",
                subscription_url="https://panel.invalid/sub/other",
                deleted_at=datetime.now(UTC) if deleted else None,
            )
        )
        await session.commit()
    rules = MigrationPolicy(
        namespace="mirza-cutover",
        source_unit="toman",
        archive_invoice_ids=frozenset({"old-2"}),
        accept_quarantine=frozenset({"Payment_report:p-1"}),
        panel_bindings=(
            PanelBinding(invoice_id="old-1", panel_user_id=41, panel_username="legacy_exact"),
        ),
    )
    plan = plan_migration(source(), rules)
    async with factory() as session:
        with pytest.raises(MigrationError, match="account_owner_conflict"):
            await apply_migration(session, plan, confirm_sha256=plan.sha256)
        await session.rollback()
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 1
        assert await session.scalar(select(func.count()).select_from(WalletLedgerEntry)) == 0
        account = await session.scalar(select(PasarGuardAccount))
        owner = await session.get(User, account.user_id)
        assert owner.telegram_id == 8888
        assert account.subscription_url == "https://panel.invalid/sub/other"


async def test_old_trial_entitlement_is_preserved_and_admin_reset_can_release_it(two_sessions):
    from services.customer_lookup import get_customer_card, reset_trial
    from services.trial import TrialOffer, trial_open_for

    factory, _ = two_sessions
    raw = deepcopy(source().tables)
    raw["user"][0]["limit_usertest"] = "0"
    plan = plan_migration(MirzaDump("c" * 64, raw), policy())
    async with factory() as session:
        await apply_migration(session, plan, confirm_sha256=plan.sha256)
        await session.commit()
        user = await session.scalar(select(User).where(User.telegram_id == 1001))
        direct = TrialOffer(
            slug="direct",
            title_fa="مستقیم",
            data_limit_gb=1,
            duration_days=1,
            group_ids=[1],
            counts_legacy=True,
        )
        tunnel = TrialOffer(
            slug="tunnel",
            title_fa="تونل",
            data_limit_gb=1,
            duration_days=1,
            group_ids=[2],
            counts_legacy=False,
        )
        assert (
            user.trial_claimed_at is None
        ), "the source counter does not establish a historical date"
        assert await trial_open_for(session, user, direct) is False
        assert await trial_open_for(session, user, tunnel) is True
        card = await get_customer_card(session, user.id)
        assert card.has_taken_trial is True
        await reset_trial(session, user.id)
        await session.commit()
        assert await trial_open_for(session, user, direct) is True
