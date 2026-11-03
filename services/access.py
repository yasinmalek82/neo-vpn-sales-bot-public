"""Who counts as an admin, in one place.

The bot's handlers and the Mini App panel (`web/`) ask the same question. It
used to be written twice, with the same body, in `bot/handlers/common.py` and
`bot/handlers/admin.py`; a web copy would have been a third, and a list that
disagrees between the bot and the panel is a hole.
"""

from __future__ import annotations

from bot.config import Settings
from database.models import User

__all__ = ["is_admin"]


def is_admin(
    telegram_id: int | None,
    settings: Settings,
    db_user: User | None = None,
) -> bool:
    """Loaded configuration owns admin authority; the DB flag is only a cache.

    Web requests do not pass through the bot middleware that synchronizes the
    flag. Trusting it would retain a removed administrator until their next bot
    interaction (and indefinitely if that synchronization fails).
    """
    _ = db_user  # retained for callers; a cached flag cannot grant privileges
    return telegram_id is not None and telegram_id in settings.admin_telegram_ids
