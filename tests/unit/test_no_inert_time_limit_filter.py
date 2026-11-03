"""«فیلتر محدودیت زمانی قبل از پلن» saved a flag the purchase flow never read.

T-006 replaced that pre-plan question with duration tabs, and the flat list
has no tabs at all. The button was retired, but the group card kept reporting
the flag as «روشن/خاموش» — a status for a setting with no effect and no way to
change it. A control nothing reads is worse than none (CLAUDE.md).

Ported from ``feat/plan-ladder`` (51c65b7). ``StoreCB.CAT_TL`` stays on this
branch so an old button still gets the «retired» alert.
"""

from __future__ import annotations

from types import SimpleNamespace

from bot.handlers import store_admin as store_admin_handlers
from bot.keyboards.store_admin import (
    store_cat_sale_keyboard,
    store_category_dashboard_keyboard,
)
from bot.texts import texts


def _volume_category() -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        slug="volume",
        title_fa="حجمی",
        enabled=True,
        sort_order=10,
        panel_group_ids=[],
        parent_id=None,
        sales_flags={"kind": "volume", "time_limit_filter_enabled": True},
    )


def test_category_card_offers_no_time_limit_filter() -> None:
    cat = _volume_category()
    for markup in (
        store_category_dashboard_keyboard(cat, is_owner=True),  # type: ignore[arg-type]
        store_cat_sale_keyboard(cat),  # type: ignore[arg-type]
    ):
        labels = [btn.text for row in markup.inline_keyboard for btn in row]
        assert not any("محدودیت زمانی" in label for label in labels)


def test_category_dashboard_text_does_not_report_the_filter() -> None:
    from services.store_admin import CatalogView

    cat = _volume_category()
    view = CatalogView(version=None, is_draft=False, categories=[cat], packages=[])  # type: ignore[arg-type,list-item]
    body = store_admin_handlers._dashboard_text(view, cat)  # type: ignore[arg-type]
    assert "محدودیت زمانی" not in body
    assert "محدودیت زمانی" not in texts.admin_store_dash_shelf
