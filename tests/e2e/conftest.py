"""Fixtures for the end-to-end suite: ``app`` is the whole bot, wired to fakes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Generator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from tests.e2e.harness.app import App, running_app
from tests.e2e.harness.db import build_template


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "smoke(rows): the LIVE_SMOKE_FA checklist rows a scenario proves"
    )


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The schema from ``alembic upgrade head``, built once and copied per test."""
    return build_template(tmp_path_factory.mktemp("e2e_template"))


@pytest_asyncio.fixture
async def app(
    migrated_template: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[App]:
    async with running_app(migrated_template, tmp_path, monkeypatch) as running:
        yield running


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, Any, Any]:
    """After a scenario passes, make sure the bot did not break a rule silently.

    Done here and not in fixture teardown so a failure is a failure of the test
    (and a strict xfail can claim it), not an error at teardown.
    """
    result = yield
    running = getattr(item, "funcargs", {}).get("app")
    if isinstance(running, App):
        running.final_checks()
    return result
