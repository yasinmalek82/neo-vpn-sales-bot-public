"""One navigation contract for every admin screen.

Before this module the same destination was rendered four different ways —
«🏠 بازگشت به پنل ادمین», «🏠 خانه ادمین», a bare «بازگشت» that went somewhere
else, and an «❌ لغو» that jumped all the way home — and several screens had no
way back at all. Every admin keyboard now ends with rows built here, so a label
is written once and every destination keeps one name.

This module sits at the bottom of the keyboard dependency graph: it owns the
canonical callback strings and imports nothing from the other keyboard modules.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton

# Canonical callbacks. ``AdminCB`` re-exports these so there is a single source.
ADMIN_HOME_CB = "adm:home"
ADMIN_CANCEL_CB = "adm:cancel"

HOME_LABEL = "🏠 خانه ادمین"
CANCEL_LABEL = "❌ لغو"


def back_button(to: str, label: str) -> InlineKeyboardButton:
    """One step up. ``label`` names the parent screen, never a bare «بازگشت»."""
    return InlineKeyboardButton(text=f"⬅️ {label}", callback_data=to)


def home_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text=HOME_LABEL, callback_data=ADMIN_HOME_CB)


def cancel_button() -> InlineKeyboardButton:
    """Abort a text prompt. Pairs with the global ``/cancel`` command."""
    return InlineKeyboardButton(text=CANCEL_LABEL, callback_data=ADMIN_CANCEL_CB)


def nav_rows(
    *,
    back_to: str | None = None,
    back_label: str | None = None,
    home: bool = True,
) -> list[list[InlineKeyboardButton]]:
    """Trailing rows of an admin screen: optional parent, then home.

    Every admin keyboard ends with this, which is what the navigation test in
    ``tests/unit/test_admin_navigation.py`` asserts.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if back_to is not None:
        rows.append([back_button(back_to, back_label or "بازگشت")])
    if home:
        rows.append([home_button()])
    return rows
