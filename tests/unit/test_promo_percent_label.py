"""Live finding: promo codes were listed as «15.0%»."""

from __future__ import annotations

import pytest

from bot.keyboards.store_admin import store_promo_keyboard


@pytest.mark.parametrize(
    ("percent", "shown"),
    [(15.0, "(15%)"), (15, "(15%)"), ("10.0", "(10%)"), (12.5, "(12.5%)"), (100.0, "(100%)")],
)
def test_a_whole_percent_is_listed_without_a_decimal(percent: object, shown: str) -> None:
    markup = store_promo_keyboard(
        [{"code": "SAVE", "percent": percent, "active": True}], is_owner=True
    )
    label = markup.inline_keyboard[0][0].text
    assert label.endswith(f"SAVE {shown}"), label
