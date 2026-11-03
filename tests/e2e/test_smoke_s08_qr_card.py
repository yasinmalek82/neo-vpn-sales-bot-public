"""Section 8, the QR on its template and the payment card the owner edits.

Rows ۸.۱۶ to ۸.۱۹, ۸.۲۹ and ۸.۳۰. «Scans with a phone» (۸.۱۶) is MANUAL; what is proven here is
that the code in the delivered picture is the code for the account's own link.
"""

from __future__ import annotations

import io

import pytest
import qrcode
from PIL import Image
from qrcode.constants import ERROR_CORRECT_M

from bot.texts import texts
from services.qr_brand import BOX, PILL, PILL_GREEN, QR_SIDE, SIZE, TEMPLATE_PATH
from tests.e2e._s07b_helpers import tap_starting
from tests.e2e._s08_helpers import buy_active, enable_downloads, my_service, square_png
from tests.e2e.harness import App, Store, digits, order_by_card, plain, publish_store, smoke
from tests.e2e.harness.actors import Actor
from tests.e2e.harness.settings import CARD_NUMBER
from tests.e2e.harness.telegram import SentMessage

RED = (200, 30, 40)


@pytest.fixture
async def store(app: App) -> Store:
    return await publish_store(app)


def _image(message: SentMessage) -> Image.Image:
    assert message.kind == "photo" and message.file_bytes, message
    return Image.open(io.BytesIO(message.file_bytes)).convert("RGB")


def _code_reads(image: Image.Image, url: str) -> bool:
    """Every module of the code in the picture, read back from its pixels, equals the link's."""
    code = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_M, box_size=10, border=2)
    code.add_data(url)
    code.make(fit=True)
    matrix = code.get_matrix()
    n = len(matrix)
    x0 = (BOX[0] + BOX[2] - QR_SIDE) // 2
    y0 = (BOX[1] + BOX[3] - QR_SIDE) // 2
    step = QR_SIDE / n
    pixels = image.load()
    for row in range(n):
        for col in range(n):
            r, g, b = pixels[int(x0 + (col + 0.5) * step), int(y0 + (row + 0.5) * step)]
            if (r + g + b < 3 * 128) != bool(matrix[row][col]):
                return False
    return True


def _link(app: App, user) -> str:
    return f"{app.panel.base_url}/sub/{user.token}"


def _on_default_template(image: Image.Image) -> bool:
    template = Image.open(TEMPLATE_PATH).convert("RGB")
    return image.getpixel((20, 20)) == template.getpixel((20, 20)) and PILL_GREEN in set(
        image.crop((PILL[0] + 10, PILL[1] + 10, PILL[2] - 10, PILL[3] - 10)).getdata()
    )


async def _service_card_photo(ali: Actor) -> SentMessage:
    await my_service(ali)
    return ali.last_message


# --------------------------------------------------------------------- the picture


@smoke("۸.۱۶")
async def test_the_delivered_qr_is_on_the_template_with_this_bots_handle_and_encodes_the_link(
    app: App, store: Store
) -> None:
    ali = app.customer("ali")
    account = await buy_active(app, store, ali)
    delivered = next(m for m in reversed(ali.messages) if m.kind == "photo")
    for photo in (delivered, await _service_card_photo(ali)):
        image = _image(photo)
        assert image.size == (SIZE, SIZE)
        assert _code_reads(image, _link(app, account)), "the picture's code is the account's link"
        assert not _code_reads(image, _link(app, account) + "x")
        assert _on_default_template(image), "template, with the handle pill"
        pill = image.crop((PILL[0] + 10, PILL[1] + 10, PILL[2] - 10, PILL[3] - 10))
        assert any(sum(c) < 150 for c in set(pill.getdata())), "a handle is written on the pill"


# ------------------------------------------------------------------- the background


@smoke("۸.۱۷")
async def test_a_square_photo_becomes_the_background_with_the_code_in_the_middle(
    app: App, store: Store
) -> None:
    enable_downloads(app)
    admin = app.admin()
    await admin.send("/admin")
    await tap_starting(admin, "تنظیمات")
    await tap_starting(admin, "پس‌زمینهٔ QR")
    await admin.send_photo(data=square_png(RED))
    assert plain(texts.admin_qr_bg_saved) in admin.last_text
    preview = _image(admin.last_message)
    assert preview.getpixel((20, 20)) == RED, "the preview is on the owner's picture"
    assert preview.getpixel((SIZE // 2, (BOX[1] + BOX[3]) // 2)) != RED, "the code is in the middle"
    assert "♻️ برگشت به قالب پیش‌فرض" in admin.buttons

    ali = app.customer("ali")
    account = await buy_active(app, store, ali)
    delivered = _image(next(m for m in reversed(ali.messages) if m.kind == "photo"))
    assert delivered.getpixel((20, 20)) == RED, "the next customer's service gets it"
    assert _code_reads(delivered, _link(app, account))
    assert set(delivered.crop(PILL).getdata()) == {
        RED
    }, "an upload is not drawn over below the code"


@smoke("۸.۱۸")
async def test_a_text_or_a_document_is_refused_in_persian_and_the_old_background_stays(
    app: App, store: Store
) -> None:
    enable_downloads(app)
    admin = app.admin()
    await admin.send("/admin")
    await tap_starting(admin, "تنظیمات")
    await tap_starting(admin, "پس‌زمینهٔ QR")
    await admin.send_photo(data=square_png(RED))
    refusal = plain(texts.admin_qr_bg_bad.format(reason=texts.admin_qr_bg_reason_not_an_image))
    await admin.send("این یک متن است")
    assert admin.last_text == refusal and "این عکس قابل استفاده نیست" in admin.last_text
    file_id = app.tg.register_file(square_png((10, 10, 10)))
    await admin._send_message(  # noqa: SLF001 - the actor has no «send a file»; this is one
        document={"file_id": file_id, "file_unique_id": file_id[:12], "file_name": "bg.png"}
    )
    assert admin.last_text == refusal
    ali = app.customer("ali")
    await buy_active(app, store, ali)
    delivered = _image(next(m for m in reversed(ali.messages) if m.kind == "photo"))
    assert delivered.getpixel((20, 20)) == RED, "the earlier background was not touched"


@smoke("۸.۱۹")
async def test_resetting_to_the_default_template_asks_then_shows_the_default_preview(
    app: App, store: Store
) -> None:
    enable_downloads(app)
    admin = app.admin()
    await admin.send("/admin")
    await tap_starting(admin, "تنظیمات")
    await tap_starting(admin, "پس‌زمینهٔ QR")
    await admin.send_photo(data=square_png(RED))
    await tap_starting(admin, "♻️ برگشت به قالب پیش‌فرض")
    assert admin.last_text == plain(texts.admin_qr_bg_reset_ask)
    ali = app.customer("ali")
    await buy_active(app, store, ali, "special_50", "ali12")
    still_red = _image(next(m for m in reversed(ali.messages) if m.kind == "photo"))
    assert still_red.getpixel((20, 20)) == RED, "the question alone changed nothing"
    await admin.tap("♻️ بله، قالب پیش‌فرض")
    preview = _image(admin.last_message)
    assert _on_default_template(preview), "the default template is back, with its pill"
    assert "♻️ برگشت به قالب پیش‌فرض" not in admin.buttons
    bob = app.customer("bob")
    await buy_active(app, store, bob, "special_50", "bobo1")
    assert _on_default_template(
        _image(next(m for m in reversed(bob.messages) if m.kind == "photo"))
    )


# ---------------------------------------------------------------- the payment card


NEW_CARD_FA = "۶۰۳۷-۹۹۷۱ ۱۲۳۴ ۵۶۷۸"


async def _open_payment(admin: Actor) -> None:
    await admin.send("/admin")
    await tap_starting(admin, "فروشگاه")
    await tap_starting(admin, "💳 پرداخت")


@smoke("۸.۲۹")
async def test_a_new_card_with_persian_digits_shows_on_the_next_card_purchase_without_restart(
    app: App, store: Store
) -> None:
    admin = app.admin()
    await _open_payment(admin)
    assert CARD_NUMBER[:4] in digits(admin.last_text) and "از .env" in admin.last_text
    await tap_starting(admin, "🔢 شماره کارت")
    await admin.send(NEW_CARD_FA)
    assert "6037 9971 1234 5678" in admin.last_text, "typed Persian digits, shown as a card"
    assert CARD_NUMBER not in digits(admin.last_text.split("قبلی")[0])
    await tap_starting(admin, "تأیید و ذخیره")
    assert "6037 9971 1234 5678" in admin.last_text and "✅ ذخیره شد" in admin.last_text

    ali = app.customer("ali")
    await ali.start()
    await order_by_card(ali, store, "special_50")
    assert "6037997112345678" in digits(ali.last_text).replace(" ", ""), ali.last_text
    assert CARD_NUMBER not in digits(ali.last_text).replace(" ", "")


@smoke("۸.۳۰")
async def test_back_to_env_brings_the_original_card_back(app: App, store: Store) -> None:
    admin = app.admin()
    await _open_payment(admin)
    await tap_starting(admin, "🔢 شماره کارت")
    await admin.send(NEW_CARD_FA)
    await tap_starting(admin, "تأیید و ذخیره")
    await tap_starting(admin, "♻️ برگشت به .env")
    assert "از .env خوانده می‌شود" in admin.last_text and "6037 9971 1234 5678" in admin.last_text
    await tap_starting(admin, "♻️ بله، برگشت به .env")
    assert (
        CARD_NUMBER[:4] in digits(admin.last_text) and "از .env" in admin.last_text
    ), admin.last_text
    ali = app.customer("ali")
    await ali.start()
    await order_by_card(ali, store, "special_50")
    assert CARD_NUMBER in digits(ali.last_text).replace(" ", ""), ali.last_text
