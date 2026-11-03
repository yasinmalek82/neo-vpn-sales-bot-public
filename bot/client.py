"""Shared Telegram client: English numerals in all outgoing text and captions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import Bot
from aiogram.client.default import Default, DefaultBotProperties
from aiogram.client.session.middlewares.base import (
    BaseRequestMiddleware,
    NextRequestMiddlewareType,
)
from aiogram.enums import ParseMode
from aiogram.methods import TelegramMethod
from aiogram.methods.base import Response, TelegramType
from aiogram.types import TelegramObject

from services.ux_format import report_numbers

if TYPE_CHECKING:
    from aiogram.client.session.base import BaseSession

    from bot.config import Settings


def _html_mode(value: object, bot: Bot) -> bool:
    mode = getattr(value, "parse_mode", None)
    if isinstance(mode, Default):
        mode = bot.default[mode.name]
    return mode == ParseMode.HTML


def _caption_copy(media: TelegramObject, bot: Bot) -> TelegramObject:
    caption = getattr(media, "caption", None)
    return (
        media.model_copy(
            update={"caption": report_numbers(caption, preserve_html=_html_mode(media, bot))}
        )
        if isinstance(caption, str)
        else media
    )


class ReportNumbersMiddleware(BaseRequestMiddleware):
    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        fields: dict[str, Any] = {}
        for name in ("text", "caption"):
            value = getattr(method, name, None)
            if isinstance(value, str):
                fields[name] = report_numbers(value, preserve_html=_html_mode(method, bot))
        media = getattr(method, "media", None)
        if isinstance(media, list):
            fields["media"] = [_caption_copy(item, bot) for item in media]
        elif isinstance(media, TelegramObject):
            fields["media"] = _caption_copy(media, bot)
        outgoing = method.model_copy(update=fields) if fields else method
        return await make_request(bot, outgoing)


def configure_report_numbers(bot: Bot) -> None:
    """Also used by scoped live-test clients that subclass Bot."""
    if not any(isinstance(item, ReportNumbersMiddleware) for item in bot.session.middleware):
        bot.session.middleware(ReportNumbersMiddleware())


def create_bot(settings: Settings, *, session: BaseSession | None = None) -> Bot:
    bot = Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    configure_report_numbers(bot)
    return bot
