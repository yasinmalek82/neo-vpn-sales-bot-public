"""The owner requires ASCII numerals in every delivered bot report body/caption."""

from __future__ import annotations

import pytest
from aiogram.methods import EditMessageMedia, SendMediaGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from aiogram.types.input_file import BufferedInputFile

from bot.client import create_bot
from tests.e2e.harness.actors import receipt_png
from tests.e2e.harness.settings import make_settings
from tests.e2e.harness.telegram import FakeTelegram


@pytest.fixture
async def client(tmp_path):
    session = FakeTelegram()
    session.add_private_chat(1001)
    bot = create_bot(make_settings("sqlite+aiosqlite:///:memory:", tmp_path), session=session)
    try:
        yield bot, session
    finally:
        await bot.session.close()


async def test_text_and_edits_have_ascii_amounts_ids_dates_and_counts(client):
    bot, tg = client
    message = await bot.send_message(1001, "سفارش ۱۲ • ۲۲۵٬۰۰۰ تومان • ۱۴۰۵/۰۸/۱۰ • ٣ کاربر")
    assert message.text == "سفارش 12 • 225,000 تومان • 1405/08/10 • 3 کاربر"
    await bot.edit_message_text(
        "مانده ۷۵٬۰۰۰؛ ۲ سفارش", chat_id=1001, message_id=message.message_id
    )
    assert tg.calls[-1].params["text"] == "مانده 75,000؛ 2 سفارش"


async def test_photo_document_and_edited_captions_use_same_policy(client):
    bot, tg = client
    photo = await bot.send_photo(
        1001,
        BufferedInputFile(receipt_png(), filename="test.png"),
        caption="رسید ۱۲ • ۱۰٬۰۰۰ تومان",
    )
    assert photo.caption == "رسید 12 • 10,000 تومان"
    doc = await bot.send_document(
        1001,
        BufferedInputFile(receipt_png(), filename="test.png"),
        caption="بکاپ ۲ فایل؛ ۱٫۵ مگابایت",
    )
    assert doc.caption == "بکاپ 2 فایل؛ 1.5 مگابایت"
    await bot.edit_message_caption(chat_id=1001, message_id=photo.message_id, caption="تأیید ۱ بار")
    assert tg.calls[-1].params["caption"] == "تأیید 1 بار"


async def test_normalization_preserves_markup_opaque_links_and_button_identity(client):
    bot, tg = client
    url = "https://panel.test/sub/opaque۱۲?signature=٣۴"
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="سفارش ۱۲", callback_data="order:12:confirm")]]
    )
    message = await bot.send_message(
        1001, f'<b>سفارش ۱۲</b> <a href="{url}">مشاهده ۲</a> {url}', reply_markup=markup
    )
    assert tg.calls[-1].params["text"] == f'<b>سفارش 12</b> <a href="{url}">مشاهده 2</a> {url}'
    assert message.text == f"سفارش 12 مشاهده 2 {url}"
    assert tg.calls[-1].params["reply_markup"] == markup
    assert markup.inline_keyboard[0][0].callback_data == "order:12:confirm"
    assert markup.inline_keyboard[0][0].text == "سفارش ۱۲"


async def test_original_request_and_entity_offsets_are_not_mutated(client):
    from aiogram.methods import SendMessage
    from aiogram.types import MessageEntity

    bot, tg = client
    entity = MessageEntity(type="bold", offset=0, length=2)
    method = SendMessage(chat_id=1001, text="۱۲", entities=[entity], parse_mode=None)
    result = await bot(method)
    assert result.text == "12"
    assert method.text == "۱۲" and method.entities == [entity]
    assert tg.calls[-1].params["entities"] == [entity]


@pytest.mark.parametrize("method_type", [SendMediaGroup, EditMessageMedia])
async def test_nested_media_captions_are_normalized_without_rewriting_file_id(client, method_type):
    bot, tg = client
    media = InputMediaPhoto(media="opaque_file_12", caption="فایل ۱۲")
    # Inspect the real session boundary without requiring FakeTelegram to invent
    # otherwise unused album/edit-media API behavior.
    captured = []

    async def transport(_bot, method, timeout=None):
        captured.append(method)
        return True

    tg.make_request = transport
    method = (
        method_type(chat_id=1001, media=[media])
        if method_type is SendMediaGroup
        else method_type(chat_id=1001, message_id=1, media=media)
    )
    await bot(method)
    sent = captured[0].media[0] if isinstance(captured[0].media, list) else captured[0].media
    assert sent.caption == "فایل 12" and sent.media == "opaque_file_12"
    assert media.caption == "فایل ۱۲"


async def test_plain_text_angle_brackets_are_not_mistaken_for_html_attributes(client):
    from aiogram.methods import SendMessage

    bot, tg = client
    await bot(SendMessage(chat_id=1001, text="بازه <۱۲>؛ فایل ۲", parse_mode=None))
    assert tg.calls[-1].params["text"] == "بازه <12>؛ فایل 2"


def test_quoted_html_attribute_with_angle_bracket_keeps_opaque_value():
    from services.ux_format import report_numbers

    original = '<a href="https://panel.test/sub?signature=x>۱۲">سفارش ۲</a>'
    assert report_numbers(original) == '<a href="https://panel.test/sub?signature=x>۱۲">سفارش 2</a>'
