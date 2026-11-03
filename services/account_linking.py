"""Explicit admin-confirmed ownership mapping of an existing VPN account.

Panel operations are GET only. Manual/friend accounts are exempt from automatic
lifecycle deletion when first linked; an already linked account is a no-op.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api_client import PasarGuardClient
from bot.config import Settings
from database.base import end_read_transaction
from database.models import PasarGuardAccount, User
from services.access import is_admin
from services.report_events import account_linked
from services.report_outbox import enqueue_report
from services.subscription_identity import reviewed_subscription_matches


class LinkError(ValueError):
    """A safe machine code for the administrator UI."""


@dataclass(frozen=True)
class AccountLinkPreview:
    user_id: int
    telegram_id: int
    panel_user_id: int
    panel_username: str
    subscription_url: str = field(repr=False)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


def _authorize(settings: Settings, actor: int) -> None:
    if not is_admin(actor, settings):
        raise LinkError("admin_required")


def _username(value: str) -> str:
    value = value.strip()
    if value.casefold() == "mirza_sample":
        raise LinkError("infrastructure_account")
    if (
        not value
        or len(value) > 64
        or not value.isprintable()
        or value in {".", ".."}
        or any(c in value for c in "/%:?#\\")
    ):
        raise LinkError("invalid_username")
    return value


async def _release(session: AsyncSession) -> None:
    if not await end_read_transaction(session):
        raise LinkError("uncommitted_changes")


async def _mapping(
    session: AsyncSession, *, user_id: int, panel_user_id: int, panel_username: str
) -> PasarGuardAccount | None:
    rows = list(
        (
            await session.scalars(
                select(PasarGuardAccount)
                .where(
                    or_(
                        PasarGuardAccount.panel_user_id == panel_user_id,
                        PasarGuardAccount.panel_username == panel_username,
                    )
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    if not rows:
        return None
    if len(rows) != 1 or any(row.user_id != user_id for row in rows):
        raise LinkError("other_owner")
    row = rows[0]
    if row.panel_user_id != panel_user_id or row.panel_username != panel_username:
        raise LinkError("identity_conflict")
    if row.deleted_at is not None or row.delete_requested_at is not None:
        raise LinkError("account_archived")
    return row


async def preview_account_link(
    session: AsyncSession,
    client: PasarGuardClient,
    settings: Settings,
    *,
    actor_telegram_id: int,
    user_id: int,
    panel_username: str,
) -> AccountLinkPreview:
    _authorize(settings, actor_telegram_id)
    username = _username(panel_username)
    user = await session.get(User, user_id)
    if user is None:
        raise LinkError("user_not_found")
    telegram_id = user.telegram_id
    await _release(session)
    live = await client.get_user(username)
    url = client.subscription_url_from_response(live)
    if live.username != username or live.id <= 0 or not url:
        raise LinkError("invalid_panel_identity")
    await _mapping(session, user_id=user_id, panel_user_id=live.id, panel_username=username)
    await _release(session)
    return AccountLinkPreview(user_id, telegram_id, live.id, username, url)


async def confirm_account_link(
    session: AsyncSession,
    client: PasarGuardClient,
    settings: Settings,
    *,
    actor_telegram_id: int,
    preview: AccountLinkPreview,
    confirm_sha256: str,
) -> tuple[PasarGuardAccount, bool]:
    _authorize(settings, actor_telegram_id)
    username = _username(preview.panel_username)
    if confirm_sha256 != preview.sha256:
        raise LinkError("confirmation_mismatch")
    await _release(session)
    live = await client.get_user(username)
    if (
        live.id != preview.panel_user_id
        or live.username != username
        or not await reviewed_subscription_matches(client, live, preview.subscription_url)
    ):
        raise LinkError("panel_snapshot_changed")
    # Read ownership again after network IO, inside the write transaction.
    user = await session.get(User, preview.user_id, populate_existing=True)
    if user is None or user.telegram_id != preview.telegram_id:
        raise LinkError("user_changed")
    existing = await _mapping(
        session, user_id=user.id, panel_user_id=live.id, panel_username=username
    )
    if existing is not None:
        return existing, False
    account = PasarGuardAccount(
        user_id=user.id,
        panel_user_id=live.id,
        panel_username=username,
        subscription_url=preview.subscription_url,
        display_name=username,
        delete_exempt=True,
        note=f"manual link by admin {actor_telegram_id}",
    )
    await session.flush()
    try:
        async with session.begin_nested():
            session.add(account)
            await session.flush()
    except IntegrityError:
        existing = await _mapping(
            session, user_id=user.id, panel_user_id=live.id, panel_username=username
        )
        if existing is None:
            raise
        return existing, False
    report = account_linked(
        account_id=account.id,
        user_id=user.id,
        telegram_id=user.telegram_id,
        panel_username=username,
        actor_telegram_id=actor_telegram_id,
    )
    await enqueue_report(
        session,
        event_key=report.event_key,
        topic_key=report.topic_key,
        body=report.body,
        link_user_id=user.id,
    )
    return account, True
