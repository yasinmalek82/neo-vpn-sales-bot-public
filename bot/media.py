"""Send stored Telegram file IDs using the media method they belong to.

Telegram photo and document IDs cannot be interchanged, even for image files.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, Message


async def send_stored_media(
    bot: Bot,
    *,
    chat_id: int,
    file_id: str,
    media_type: str,
    caption: str,
    reply_markup: InlineKeyboardMarkup,
) -> Message:
    if media_type == "document":
        return await bot.send_document(
            chat_id=chat_id, document=file_id, caption=caption, reply_markup=reply_markup
        )
    if media_type == "photo":
        return await bot.send_photo(
            chat_id=chat_id, photo=file_id, caption=caption, reply_markup=reply_markup
        )
    raise ValueError("unsupported media type")


async def answer_stored_media(
    message: Message,
    *,
    file_id: str,
    media_type: str,
    caption: str,
    reply_markup: InlineKeyboardMarkup,
) -> Message:
    if media_type == "document":
        return await message.answer_document(
            document=file_id, caption=caption, reply_markup=reply_markup
        )
    if media_type == "photo":
        return await message.answer_photo(photo=file_id, caption=caption, reply_markup=reply_markup)
    raise ValueError("unsupported media type")
