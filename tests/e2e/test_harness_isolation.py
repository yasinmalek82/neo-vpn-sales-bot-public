"""The harness must not touch the developer's machine, and must leave no trace.

Nothing may come from ``.env`` or the environment, ``data/bot.db`` is never opened,
the schema is the real Alembic chain, and every piece of global state it swaps is
back afterwards (otherwise the unit suite in the same pytest run would break).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import bot.config as bot_config
import database.base as db_base
from api_client.client import PasarGuardClient
from bot.keyboards import style
from tests.e2e.harness import App
from tests.e2e.harness import settings as harness_settings
from tests.e2e.harness.app import running_app
from tests.e2e.harness.settings import make_settings

ROOT = Path(__file__).resolve().parents[2]


def test_nothing_leaks_from_dotenv_or_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "ADMIN_TELEGRAM_IDS=111\nPAYMENT_CARD_NUMBER=5022000000000000\n"
        "SUPPORT_HANDLE=@owners_real_handle\nPASARGUARD_API_KEY=pg_key_real\n"
    )
    monkeypatch.chdir(tmp_path)
    # CI exports this one, and the environment outranks ``.env``: the control
    # below would then read CI's value and never prove ``.env`` is a source.
    monkeypatch.delenv("ADMIN_TELEGRAM_IDS", raising=False)
    monkeypatch.setenv("PASARGUARD_BASE_URL", "https://real-panel.example")
    monkeypatch.setenv("CHANNEL_HANDLE", "@real_channel")
    monkeypatch.setenv("TRIAL_ENABLED", "false")
    monkeypatch.setenv("BOT_TOKEN", "999:real-token")

    # Control: a plain Settings() really does see both sources here.
    leaky = bot_config.Settings()  # type: ignore[call-arg]
    assert leaky.admin_telegram_ids == [111] and leaky.channel_handle == "@real_channel"

    clean = make_settings("sqlite+aiosqlite:///x.db", tmp_path)
    assert clean.admin_telegram_ids == harness_settings.ADMIN_IDS
    assert clean.payment_card_number == harness_settings.CARD_NUMBER
    assert clean.support_handle == harness_settings.SUPPORT_HANDLE
    assert clean.pasar_guard_api_key == harness_settings.PANEL_KEY
    assert clean.pasar_guard_base_url == harness_settings.PANEL_URL
    assert clean.channel_handle == harness_settings.CHANNEL_HANDLE
    assert clean.trial_enabled is True
    assert clean.bot_token == harness_settings.BOT_TOKEN
    # ...and the environment is back the way the developer had it.
    import os

    assert os.environ["CHANNEL_HANDLE"] == "@real_channel"


async def test_the_scenario_database_is_a_private_migrated_file(app: App) -> None:
    assert app.db_path.name == "bot.e2e.db"
    assert "data/bot.db" not in str(db_base.get_engine().url)
    assert str(db_base.get_engine().url).endswith(str(app.db_path))
    assert app.settings.database_url.endswith(str(app.db_path))
    connection = sqlite3.connect(app.db_path)
    try:
        (version,) = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        orders_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='orders'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert version  # created by `alembic upgrade head`, not by create_all
    assert "EXPIRED" in orders_sql  # the CHECK that only the real migrations carry


async def test_the_customer_database_is_never_touched(app: App) -> None:
    real = ROOT / "data" / "bot.db"
    before = real.stat().st_mtime_ns if real.exists() else None
    ali = app.customer("ali")
    await ali.start()
    after = real.stat().st_mtime_ns if real.exists() else None
    assert before == after


async def test_every_swapped_global_is_restored(
    migrated_template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    registered_dispatcher: object,
) -> None:
    import scheduler as scheduler_module

    snapshot = (
        db_base._engine,
        db_base._session_factory,
        bot_config.settings,
        bot_config.get_settings,
        PasarGuardClient.__init__,
        style._enabled,
        scheduler_module._scheduler,
        scheduler_module.AsyncIOScheduler,
    )
    other_parents = [(r, r._parent_router) for r in registered_dispatcher.sub_routers]  # type: ignore[attr-defined]
    with monkeypatch.context() as patches:
        for _ in range(2):  # a second run proves the routers were handed back
            async with running_app(migrated_template, tmp_path, patches) as running:
                assert bot_config.get_settings() is running.settings
        after = (
            db_base._engine,
            db_base._session_factory,
            scheduler_module._scheduler,
            style._enabled,
        )
    assert after == (snapshot[0], snapshot[1], snapshot[6], snapshot[5])
    assert bot_config.settings is snapshot[2]
    assert bot_config.get_settings is snapshot[3]
    assert PasarGuardClient.__init__ is snapshot[4]
    assert scheduler_module.AsyncIOScheduler is snapshot[7]
    # The session-wide dispatcher the unit suite shares keeps all its routers.
    assert [(r, r._parent_router) for r in registered_dispatcher.sub_routers] == other_parents  # type: ignore[attr-defined]
    assert all(parent is registered_dispatcher for _, parent in other_parents)


async def test_the_panel_the_bot_reaches_is_only_the_fake(app: App) -> None:
    from api_client.factory import create_pasarguard_client

    client = create_pasarguard_client(app.settings)
    await client.get_current_admin()
    assert [r.key for r in app.panel.requests] == ["GET /api/admin"]
    await client.aclose()
    app.settings.pasar_guard_base_url = "https://real.example"
    with pytest.raises(AssertionError, match="left the machine"):
        create_pasarguard_client(app.settings)
