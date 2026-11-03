"""The service QR on the brand template, or on the owner's own background.

Owner's decisions (2026-09-26): the template is the default; its bottom pill
shows the bot's handle drawn live (a forwarded QR still names the bot, and the
test and live bots differ); an uploaded background gets the code in the same
central box. No decoder is installed, so scannability is proven by reading
every module of the pasted code back from the pixels and comparing it with the
matrix the QR library generated.
"""

from __future__ import annotations

import io
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import qrcode
from PIL import Image
from qrcode.constants import ERROR_CORRECT_M
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from bot.delivery import service_qr_png
from database.base import Base
from database.models import BrandAsset
from services.qr_brand import (
    BOTTOM_LINE,
    BOX,
    INK,
    PILL,
    PILL_GREEN,
    QR_BACKGROUND_KEY,
    QR_SIDE,
    SIZE,
    QrBackgroundError,
    clear_qr_background,
    load_qr_background,
    normalize_background,
    render_branded_qr,
    save_qr_background,
)

URL = "https://pasar.example.ir:8000/sub/abcdEFGH1234567890abcdef"


def _open(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png)).convert("RGB")


def _png(size: tuple[int, int], colour: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def _modules_match(image: Image.Image, url: str) -> bool:
    """Every module of the pasted code, read back from the pixels."""
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


def test_the_code_on_the_template_is_the_code_for_the_link() -> None:
    image = _open(render_branded_qr(URL, background=None, handle="neo666_bot"))
    assert image.size == (SIZE, SIZE)
    assert _modules_match(image, URL)
    assert not _modules_match(image, URL + "x"), "the check can tell two links apart"


def test_the_templates_placeholder_is_covered() -> None:
    """«[QR CODE] / کد ربات را اینجا بگذارید» was grey text inside the box."""
    image = _open(render_branded_qr(URL, background=None, handle="neo666_bot"))
    pixels = image.load()
    x_qr0 = (BOX[0] + BOX[2] - QR_SIDE) // 2
    y_qr0 = (BOX[1] + BOX[3] - QR_SIDE) // 2
    corner = 50  # the box's rounded corners show the dark background
    margin = [
        pixels[x, y]
        for y in range(BOX[1] + 4, BOX[3] - 4, 3)
        for x in range(BOX[0] + 4, BOX[2] - 4, 3)
        if not (x_qr0 <= x < x_qr0 + QR_SIDE and y_qr0 <= y < y_qr0 + QR_SIDE)
        and not (
            (x < BOX[0] + corner or x > BOX[2] - corner)
            and (y < BOX[1] + corner or y > BOX[3] - corner)
        )
    ]
    assert len(margin) > 1000
    assert all(p == (255, 255, 255) for p in margin)


def test_the_pill_shows_the_live_handle() -> None:
    with_handle = _open(render_branded_qr(URL, background=None, handle="neo666_bot"))
    other = _open(render_branded_qr(URL, background=None, handle="another_long_bot"))
    crop = (PILL[0] + 10, PILL[1] + 10, PILL[2] - 10, PILL[3] - 10)
    pill = with_handle.crop(crop)
    colours = set(pill.getdata())
    assert PILL_GREEN in colours, "the pill is still there"
    assert any(sum(c) < 150 for c in colours), "and a handle is written on it"
    assert pill.tobytes() != other.crop(crop).tobytes(), "the handle is drawn, not baked in"


def test_with_no_handle_the_bottom_line_goes_rather_than_lie() -> None:
    """The template's «[@Neo_vpn_sell_bot]» placeholder must never reach a customer."""
    image = _open(render_branded_qr(URL, background=None, handle=None))
    region = image.crop(BOTTOM_LINE)
    assert set(region.getdata()) == {INK}


def test_an_uploaded_background_gets_the_code_in_the_same_box() -> None:
    red = (200, 30, 40)
    background = normalize_background(_png((900, 600), red))
    image = _open(render_branded_qr(URL, background=background, handle="neo666_bot"))
    assert image.size == (SIZE, SIZE)
    assert _modules_match(image, URL)
    # Only the code is drawn on an upload: its bottom line is the owner's design.
    assert set(image.crop(PILL).getdata()) == {red}
    assert image.getpixel((20, 20)) == red


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b"not an image at all", "not_an_image"),
        (_png((300, 300), (0, 0, 0)), "too_small"),
        (b"", "too_large"),
    ],
)
def test_an_unusable_upload_is_refused(raw: bytes, reason: str) -> None:
    with pytest.raises(QrBackgroundError, match=reason):
        normalize_background(raw)


def test_a_wide_upload_is_cropped_from_the_middle() -> None:
    """Left third blue, middle red, right third blue: the square is the red middle."""
    image = Image.new("RGB", (1500, 500), (0, 0, 255))
    image.paste((255, 0, 0), (500, 0, 1000, 500))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    out = _open(normalize_background(buffer.getvalue()))
    assert out.size == (SIZE, SIZE)
    assert out.getpixel((SIZE // 2, SIZE // 2)) == (255, 0, 0)
    assert out.getpixel((5, SIZE // 2))[0] > 200, "no blue edge kept"


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_the_upload_is_kept_replaced_and_cleared(session: AsyncSession) -> None:
    assert await load_qr_background(session) is None
    first = await save_qr_background(session, _png((800, 800), (10, 200, 10)))
    await session.commit()
    assert await load_qr_background(session) == first
    second = await save_qr_background(session, _png((800, 800), (10, 10, 200)))
    await session.commit()
    assert await load_qr_background(session) == second != first
    assert await clear_qr_background(session) is True
    await session.commit()
    assert await load_qr_background(session) is None
    assert await clear_qr_background(session) is False


@pytest.mark.asyncio
async def test_a_broken_stored_background_still_sends_a_working_code(
    session: AsyncSession,
) -> None:
    """A bad upload must never cost a customer the link they paid for."""
    session.add(BrandAsset(key=QR_BACKGROUND_KEY, content=b"corrupt"))
    await session.commit()
    png = await service_qr_png(
        session, SimpleNamespace(bot_username=""), url=URL, bot=None  # type: ignore[arg-type]
    )
    image = _open(png)
    assert image.size != (SIZE, SIZE), "the plain code, not a half-drawn template"


@pytest.mark.asyncio
async def test_the_customer_card_uses_the_branded_code(session: AsyncSession) -> None:
    png = await service_qr_png(
        session, SimpleNamespace(bot_username=""), url=URL, bot=None  # type: ignore[arg-type]
    )
    image = _open(png)
    assert image.size == (SIZE, SIZE)
    assert _modules_match(image, URL)


@pytest.mark.asyncio
async def test_the_admin_upload_screen_refuses_a_bad_photo_and_keeps_the_old_one(
    session: AsyncSession,
) -> None:
    from bot.handlers import admin as handlers
    from bot.texts import texts

    good = await save_qr_background(session, _png((800, 800), (1, 2, 3)))
    await session.commit()
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=4242),
        photo=[SimpleNamespace(file_id="f")],
        answer=AsyncMock(),
        answer_photo=AsyncMock(),
    )
    bot = SimpleNamespace(download=AsyncMock(return_value=io.BytesIO(b"not an image")))
    state = AsyncMock()
    await handlers.admin_qr_background_upload(
        message,  # type: ignore[arg-type]
        state,
        session,
        SimpleNamespace(admin_telegram_ids=[4242]),  # type: ignore[arg-type]
        SimpleNamespace(id=1, telegram_id=4242, is_admin=True),  # type: ignore[arg-type]
        bot,  # type: ignore[arg-type]
    )
    said = message.answer.await_args.args[0]
    assert texts.admin_qr_bg_reason_not_an_image in said
    assert message.answer.await_args.kwargs["reply_markup"] is not None
    assert await load_qr_background(session) == good


def test_every_module_edge_is_crisp() -> None:
    """Only black and white in the code: grey edges are what a phone camera
    struggles with at an angle, even when each module's centre is right."""
    image = _open(render_branded_qr(URL, background=None, handle="neo666_bot"))
    x0 = (BOX[0] + BOX[2] - QR_SIDE) // 2
    y0 = (BOX[1] + BOX[3] - QR_SIDE) // 2
    code = image.crop((x0, y0, x0 + QR_SIDE, y0 + QR_SIDE))
    assert set(code.getdata()) <= {(0, 0, 0), (255, 255, 255)}
