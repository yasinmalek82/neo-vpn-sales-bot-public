"""Helpers for section ۱۷ (marketing nudges)."""

from __future__ import annotations

from typing import Any

from database.models.marketing import MarketingSettings
from tests.e2e._s07b_helpers import tap_starting
from tests.e2e.harness import App
from tests.e2e.harness.actors import Actor


async def open_marketing(admin: Actor) -> None:
    """/admin → ⚙️ تنظیمات → 📣 پیام‌های بازاریابی."""
    await admin.send("/admin")
    await tap_starting(admin, "تنظیمات")
    await tap_starting(admin, "📣 پیام‌های بازاریابی")


async def marketing_row(app: App) -> MarketingSettings | None:
    async with app.session() as session:
        return await session.get(MarketingSettings, 1)


async def switch_on(app: App, *campaigns: str, **values: Any) -> None:
    """Write the settings row directly (a scenario about a job, not about the screen)."""
    from datetime import UTC, datetime, timedelta

    async with app.session() as session:
        row = await session.get(MarketingSettings, 1)
        if row is None:
            row = MarketingSettings(id=1)
            session.add(row)
        for name in campaigns:
            # Long before anything the scenario creates, unless it says otherwise.
            setattr(row, f"{name}_enabled_at", datetime.now(UTC) - timedelta(days=365))
        for key, value in values.items():
            setattr(row, key, value)
        await session.commit()
