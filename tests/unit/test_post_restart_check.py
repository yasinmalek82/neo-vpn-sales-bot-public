"""Release checks cannot manufacture a DB or silently bypass effective panel config."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import create_async_engine

from api_client.client import PasarGuardClient
from database.models.pasarguard_settings import PasarGuardSettings
from scripts import post_restart_check as check


def _settings(path: Path, **kwargs):
    return SimpleNamespace(database_url=f"sqlite+aiosqlite:///{path}", debug=False, **kwargs)


def _database(path: Path, revision: str | None = None) -> None:
    with sqlite3.connect(path) as db:
        if revision is not None:
            db.execute("CREATE TABLE alembic_version(version_num VARCHAR(32) PRIMARY KEY)")
            db.execute("INSERT INTO alembic_version VALUES (?)", (revision,))


def _head() -> str:
    return ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()


async def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing" / "bot.db"
    message, ok = await check._check_db(_settings(path))
    assert not ok
    assert not path.parent.exists()
    assert "FAIL" in message


@pytest.mark.parametrize("revision", [None, "outdated_revision"])
async def test_empty_or_old_database_cannot_pass(tmp_path, revision):
    path = tmp_path / "bot.db"
    _database(path, revision)
    before = path.read_bytes()
    message, ok = await check._check_db(_settings(path))
    assert not ok
    assert "FAIL" in message
    assert path.read_bytes() == before


async def test_current_database_passes_without_mutation(tmp_path):
    path = tmp_path / "a database.db"
    _database(path, _head())
    before = path.read_bytes()
    message, ok = await check._check_db(_settings(path))
    assert ok
    assert _head() in message
    assert path.read_bytes() == before


async def test_missing_explicit_panel_config_fails(tmp_path):
    path = tmp_path / "bot.db"
    _database(path, _head())
    engine = create_async_engine(_settings(path).database_url)
    async with engine.begin() as conn:
        await conn.run_sync(PasarGuardSettings.__table__.create)
    await engine.dispose()
    settings = _settings(
        path,
        pasar_guard_api_key="",
        pasar_guard_base_url="https://panel.example.com",
        pasar_guard_sub_path="sub",
        pasar_guard_timeout=2,
    )
    message, ok = await check._probe_panel(settings)
    assert not ok
    assert "FAIL" in message


async def test_panel_uses_db_override_and_releases_db_before_network(tmp_path, monkeypatch):
    path = tmp_path / "bot.db"
    _database(path, _head())
    engine = create_async_engine(_settings(path).database_url)
    async with engine.begin() as conn:
        await conn.run_sync(PasarGuardSettings.__table__.create)
        await conn.execute(
            PasarGuardSettings.__table__.insert().values(
                id=1,
                base_url="https://db-panel.test:8000",
                api_key="pg_key_db_test",
                sub_path="sub",
                timeout=2,
            )
        )
    await engine.dispose()
    requests = []

    def transport(request):
        requests.append(request)
        assert str(request.url) == "https://db-panel.test:8000/api/admin"
        assert request.headers["X-Api-Key"] == "pg_key_db_test"
        # A still-open read transaction would prevent an exclusive writer.
        with sqlite3.connect(path, timeout=0) as db:
            db.execute("BEGIN EXCLUSIVE")
            db.rollback()
        return httpx.Response(401, json={"detail": "private-credential-fragment"})

    def factory(cfg):
        return PasarGuardClient(
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            client=httpx.AsyncClient(
                base_url=cfg.base_url, transport=httpx.MockTransport(transport)
            ),
        )

    monkeypatch.setattr("api_client.factory.create_pasarguard_client_from_config", factory)
    settings = _settings(
        path,
        pasar_guard_api_key="",
        pasar_guard_base_url="https://panel.example.com",
        pasar_guard_sub_path="sub",
        pasar_guard_timeout=2,
    )
    message, ok = await check._probe_panel(settings)
    assert len(requests) == 1
    assert not ok
    assert "private-credential-fragment" not in message
    assert "pg_key_db_test" not in message


async def test_failures_never_print_raw_config_or_network_errors(monkeypatch, capsys):
    import bot.config

    secret = "private-credential-fragment"
    monkeypatch.setattr(bot.config, "Settings", lambda: (_ for _ in ()).throw(ValueError(secret)))
    assert await check._async_main() == 1
    assert secret not in capsys.readouterr().out


async def test_default_command_reports_panel_not_run_and_never_probes(
    tmp_path, monkeypatch, capsys
):
    import bot.config

    path = tmp_path / "bot.db"
    _database(path, _head())
    settings = _settings(path, run_mode="polling")
    monkeypatch.setattr(bot.config, "Settings", lambda: settings)

    async def no_network(_settings):
        raise AssertionError("default command must not contact panel")

    monkeypatch.setattr(check, "_probe_panel", no_network)
    assert await check._async_main() == 0
    output = capsys.readouterr().out
    assert "panel NOT RUN" in output
    assert "Telegram UI/monitoring NOT VERIFIED" in output


async def test_bad_db_prevents_even_explicit_network_probe(tmp_path, monkeypatch):
    import bot.config

    path = tmp_path / "missing.db"
    monkeypatch.setattr(bot.config, "Settings", lambda: _settings(path, run_mode="polling"))

    async def no_network(_settings):
        raise AssertionError("cannot probe before the DB target is verified")

    monkeypatch.setattr(check, "_probe_panel", no_network)
    assert await check._async_main(probe_panel=True) == 1
    assert not path.exists()
