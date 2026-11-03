"""The service QR on the brand template, or on a background the owner uploaded.

The template (assets/brand/qr_template_default.png) is 1600×1600 with a white
box in the middle for the code and a green pill at the bottom for the bot's
handle. The box is redrawn over the template's own placeholder, and the pill
over its placeholder handle, which is drawn live: a forwarded QR still says
which bot it came from, and the test bot and the live bot differ.

An uploaded background is square-cropped to the same size and the code goes
in the same box, so a design only has to leave the middle clear. Only the
code is drawn on it — its bottom line is the owner's to design.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError
from qrcode.constants import ERROR_CORRECT_M
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models.brand_asset import BrandAsset

TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "assets" / "brand" / "qr_template_default.png"
QR_BACKGROUND_KEY = "qr_background"

SIZE = 1600
# Measured on the template. The box is where the code goes on any background.
BOX = (396, 322, 1202, 1128)
BOX_RADIUS = 44
QR_SIDE = 700
PILL = (390, 1392, 912, 1486)
PILL_RADIUS = 30
PILL_GREEN = (46, 230, 160)
INK = (11, 15, 20)
# The whole bottom line, pill and «یا مستقیم بزن روی», for when there is no handle.
BOTTOM_LINE = (370, 1380, 1240, 1500)

# Telegram photos are capped at 10 MB; a bigger upload is not a background.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
_PNG_MAGIC = b"\x89PNG"


class QrBackgroundError(Exception):
    """The upload is not an image this can use."""


def _qr_image(url: str) -> Image.Image:
    cleaned = (url or "").strip()
    if not cleaned:
        raise ValueError("missing_subscription_url")
    code = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_M, box_size=10, border=2)
    code.add_data(cleaned)
    code.make(fit=True)
    image = code.make_image(fill_color="black", back_color="white").get_image().convert("RGB")
    # Nearest neighbour keeps every module a crisp square at any size.
    return image.resize((QR_SIDE, QR_SIDE), Image.Resampling.NEAREST)


def _font_for(text: str, *, width: int, start: int = 52) -> ImageFont.FreeTypeFont:
    size = start
    while size > 18:
        font = ImageFont.load_default(size=size)
        if isinstance(font, ImageFont.FreeTypeFont) and font.getlength(text) <= width:
            return font
        size -= 2
    return ImageFont.load_default(size=18)  # type: ignore[return-value]


def _draw_handle(canvas: Image.Image, handle: str | None) -> None:
    draw = ImageDraw.Draw(canvas)
    if not handle:
        # No handle to show: the template's placeholder must not stay either.
        draw.rectangle(BOTTOM_LINE, fill=INK)
        return
    draw.rounded_rectangle(PILL, radius=PILL_RADIUS, fill=PILL_GREEN)
    text = f"@{handle.lstrip('@')}"
    x0, y0, x1, y1 = PILL
    font = _font_for(text, width=(x1 - x0) - 48)
    draw.text(((x0 + x1) / 2, (y0 + y1) / 2), text, font=font, fill=INK, anchor="mm")


def render_branded_qr(url: str, *, background: bytes | None, handle: str | None) -> bytes:
    """PNG bytes of the service QR on the template or on ``background``."""
    qr = _qr_image(url)
    if background is None:
        canvas = Image.open(TEMPLATE_PATH).convert("RGB")
    else:
        canvas = Image.open(io.BytesIO(background)).convert("RGB")
    if canvas.size != (SIZE, SIZE):
        canvas = canvas.resize((SIZE, SIZE), Image.Resampling.LANCZOS)
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(BOX, radius=BOX_RADIUS, fill=(255, 255, 255))
    x0, y0, x1, y1 = BOX
    canvas.paste(qr, ((x0 + x1 - QR_SIDE) // 2, (y0 + y1 - QR_SIDE) // 2))
    if background is None:
        _draw_handle(canvas, handle)
    buffer = io.BytesIO()
    # No ``optimize``: it tripled render time to save bytes Telegram recompresses.
    canvas.save(buffer, format="PNG")
    payload = buffer.getvalue()
    if not payload.startswith(_PNG_MAGIC):
        raise ValueError("qr_png_invalid")
    return payload


def normalize_background(raw: bytes) -> bytes:
    """Centre-crop to a square, scale to 1600×1600, return PNG bytes."""
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise QrBackgroundError("too_large")
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise QrBackgroundError("not_an_image") from exc
    image = image.convert("RGB")
    side = min(image.size)
    if side < 400:
        raise QrBackgroundError("too_small")
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    image = image.crop((left, top, left + side, top + side))
    image = image.resize((SIZE, SIZE), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


async def load_qr_background(session: AsyncSession) -> bytes | None:
    row = await session.get(BrandAsset, QR_BACKGROUND_KEY)
    return bytes(row.content) if row is not None else None


async def has_qr_background(session: AsyncSession) -> bool:
    """Whether an upload replaces the template. Reads the key only, never the image."""
    return (
        await session.scalar(select(BrandAsset.key).where(BrandAsset.key == QR_BACKGROUND_KEY))
    ) is not None


async def save_qr_background(session: AsyncSession, raw: bytes) -> bytes:
    """Store the owner's upload, normalized. Returns what was stored."""
    content = normalize_background(raw)
    row = await session.get(BrandAsset, QR_BACKGROUND_KEY)
    if row is None:
        session.add(BrandAsset(key=QR_BACKGROUND_KEY, content=content))
    else:
        row.content = content
        row.updated_at = datetime.now(UTC)
    await session.flush()
    return content


async def clear_qr_background(session: AsyncSession) -> bool:
    """Back to the template. Returns whether there was an upload to remove."""
    row = await session.get(BrandAsset, QR_BACKGROUND_KEY)
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True
