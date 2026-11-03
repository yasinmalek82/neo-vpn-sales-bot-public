"""Live finding: a panel key with `groups.read_simple` but not `groups.read`.

`GET /api/groups` answers 403, `GET /api/groups/simple` answers 200; the
«🔗 گروه پنل» picker and «📋 لیست گروه‌های پنل» both said the list did not come.
"""

from __future__ import annotations

from tests.e2e._s07b_helpers import tap_starting
from tests.e2e._s14_helpers import open_panel_page
from tests.e2e.harness import App, publish_store


def _only_simple(app: App) -> None:
    """The key may read the simple list only."""
    app.panel.forbid("GET /api/groups")
    assert not app.panel._faults[-1].pattern.endswith("simple")  # noqa: SLF001


async def test_the_group_list_falls_back_to_the_simple_list(app: App) -> None:
    admin = app.admin()
    _only_simple(app)
    await open_panel_page(admin)

    await tap_starting(admin, "📋 لیست گروه")

    assert "#1 direct" in admin.last_text, admin.last_text
    assert "#2 tunnel" in admin.last_text
    assert "#3 premium" in admin.last_text
    # The simple list carries no counts: none may be invented.
    assert "کاربر" not in admin.last_text and "اینباند" not in admin.last_text, admin.last_text
    assert len(app.panel.requests_to("GET /api/groups/simple")) == 1


async def test_the_store_group_picker_falls_back_to_the_simple_list(app: App) -> None:
    await publish_store(app)
    admin = app.admin()
    _only_simple(app)
    await admin.send("/admin")
    await tap_starting(admin, "🛍 فروشگاه")
    await tap_starting(admin, "نامحدود")
    await tap_starting(admin, "🖥 پنل")

    await tap_starting(admin, "🔗 گروه پنل")

    labels = " | ".join(admin.buttons)
    assert "direct" in labels and "tunnel" in labels and "premium" in labels, admin.buttons
