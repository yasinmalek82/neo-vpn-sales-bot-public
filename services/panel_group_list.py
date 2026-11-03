"""The panel's own groups, as the owner picks them for a store group.

One place for the list: the bot's picker (``bot/handlers/store_admin.py``), the
bot's «📋 لیست گروه‌های پنل» and the Mini App's picker all read it here. Nothing in
this file writes: saving a choice is ``services/store_group_settings.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

from api_client.factory import get_pasarguard_client
from api_client.models import GroupResponse, GroupSimple, GroupsResponse, GroupsSimpleResponse
from bot.texts import texts
from database.base import end_read_transaction

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from bot.config import Settings

logger = logging.getLogger(__name__)

# (id, name, is_disabled): what the bot's picker keeps for each group.
LiveGroup = tuple[int, str, bool]


async def fetch_panel_groups(
    session: AsyncSession,
    settings: Settings,
) -> tuple[list[GroupResponse | GroupSimple], str | None]:
    """The panel's groups with their counts, or ([], short error).

    A key that may read only the simple list (``groups.read_simple`` without
    ``groups.read``: 403 on ``/api/groups``) gets ``GroupSimple`` rows, id and
    name with no counts. Live, both the picker and the list said the list did
    not come for such a key.

    The panel's address and key are read from the database first and the read
    snapshot is let go before the first await on the panel, so a scheduler job
    that commits meanwhile cannot make a later write die with «database is locked».
    """
    from api_client.exceptions import PasarGuardAuthError, PasarGuardError

    client = await get_pasarguard_client(session, settings)
    await end_read_transaction(session)
    try:
        try:
            try:
                resp: GroupsResponse | GroupsSimpleResponse = await client.list_groups(limit=200)
            except PasarGuardAuthError as exc:
                if exc.status_code != 403:  # noqa: PLR2004
                    raise
                resp = await client.list_groups_simple(limit=200)
        except PasarGuardError as exc:
            logger.warning("list_groups failed: %s", type(exc).__name__)
            return [], str(exc)[:200]
        except Exception as exc:
            logger.warning("list_groups unexpected: %s", type(exc).__name__)
            return [], type(exc).__name__
        return [*resp.groups], None
    finally:
        await client.aclose()


async def fetch_live_groups(
    session: AsyncSession,
    settings: Settings,
) -> tuple[list[LiveGroup], str | None]:
    """Return ([(id, name, is_disabled), ...], error_message_or_None)."""
    groups, err = await fetch_panel_groups(session, settings)
    return [(g.id, g.name, bool(getattr(g, "is_disabled", False))) for g in groups], err


async def panel_group_choices(
    session: AsyncSession,
    settings: Settings,
) -> list[LiveGroup] | None:
    """The groups for a picker page, or ``None`` when the panel cannot list them.

    ``None`` covers every way it can fail (no key, no address, a timeout, a
    refusal, an empty list): the bot shows the same «لیست از پنل نیومد» line for all
    of them, and the typed ids stay. A page never sees a client error.
    """
    try:
        groups, err = await fetch_live_groups(session, settings)
    except Exception as exc:
        logger.warning("panel groups unavailable: %s", type(exc).__name__)
        return None
    if err or not groups:
        return None
    return groups


def picker_rows(groups: Iterable[LiveGroup], selected: Iterable[int]) -> list[LiveGroup]:
    """The rows a picker shows: the panel's groups, then every chosen id it no longer lists.

    A chosen id the panel dropped still gets a row (named «در پنل نیست», marked off),
    so it can be un-picked instead of being saved again unseen.
    """
    rows = list(groups)
    listed = {gid for gid, _name, _off in rows}
    rows.extend(
        (gid, texts.admin_store_pick_missing, True) for gid in selected if gid not in listed
    )
    return rows
