"""The navigation each role sees, and the screen it opens first.

One place, so a new admin screen is one tuple entry and an admin link can never
reach a non-admin's nav by a template edit. ``web/routes/me.py`` renders these
for ``GET /panel/me/home`` from the role the signature produced.
"""

from __future__ import annotations

from dataclasses import dataclass

from web.auth import ROLE_ADMIN, ROLE_RESELLER

# Stroke paths for a 24x24 icon, drawn by the shell's ``.ico`` style.
ICON_CHART = "M4 20V10M10 20V4M16 20v-7M21 20H3"
ICON_WALLET = "M4 7h15a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1zM4 7l12-3v3M16 13h2"
ICON_STORE = "M4 9l1.5-5h13L20 9M4 9v10h16V9M4 9h16M9 19v-5h6v5"
ICON_TAG = "M3 12V4h8l10 10-8 8L3 12zM8.7 8.5a1.2 1.2 0 1 1-2.4 0 1.2 1.2 0 0 1 2.4 0z"
ICON_HORN = "M4 14v-4l11-5v14L4 14zM7 14l1 6h3l-1-5"
ICON_BAG = "M5 8h14l-1 12H6L5 8zM9 8V6a3 3 0 0 1 6 0v2"
ICON_PEOPLE = (
    "M16 20v-1.5a3.5 3.5 0 0 0-3.5-3.5h-5A3.5 3.5 0 0 0 4 18.5V20M10 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z"
    "M20 20v-1.5a3.5 3.5 0 0 0-2.5-3.35M15.5 4.2a3.5 3.5 0 0 1 0 6.6"
)
ICON_GEAR = (
    "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1"
    "a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3"
    "l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1"
    "a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3h.1"
    "a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1"
    "a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8v.1a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"
)

# The first screen an admin lands on (the owner's choice, 2026-10-10: the store).
ADMIN_FIRST_SCREEN = "/panel/store"


@dataclass(frozen=True)
class NavItem:
    label: str
    url: str
    icon: str


@dataclass(frozen=True)
class Nav:
    note: str  # the small line under the brand name in the side column
    items: tuple[NavItem, ...]
    first_screen: str


ADMIN_NAV = Nav(
    note="مدیریت",
    items=(
        NavItem("فروشگاه", "/panel/store", ICON_BAG),
        NavItem("مالی", "/panel/finance", ICON_CHART),
        NavItem("مشتری‌ها", "/panel/customers", ICON_PEOPLE),
        NavItem("نماینده‌ها", "/panel/resellers", ICON_STORE),
        NavItem("کدهای تخفیف", "/panel/promo", ICON_TAG),
        NavItem("کمپین‌ها", "/panel/marketing", ICON_HORN),
        NavItem("تنظیمات", "/panel/settings", ICON_GEAR),
    ),
    first_screen=ADMIN_FIRST_SCREEN,
)
RESELLER_NAV = Nav(
    note="نمایندگی",
    items=(NavItem("داشبورد", "/panel/me/reseller", ICON_STORE),),
    first_screen="/panel/me/reseller",
)
CUSTOMER_NAV = Nav(
    note="حساب من",
    items=(NavItem("حساب من", "/panel/me", ICON_WALLET),),
    first_screen="/panel/me",
)


def nav_for(role: str) -> Nav:
    """The nav of ``role``; anything but admin and reseller gets the customer's."""
    if role == ROLE_ADMIN:
        return ADMIN_NAV
    if role == ROLE_RESELLER:
        return RESELLER_NAV
    return CUSTOMER_NAV
