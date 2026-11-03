"""What a reseller actually sees — the screens, not the arithmetic.

Every defect in this file was found by walking the feature rather than by a
unit test, which is the point: the pricing unit was correct the whole time and
the surface around it kept throwing the answer away or describing it wrong.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.keyboards.main_menu import main_menu_keyboard
from bot.keyboards.purchase import ready_plans_keyboard
from bot.keyboards.reply_menu import (
    ACCOUNT,
    ACCOUNT_RESELLER,
    ReplyRoot,
    main_reply_keyboard,
    match_root,
)


def _pkg(slug: str, title: str, price: int) -> SimpleNamespace:
    return SimpleNamespace(id=1, slug=slug, title_fa=title, fixed_price_toman=price)


def _labels(markup) -> list[str]:  # type: ignore[no-untyped-def]
    return [b.text for row in markup.inline_keyboard for b in row]


# --------------------------------------------------------------------------- #
# The shelf
# --------------------------------------------------------------------------- #


def test_a_customer_sees_the_retail_price() -> None:
    labels = _labels(
        ready_plans_keyboard(
            [_pkg("a", "۵۰ گیگ", 150_000)],
            include_custom=False,
            prices_by_slug={"a": 150_000},
        )
    )
    assert "۱۵۰٬۰۰۰" in labels[0]


def test_a_reseller_browses_at_the_price_they_will_be_charged() -> None:
    """They were browsing at 150٬000 and charged 110٬000 at checkout.

    Not a pleasant surprise — a screen that disagrees with the till reads as
    broken, and the owner cannot tell which number is the bug.
    """
    labels = _labels(
        ready_plans_keyboard(
            [_pkg("a", "۵۰ گیگ", 150_000)],
            include_custom=False,
            prices_by_slug={"a": 110_000},
        )
    )
    assert "۱۱۰٬۰۰۰" in labels[0]
    assert "۱۵۰٬۰۰۰" not in labels[0]


def test_a_plan_with_no_agreed_price_keeps_the_retail_label() -> None:
    """They pay retail for it, so the shelf price is already right."""
    labels = _labels(
        ready_plans_keyboard(
            [_pkg("a", "ویژه", 150_000), _pkg("b", "اقتصادی", 90_000)],
            include_custom=False,
            prices_by_slug={"a": 110_000, "b": 90_000},
        )
    )
    assert "۱۱۰٬۰۰۰" in labels[0]
    assert "۹۰٬۰۰۰" in labels[1]


def test_prices_are_in_one_numeral_system() -> None:
    """«150٬000» — Persian separator, ASCII digits — on the busiest screen."""
    labels = _labels(
        ready_plans_keyboard(
            [_pkg("a", "۵۰ گیگ", 150_000)],
            include_custom=False,
            prices_by_slug={"a": 150_000},
        )
    )
    assert not any(ch.isdigit() and ch.isascii() for ch in labels[0])


# --------------------------------------------------------------------------- #
# The menu
# --------------------------------------------------------------------------- #


def test_the_menu_calls_them_customers_for_a_reseller() -> None:
    """A shop's list holds thirty services belonging to thirty other people."""
    customer = _labels(main_menu_keyboard(is_admin=False))
    reseller = _labels(main_menu_keyboard(is_admin=False, is_reseller=True))
    # Substring, not membership: the badge in front of the label is a taste
    # call that has already changed once, and pinning it here would make every
    # future change to it look like a broken reseller menu.
    assert any("سرویس‌های من" in text for text in customer)
    assert any("مشتری‌های من" in text for text in reseller)
    assert not any("سرویس‌های من" in text for text in reseller)


def test_the_bottom_keyboard_agrees_with_the_menu() -> None:
    rows = main_reply_keyboard(is_reseller=True).keyboard
    labels = [b.text for row in rows for b in row]
    assert ACCOUNT_RESELLER in labels
    assert ACCOUNT not in labels


def test_both_labels_route_to_the_same_screen() -> None:
    """Otherwise renaming the button quietly unwires it."""
    assert match_root(ACCOUNT) == ReplyRoot.ACCOUNT
    assert match_root(ACCOUNT_RESELLER) == ReplyRoot.ACCOUNT


@pytest.mark.parametrize("is_reseller", [False, True])
def test_the_menu_has_the_same_entries_either_way(is_reseller: bool) -> None:
    """Only the wording changes — a reseller must not lose a button."""
    entries = {
        b.callback_data
        for row in main_menu_keyboard(is_admin=False, is_reseller=is_reseller).inline_keyboard
        for b in row
    }
    plain = {
        b.callback_data for row in main_menu_keyboard(is_admin=False).inline_keyboard for b in row
    }
    assert entries == plain
