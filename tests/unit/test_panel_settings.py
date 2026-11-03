"""Unit tests for in-bot PasarGuard settings (API key only)."""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from api_client.exceptions import (
    PasarGuardAPIError,
    PasarGuardAuthError,
    PasarGuardError,
    PasarGuardNotFoundError,
)
from bot.config import Settings
from database.base import Base
from database.models.pasarguard_settings import PasarGuardSettings
from services.panel_settings import (
    check_panel_connection,
    clear_to_env,
    get_effective_pasarguard_config,
    get_trial_hwid_limit,
    panel_error_reason_fa,
    public_settings_snapshot,
    save_pasarguard_settings,
    save_trial_shape,
)


def _settings(**overrides: object) -> Settings:
    base = {
        "BOT_TOKEN": "test-token",
        "PASARGUARD_BASE_URL": "https://env.example.com",
        "PASARGUARD_API_KEY": "pg_key_envvvvv-envv-4env-8env-envvenvvenvv",
        "PASARGUARD_SUB_PATH": "env-sub",
        "PASARGUARD_TIMEOUT": "30",
        "DEFAULT_PANEL_GROUP_IDS": "[1, 2]",
        "ADMIN_TELEGRAM_IDS": "[]",
    }
    for k, v in overrides.items():
        key = k.upper() if not k.isupper() else k
        base[key] = str(v)
    return Settings(
        _env_file=None,
        **{  # type: ignore[call-arg]
            "BOT_TOKEN": base["BOT_TOKEN"],
            "PASARGUARD_BASE_URL": base["PASARGUARD_BASE_URL"],
            "PASARGUARD_API_KEY": base.get("PASARGUARD_API_KEY", ""),
            "PASARGUARD_SUB_PATH": base["PASARGUARD_SUB_PATH"],
            "PASARGUARD_TIMEOUT": float(base["PASARGUARD_TIMEOUT"]),
            "DEFAULT_PANEL_GROUP_IDS": [1, 2],
            "ADMIN_TELEGRAM_IDS": [],
            # Passed through, not dropped: a trial test that sets the default
            # value would otherwise pass without the env ever being read.
            "TRIAL_HWID_LIMIT": int(base.get("TRIAL_HWID_LIMIT", 1)),
        },
    )


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_env_fallback_when_db_empty(session):
    settings = _settings()
    cfg = await get_effective_pasarguard_config(session, settings)
    assert cfg.source == "env"
    assert cfg.base_url == "https://env.example.com"
    assert cfg.api_key.startswith("pg_key_")
    assert cfg.uses_api_key is True
    assert cfg.auth_mode == "api_key"
    assert cfg.sub_path == "env-sub"


@pytest.mark.asyncio
async def test_db_override_wins_over_env(session):
    settings = _settings()
    await save_pasarguard_settings(
        session,
        admin_user_id=42,
        base_url="https://db.example.com",
        api_key="pg_key_dbdbdbdb-dbdb-4dbd-8dbd-dbdbdbdbdbdb",
        sub_path="db-sub",
        timeout=12.5,
    )
    await session.commit()

    cfg = await get_effective_pasarguard_config(session, settings)
    assert cfg.source == "db"
    assert cfg.base_url == "https://db.example.com"
    assert cfg.api_key == "pg_key_dbdbdbdb-dbdb-4dbd-8dbd-dbdbdbdbdbdb"
    assert cfg.uses_api_key is True
    assert cfg.auth_mode == "api_key"
    assert cfg.sub_path == "db-sub"
    assert cfg.timeout == 12.5

    row = await session.get(PasarGuardSettings, 1)
    assert row is not None
    assert row.admin_username is None
    assert row.admin_password is None


@pytest.mark.asyncio
async def test_partial_db_override_is_mixed(session):
    settings = _settings()
    await save_pasarguard_settings(session, base_url="https://only-url.example.com")
    await session.commit()
    cfg = await get_effective_pasarguard_config(session, settings)
    assert cfg.source == "mixed"
    assert cfg.base_url == "https://only-url.example.com"
    assert cfg.api_key.startswith("pg_key_env")


@pytest.mark.asyncio
async def test_clear_to_env(session):
    settings = _settings()
    await save_pasarguard_settings(
        session,
        base_url="https://db.example.com",
        api_key="pg_key_dbonlyyy-dbdb-4dbd-8dbd-dbdbdbdbdbdb",
    )
    await session.commit()
    await clear_to_env(session, admin_user_id=1)
    await session.commit()
    cfg = await get_effective_pasarguard_config(session, settings)
    assert cfg.source == "env"
    assert cfg.base_url == "https://env.example.com"
    assert cfg.api_key.startswith("pg_key_env")


@pytest.mark.asyncio
async def test_api_key_not_in_repr_or_str(session):
    settings = _settings(PASARGUARD_API_KEY="")
    await save_pasarguard_settings(
        session,
        api_key="pg_key_super-secret-key-value-zzzz",
    )
    await session.commit()
    cfg = await get_effective_pasarguard_config(session, settings)
    assert "pg_key_super-secret-key-value-zzzz" not in repr(cfg)
    assert "pg_key_super-secret-key-value-zzzz" not in str(cfg)
    row = await session.get(PasarGuardSettings, 1)
    assert row is not None
    assert "pg_key_super-secret-key-value-zzzz" not in repr(row)
    snap = public_settings_snapshot(row, settings, cfg)
    assert snap["api_key_masked"] == "••••"
    assert snap["auth_mode"] == "api_key"
    assert "admin_username" not in snap
    assert "admin_password_masked" not in snap
    dump = str(snap)
    assert "pg_key_super-secret-key-value-zzzz" not in dump


@pytest.mark.asyncio
async def test_connection_success_mock(session):
    settings = _settings()
    client = MagicMock()
    client.get_current_admin = AsyncMock(return_value=MagicMock())
    client.aclose = AsyncMock()
    ok, msg = await check_panel_connection(session, settings, client=client)
    assert ok is True
    assert msg == "ok"
    client.get_current_admin.assert_awaited_once()


@pytest.mark.asyncio
async def test_connection_auth_fail_mock(session):
    settings = _settings()
    client = MagicMock()
    client.get_current_admin = AsyncMock(
        side_effect=PasarGuardAuthError("PasarGuard unauthorized (api_key)", status_code=401)
    )
    client.aclose = AsyncMock()
    ok, msg = await check_panel_connection(session, settings, client=client)
    assert ok is False
    assert "password" not in msg.lower()
    assert "pg_key_" not in msg.lower()
    assert msg == "پنل کلید API را قبول نکرد"


@pytest.mark.asyncio
async def test_connection_api_fail_sanitizes(session):
    settings = _settings()
    client = MagicMock()
    client.get_current_admin = AsyncMock(side_effect=PasarGuardError("timeout talking to panel"))
    client.aclose = AsyncMock()
    ok, msg = await check_panel_connection(session, settings, client=client)
    assert ok is False
    assert msg == "پنل در زمان تعیین‌شده جواب نداد"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (httpx.ConnectError("All connection attempts failed"), "به پنل وصل نشد"),
        (httpx.ReadTimeout("slow"), "جواب نداد"),
        (
            PasarGuardAPIError("PasarGuard API error (HTTP 502): bad gateway", status_code=502),
            "۵۰۲",
        ),
        (PasarGuardNotFoundError("PasarGuard resource not found", status_code=404), "آدرس پنل"),
        (ValueError("base_url is required for api_key='pg_key_x'"), "تنظیمات اتصال"),
        (RuntimeError("secret token=abc"), "دلیل نامشخص"),
    ],
)
def test_every_connection_failure_reads_as_a_persian_reason(exc, expected) -> None:
    reason = panel_error_reason_fa(exc).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))
    assert expected in reason
    assert not re.search(r"[A-Za-z]{4,}", reason.replace("API", "")), reason


@pytest.mark.asyncio
async def test_effective_config_db_api_key_overrides_env(session):
    settings = _settings(PASARGUARD_API_KEY="pg_key_envvvvv-envv-4env-8env-envvenvvenvv")
    cfg = await get_effective_pasarguard_config(session, settings)
    assert cfg.uses_api_key is True
    assert cfg.api_key.startswith("pg_key_")
    assert cfg.auth_mode == "api_key"

    await save_pasarguard_settings(session, api_key="pg_key_dddddddd-dddd-4ddd-8ddd-dddddddddddd")
    await session.commit()
    cfg = await get_effective_pasarguard_config(session, settings)
    assert cfg.api_key == "pg_key_dddddddd-dddd-4ddd-8ddd-dddddddddddd"


@pytest.mark.asyncio
async def test_connection_without_api_key(session):
    settings = _settings(PASARGUARD_API_KEY="")
    ok, msg = await check_panel_connection(session, settings)
    assert ok is False
    assert "API key" in msg or "api" in msg.lower()


@pytest.mark.asyncio
async def test_legacy_username_password_ignored(session):
    """Columns may still hold old values; effective config must ignore them."""
    settings = _settings(PASARGUARD_API_KEY="pg_key_envvvvv-envv-4env-8env-envvenvvenvv")
    row = PasarGuardSettings(
        id=1,
        admin_username="legacy-admin",
        admin_password="legacy-secret",
        api_key=None,
    )
    session.add(row)
    await session.commit()
    cfg = await get_effective_pasarguard_config(session, settings)
    assert cfg.api_key.startswith("pg_key_env")
    assert not hasattr(cfg, "admin_username")


# --------------------------------------------------------------------------- #
# Trial shape lives beside the panel that provisions it
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_trial_device_limit_falls_back_to_env(session):
    settings = _settings(TRIAL_HWID_LIMIT=2)
    assert await get_trial_hwid_limit(session, settings) == 2


@pytest.mark.asyncio
async def test_trial_device_limit_saves_with_no_settings_row_present(session):
    """The «ذخیره شد» lie, guarded.

    Clearing group ids and timeout used to write nothing at all when no settings
    row existed yet, while still reporting success. The trial write goes
    through `_ensure_row` so the *first* edit on a fresh deployment sticks.
    Size and length moved to each store group's trial; the device count is
    the trial setting left here.
    """
    settings = _settings(TRIAL_HWID_LIMIT=1)
    assert await session.get(PasarGuardSettings, 1) is None

    await save_trial_shape(session, hwid_limit=3)
    await session.commit()

    assert await get_trial_hwid_limit(session, settings) == 3


@pytest.mark.asyncio
async def test_trial_device_limit_clears_back_to_env(session):
    settings = _settings(TRIAL_HWID_LIMIT=1)
    await save_trial_shape(session, hwid_limit=3)
    await session.commit()
    await save_trial_shape(session, clear="hwid_limit")
    await session.commit()
    assert await get_trial_hwid_limit(session, settings) == 1


@pytest.mark.asyncio
async def test_clear_to_env_drops_the_trial_override(session):
    settings = _settings(TRIAL_HWID_LIMIT=1)
    await save_trial_shape(session, hwid_limit=3)
    await session.commit()

    await clear_to_env(session)
    await session.commit()

    assert await get_trial_hwid_limit(session, settings) == 1


@pytest.mark.asyncio
async def test_unknown_trial_field_is_refused(session):
    with pytest.raises(ValueError):
        await save_trial_shape(session, clear="not_a_field")
