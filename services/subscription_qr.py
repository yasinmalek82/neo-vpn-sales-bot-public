"""QR PNG helper for absolute subscription URLs."""

from __future__ import annotations

import io

import qrcode
from qrcode.constants import ERROR_CORRECT_M

_PNG_MAGIC = b"\x89PNG"


def render_subscription_qr_png(url: str, *, box_size: int = 8, border: int = 2) -> bytes:
    """Return non-empty PNG bytes for an absolute subscription URL.

    Raises ValueError when the URL is missing/blank or the image is invalid.
    """
    cleaned = (url or "").strip()
    if not cleaned:
        raise ValueError("missing_subscription_url")

    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(cleaned)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    payload = buffer.getvalue()
    if not payload.startswith(_PNG_MAGIC):
        raise ValueError("qr_png_invalid")
    return payload
