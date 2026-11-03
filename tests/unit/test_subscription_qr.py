"""Smoke tests for subscription URL QR helper."""

from __future__ import annotations

import pytest

from services.subscription_qr import render_subscription_qr_png

_PNG_MAGIC = b"\x89PNG"


def test_render_subscription_qr_png_non_empty() -> None:
    url = "https://panel.example.com/sub/yasin_u7jf"
    payload = render_subscription_qr_png(url)
    assert payload.startswith(_PNG_MAGIC)
    assert len(payload) > 100


def test_render_subscription_qr_png_rejects_blank() -> None:
    with pytest.raises(ValueError):
        render_subscription_qr_png("")
    with pytest.raises(ValueError):
        render_subscription_qr_png("   ")
