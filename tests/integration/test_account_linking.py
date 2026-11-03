"""An explicit admin links only the selected owner, with no VPN/financial write."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from api_client import UserResponse, UserStatus
from bot.config import Settings
from database.models import Order, PasarGuardAccount, Payment, ReportOutbox, User, WalletLedgerEntry
from services.account_linking import LinkError, confirm_account_link, preview_account_link


class ReadOnlyPanel:
    def __init__(self):
        self.user = UserResponse(
            id=41,
            username="friend_exact",
            created_at=datetime.now(UTC),
            status=UserStatus.ACTIVE,
            used_traffic=1234,
            data_limit=10000,
            expire=1900000000,
            hwid_limit=0,
            subscription_url="https://panel.invalid/sub/existing",
        )

    async def get_user(self, username):
        assert username == self.user.username
        return self.user.model_copy(deep=True)

    def subscription_url_from_response(self, user):
        return user.subscription_url


async def seed(factory):
    async with factory() as session:
        first = User(telegram_id=1001, referral_code="link-first", wallet_balance_toman=123)
        other = User(telegram_id=1002, referral_code="link-other", wallet_balance_toman=456)
        session.add_all([first, other])
        await session.commit()
        return first.id, other.id


def settings():
    return Settings(_env_file=None, BOT_TOKEN="test-token", ADMIN_TELEGRAM_IDS=[9001])


async def test_confirm_links_exact_account_once_without_panel_or_money_change(two_sessions):
    factory, _ = two_sessions
    user_id, _ = await seed(factory)
    panel = ReadOnlyPanel()
    original = panel.user.model_dump()
    async with factory() as session:
        preview = await preview_account_link(
            session,
            panel,
            settings(),
            actor_telegram_id=9001,
            user_id=user_id,
            panel_username="friend_exact",
        )
        assert await session.scalar(select(func.count()).select_from(PasarGuardAccount)) == 0
        account, created = await confirm_account_link(
            session,
            panel,
            settings(),
            actor_telegram_id=9001,
            preview=preview,
            confirm_sha256=preview.sha256,
        )
        assert created is True
        assert account.panel_username == "friend_exact" and account.panel_user_id == 41
        assert account.delete_exempt is True
        await session.commit()
    async with factory() as session:
        _, created = await confirm_account_link(
            session,
            panel,
            settings(),
            actor_telegram_id=9001,
            preview=preview,
            confirm_sha256=preview.sha256,
        )
        assert created is False
        await session.commit()
        assert await session.scalar(select(func.count()).select_from(PasarGuardAccount)) == 1
        assert await session.scalar(select(func.count()).select_from(ReportOutbox)) == 1
        for model in (Order, Payment, WalletLedgerEntry):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
        assert (
            await session.scalar(select(User.wallet_balance_toman).where(User.id == user_id)) == 123
        )
    assert panel.user.model_dump() == original


async def test_a_freshly_minted_url_can_preserve_the_still_valid_reviewed_link(two_sessions):
    factory, _ = two_sessions
    user_id, _ = await seed(factory)
    panel = ReadOnlyPanel()
    calls = []

    async def subscription_info(token):
        calls.append(token)
        return SimpleNamespace(id=41, username="friend_exact")

    panel.get_subscription_info = subscription_info
    async with factory() as session:
        preview = await preview_account_link(
            session,
            panel,
            settings(),
            actor_telegram_id=9001,
            user_id=user_id,
            panel_username="friend_exact",
        )
        panel.user.subscription_url = "https://panel.invalid/sub/fresh-token"
        account, created = await confirm_account_link(
            session,
            panel,
            settings(),
            actor_telegram_id=9001,
            preview=preview,
            confirm_sha256=preview.sha256,
        )
        assert created and account.subscription_url == preview.subscription_url
        assert calls == ["existing"]
        await session.commit()


async def test_ownership_changed_after_preview_is_refused(two_sessions):
    factory, _ = two_sessions
    user_id, other_id = await seed(factory)
    panel = ReadOnlyPanel()
    async with factory() as session:
        preview = await preview_account_link(
            session,
            panel,
            settings(),
            actor_telegram_id=9001,
            user_id=user_id,
            panel_username="friend_exact",
        )
    async with factory() as other:
        other.add(
            PasarGuardAccount(user_id=other_id, panel_user_id=41, panel_username="friend_exact")
        )
        await other.commit()
    async with factory() as session:
        with pytest.raises(LinkError, match="other_owner"):
            await confirm_account_link(
                session,
                panel,
                settings(),
                actor_telegram_id=9001,
                preview=preview,
                confirm_sha256=preview.sha256,
            )
        await session.rollback()
        assert await session.scalar(select(PasarGuardAccount.user_id)) == other_id


@pytest.mark.parametrize(
    "case", ["not_admin", "infrastructure", "different_identity", "wrong_confirmation"]
)
async def test_invalid_link_cannot_create_a_mapping(two_sessions, case):
    factory, _ = two_sessions
    user_id, _ = await seed(factory)
    panel = ReadOnlyPanel()
    async with factory() as session:
        if case in {"not_admin", "infrastructure"}:
            username = "mirza_sample" if case == "infrastructure" else "friend_exact"
            with pytest.raises(LinkError):
                await preview_account_link(
                    session,
                    panel,
                    settings(),
                    actor_telegram_id=1 if case == "not_admin" else 9001,
                    user_id=user_id,
                    panel_username=username,
                )
        else:
            preview = await preview_account_link(
                session,
                panel,
                settings(),
                actor_telegram_id=9001,
                user_id=user_id,
                panel_username="friend_exact",
            )
            panel.user.id = 42 if case == "different_identity" else 41
            with pytest.raises(LinkError):
                await confirm_account_link(
                    session,
                    panel,
                    settings(),
                    actor_telegram_id=9001,
                    preview=preview,
                    confirm_sha256="0" * 64 if case == "wrong_confirmation" else preview.sha256,
                )
        await session.rollback()
        assert await session.scalar(select(func.count()).select_from(PasarGuardAccount)) == 0


async def test_confirmation_rereads_a_mapping_already_cached_by_this_session(two_sessions):
    factory, _ = two_sessions
    user_id, other_id = await seed(factory)
    panel = ReadOnlyPanel()
    async with factory() as session:
        held = PasarGuardAccount(user_id=user_id, panel_user_id=41, panel_username="friend_exact")
        session.add(held)
        await session.commit()
        preview = await preview_account_link(
            session,
            panel,
            settings(),
            actor_telegram_id=9001,
            user_id=user_id,
            panel_username="friend_exact",
        )
        async with factory() as other:
            moved = await other.get(PasarGuardAccount, held.id)
            moved.user_id = other_id
            await other.commit()
        with pytest.raises(LinkError, match="other_owner"):
            await confirm_account_link(
                session,
                panel,
                settings(),
                actor_telegram_id=9001,
                preview=preview,
                confirm_sha256=preview.sha256,
            )
        await session.rollback()


@pytest.mark.parametrize("state", ["deleted_at", "delete_requested_at"])
async def test_a_tombstoned_mapping_cannot_be_silently_reactivated(two_sessions, state):
    factory, _ = two_sessions
    user_id, _ = await seed(factory)
    panel = ReadOnlyPanel()
    async with factory() as session:
        row = PasarGuardAccount(user_id=user_id, panel_user_id=41, panel_username="friend_exact")
        setattr(row, state, datetime.now(UTC))
        session.add(row)
        await session.commit()
        with pytest.raises(LinkError, match="account_archived"):
            await preview_account_link(
                session,
                panel,
                settings(),
                actor_telegram_id=9001,
                user_id=user_id,
                panel_username="friend_exact",
            )
        await session.rollback()
