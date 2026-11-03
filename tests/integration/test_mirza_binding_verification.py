"""The CLI verifies a reviewed link before any opening credit is written."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from api_client import UserResponse, UserStatus
from database.models import MirzaBatch, PasarGuardAccount, User, WalletLedgerEntry
from scripts import migrate_mirza
from services.mirza_import import MigrationError, PanelBinding, plan_migration
from tests.integration.test_mirza_import import policy, source


@pytest.mark.parametrize("foreign_token", [False, True])
async def test_apply_verifies_the_old_link_and_preserves_it(
    two_sessions, monkeypatch, foreign_token
):
    factory, url = two_sessions
    reviewed = "https://subs.invalid/sub/reviewed-token"
    rules = replace(
        policy(),
        accept_quarantine=frozenset({"Payment_report:p-1"}),
        panel_bindings=(PanelBinding("old-1", 41, "legacy_exact", reviewed),),
    )
    plan = plan_migration(source(), rules)
    assert not plan.blockers
    reads = []

    class Panel:
        async def get_user(self, name):
            reads.append("user")
            assert name == "legacy_exact"
            return UserResponse(
                id=41,
                username=name,
                status=UserStatus.ACTIVE,
                created_at=datetime.now(UTC),
                used_traffic=0,
                subscription_url="https://subs.invalid/sub/fresh-token",
            )

        def subscription_url_from_response(self, live):
            return live.subscription_url

        async def get_subscription_info(self, token):
            reads.append("subscription_info")
            assert token == "reviewed-token"
            return SimpleNamespace(id=42 if foreign_token else 41, username="legacy_exact")

        async def aclose(self):
            reads.append("closed")

    async def client(_session, _settings):
        return Panel()

    monkeypatch.setattr(migrate_mirza, "get_pasarguard_client", client)
    if foreign_token:
        with pytest.raises(MigrationError, match="panel_snapshot_changed"):
            await migrate_mirza._apply(plan, url, plan.sha256)
        async with factory() as session:
            for model in (User, WalletLedgerEntry, MirzaBatch, PasarGuardAccount):
                assert await session.scalar(select(func.count()).select_from(model)) == 0
    else:
        assert await migrate_mirza._apply(plan, url, plan.sha256) is True
        async with factory() as session:
            assert await session.scalar(select(PasarGuardAccount.subscription_url)) == reviewed
            assert await session.scalar(select(func.count()).select_from(WalletLedgerEntry)) == 1
    assert reads == ["user", "subscription_info", "closed"]
