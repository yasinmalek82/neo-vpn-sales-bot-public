"""Two-session durability harness for crash/rollback tests.

`:memory:` gives every connection its own private database — these tests would
pass for the wrong reason. The default is an on-disk SQLite file.
``DURABILITY_DATABASE_URL`` opts the same fixture onto a disposable server.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import database.models  # noqa: F401 — register mappers
from database.base import Base


async def panel_accepts_then_loses_reply(*_a: object, **_kw: object) -> None:
    """The panel applied the change and the caller never learned."""
    raise TimeoutError("reply lost after the panel applied the change")


@pytest_asyncio.fixture
async def two_sessions(
    tmp_path: Path,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], str]]:
    """A database file plus a factory.

    Each ``async with factory()`` is a separate connection, so a rollback in
    one is visible to the other — which is the whole point.
    """
    # Opt-in only. The default stays a private SQLite file so a developer
    # environment variable cannot point this harness at the customer database.
    override = os.environ.get("DURABILITY_DATABASE_URL")
    if override and "bot.db" in override:
        raise RuntimeError("DURABILITY_DATABASE_URL must not be the customer database")
    if override:
        engine = create_async_engine(override)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            yield factory, override
        finally:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
            await engine.dispose()
        return

    db_path = tmp_path / "durability.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory, url
    finally:
        await engine.dispose()
