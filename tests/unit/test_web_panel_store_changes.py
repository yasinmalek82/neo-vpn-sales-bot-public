"""The change log and its undo on the Mini App (1d of the store step).

What these defend, each with a test that goes red when the real line is broken:

* the page lists the entries the bot's «تغییرات فروشگاه» lists, in the same order, with
  the same notes (any admin may read; only a store owner gets an undo control);
* the confirm page says how many later changes go back with the entry (the sentence the
  bot's confirm says) and writes nothing; only the second request undoes;
* an undo on the web leaves the catalog, the log and the version the bot's real handler
  leaves, and undoing the undo restores the state before it;
* an entry that was pruned, or a form drawn before another edit, is refused as stale and
  writes nothing; a double-submitted confirm applies once;
* the POST route obeys the store's one guard (login, admin, owner, version, no GET);
* a note carries the owner's own typing (a plan title): the page escapes it;
* a reseller price keyed on a plan slug still resolves after an undo that re-mints rows.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.handlers import store_admin as ha
from bot.keyboards.store_admin import StoreCB
from bot.texts import texts
from database.base import Base
from database.models.store import StorePackage, StoreVersion
from scheduler.jobs.cleanup import _prune_archived_store_versions
from services.reseller import quote_ready_plan, set_plan_price
from services.store_admin import get_catalog_view
from services.store_changes import (
    TEXT_NOT_UNDOABLE,
    change_log_text,
    undo_confirm_text,
)
from services.store_dashboard import retail_cell
from services.store_edit import TEXT_STALE
from services.store_pricing import DEFAULT_TIER
from services.store_strategy import list_store_changes
from services.ux_format import to_persian_digits
from tests.unit.test_web_panel_store_plans import _full, _plan_slug
from tests.unit.test_web_panel_store_settings import (
    _bot_settings,
    _callback,
    _fsm,
    _groups,
    _log,
    _owner_settings,
    _seed,
    _version,
    _version_rows,
)
from tests.unit.web_panel_helpers import (
    ENV_ADMIN,
    INIT_HEADER,
    STRANGER,
    add_user,
    init_data,
    panel_client,
    panel_settings,
)

OWNER = {INIT_HEADER: init_data(ENV_ADMIN)}
Factory = async_sessionmaker[AsyncSession]
CHANGES = "/panel/store/changes"
UNDO = "/panel/store/changes/{}/undo"
EVIL = "<b>x</b>"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def seeded(two_sessions: tuple[Factory, str]) -> Any:
    factory, _ = two_sessions
    await _seed(factory)
    yield factory, await _groups(factory)


@pytest_asyncio.fixture
async def twin(two_sessions: tuple[Factory, str], tmp_path: Any) -> Any:
    """The web's database and a second one for the bot's path, seeded the same."""
    web_factory, _ = two_sessions
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bot_side.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    bot_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _seed(web_factory)
        await _seed(bot_factory)
        yield web_factory, bot_factory
    finally:
        await engine.dispose()


async def _post(
    client: TestClient,
    url: str,
    form: dict[str, str],
    *,
    version: int | None,
    headers: dict[str, str] = OWNER,
) -> tuple[int, str]:
    data = dict(form)
    if version is not None:
        data["version"] = str(version)
    resp = await client.post(url, data=data, headers=headers)
    return resp.status, await resp.text()


async def _history(factory: Factory, *, extra: int = 0, title: str = EVIL) -> None:
    """Edits through the panel itself: a price, a switch, a new plan (title typed), a limit."""
    groups = await _groups(factory)
    p50 = await _plan_slug(factory, "50 گیگ")
    p100 = await _plan_slug(factory, "100 گیگ")
    vip = f"/panel/store/g/{groups['ویژه']}/plans"
    steps = [
        (f"{vip}/{p50}/price", {"price": "160000"}),
        (f"{vip}/{p100}/toggle/renew", {}),
        (
            f"/panel/store/g/{groups['ساده']}/plans",
            {"size": "20", "duration": "30", "hwid": "0", "price": "90000", "title": title},
        ),
        (f"{vip}/{p50}/hwid", {"hwid": "3"}),
    ]
    steps += [(f"{vip}/{p50}/price", {"price": str(161_000 + i)}) for i in range(extra)]
    async with panel_client(factory, _owner_settings()) as client:
        for url, form in steps:
            status, body = await _post(client, url, form, version=await _version(factory))
            assert status == 200, body


async def _entries(factory: Factory) -> list[Any]:
    async with factory() as s:
        return await list_store_changes(s)


async def _get(client: TestClient, url: str, headers: dict[str, str] = OWNER) -> tuple[int, str]:
    resp = await client.get(url, headers=headers)
    return resp.status, await resp.text()


def _rows(page: str) -> list[tuple[str, str, str]]:
    """(marker, note, when) per row of the page, as printed."""
    found = re.findall(
        r'<span class="mark"[^>]*>(.*?)</span>\s*<span class="desc">(.*?)</span>'
        r'\s*<span class="when num">(.*?)</span>',
        page,
        re.S,
    )
    return [(m, html_lib.unescape(d), html_lib.unescape(w)) for m, d, w in found]


def _confirm_text(page: str) -> str:
    match = re.search(r'<p class="note confirm-text"[^>]*>(.*?)</p>', page, re.S)
    assert match, page
    return html_lib.unescape(match.group(1))


def _bot_screen(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture what the bot's handlers send instead of editing a Telegram message."""
    sent: list[str] = []

    async def fake(_message: Any, body: str, _kb: Any) -> None:
        sent.append(body)

    monkeypatch.setattr(ha, "_edit_or_answer", fake)
    monkeypatch.setattr(ha, "_tg_message", lambda _cb: object())
    return sent


async def _bot_log(factory: Factory) -> None:
    async with factory() as s:
        await ha.store_changes(_callback(StoreCB.HIST), _bot_settings(), None, s, _fsm())


async def _bot_ask(factory: Factory, version_id: int) -> None:
    async with factory() as s:
        await ha.store_change_undo_ask(
            _callback(f"{StoreCB.HIST_REST}{version_id}"), _bot_settings(), None, s
        )


async def _bot_undo(factory: Factory, version_id: int) -> None:
    cb = _callback(f"{StoreCB.HIST_OK}{version_id}")
    async with factory() as s:
        await ha.store_history_restore_ok(cb, _bot_settings(), None, s)
    cb.answer.assert_awaited()


# --------------------------------------------------------------------------- #
# The log is the bot's log
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_web_log_lists_the_entries_the_bots_screen_lists(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, _ = seeded
    await _history(factory)
    sent = _bot_screen(monkeypatch)
    await _bot_log(factory)
    body = html_lib.unescape(sent[0]).split(texts.admin_store_changes_hint)[0]
    bot_lines = [line for line in body.splitlines() if line[:1] in ("●", "○")]
    bot_rows = []
    for line in bot_lines:
        marker, rest = line.split(" ", 1)
        note, when = rest.rsplit(" — ", 1)
        bot_rows.append((marker, note, to_persian_digits(when)))
    async with panel_client(factory, _owner_settings()) as client:
        status, page = await _get(client, CHANGES)
    assert status == 200
    web_rows = _rows(page)
    assert web_rows == bot_rows
    # pinned as literals: newest first, the current state marked, the notes the owner reads
    assert [r[1] for r in web_rows[:5]] == [
        "کاربر همزمان پلن عوض شد",
        f"پلن «{EVIL}» ساخته شد",
        "تنظیمات پلن عوض شد",
        "پلن ویرایش شد",
        "seed",
    ]
    assert web_rows[0][0] == "●" and {r[0] for r in web_rows[1:]} == {"○"}
    assert web_rows[0][2] == "همین الان"
    assert texts.admin_store_changes_hint in html_lib.unescape(page)


@pytest.mark.asyncio
async def test_the_log_text_the_bot_prints_is_the_one_function_the_page_uses(seeded) -> None:
    factory, _ = seeded
    await _history(factory)
    entries = await _entries(factory)
    text = change_log_text(entries)
    assert text.startswith(texts.admin_store_changes_title)
    assert text.endswith(texts.admin_store_changes_hint)
    listed = text.split(texts.admin_store_changes_hint)[0].splitlines()
    assert len([line for line in listed if line[:1] in ("●", "○")]) == len(entries)
    assert change_log_text([]) == texts.admin_store_changes_empty


@pytest.mark.asyncio
async def test_any_admin_reads_the_log_but_only_an_owner_gets_an_undo_control(seeded) -> None:
    factory, _ = seeded
    await _history(factory)
    async with panel_client(factory, _owner_settings()) as client:
        _, owner_page = await _get(client, CHANGES)
    async with panel_client(factory, panel_settings()) as client:  # admin, not a store owner
        status, admin_page = await _get(client, CHANGES)
        assert status == 200
        assert (await client.get(CHANGES)).status == 401
        assert (await client.get(CHANGES, headers={INIT_HEADER: init_data(STRANGER)})).status == 403
    assert _rows(admin_page) == _rows(owner_page)
    assert "/undo" in owner_page
    assert "/undo" not in admin_page and "(فقط خواندنی)" in admin_page


@pytest.mark.asyncio
async def test_the_oldest_state_has_no_undo_and_an_empty_log_says_so(seeded) -> None:
    factory, _ = seeded
    entries = await _entries(factory)
    assert len(entries) == 1 and not entries[0].can_undo
    async with panel_client(factory, _owner_settings()) as client:
        _, page = await _get(client, CHANGES)
        assert "/undo" not in page
        version = await _version(factory)
        status, body = await _post(client, UNDO.format(entries[0].version_id), {}, version=version)
    assert status == 409 and TEXT_NOT_UNDOABLE in body


@pytest.mark.asyncio
async def test_the_store_screen_links_to_the_log(seeded) -> None:
    factory, groups = seeded
    async with panel_client(factory, _owner_settings()) as client:
        _, page = await _get(client, f"/panel/store/g/{groups['ویژه']}")
    assert 'hx-get="/panel/store/changes"' in page


@pytest.mark.asyncio
async def test_looking_at_the_log_writes_nothing(seeded) -> None:
    factory, _ = seeded
    await _history(factory)
    before, rows = await _full(factory), await _version_rows(factory)
    async with panel_client(factory, _owner_settings()) as client:
        for _ in range(2):
            assert (await _get(client, CHANGES))[0] == 200
    assert await _full(factory) == before and await _version_rows(factory) == rows


# --------------------------------------------------------------------------- #
# The confirm step: the bot's sentence, the count, nothing written
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_confirm_page_states_how_many_later_changes_go_back_and_writes_nothing(
    twin, monkeypatch: pytest.MonkeyPatch
) -> None:
    web_f, bot_f = twin
    await _history(web_f)
    await _history(bot_f)
    entries = await _entries(web_f)
    middle = entries[2]
    assert middle.undoes_also == 2
    before, rows = await _full(web_f), await _version_rows(web_f)
    async with panel_client(web_f, _owner_settings()) as client:
        status, page = await _post(
            client, UNDO.format(middle.version_id), {}, version=await _version(web_f)
        )
    assert status == 200
    text = _confirm_text(page)
    assert "فروشگاه به قبل از این تغییر برگرده؟" in text
    assert "حواست باشه: ۲ تغییر بعدی هم با این کار برمی‌گرده" in text
    assert f"«{middle.description}»" in text
    assert text == undo_confirm_text(middle)
    # the same sentence the bot's confirm screen sends for the same entry
    sent = _bot_screen(monkeypatch)
    await _bot_ask(bot_f, (await _entries(bot_f))[2].version_id)
    assert html_lib.unescape(sent[0]) == text
    # nothing was written by asking
    assert await _full(web_f) == before and await _version_rows(web_f) == rows
    # the confirm button is the second request: same version, confirm=1
    assert 'name="confirm" value="1"' in page and texts.admin_store_undo_button in page


@pytest.mark.asyncio
async def test_the_newest_change_says_nothing_else_goes_back(seeded) -> None:
    factory, _ = seeded
    await _history(factory)
    newest = (await _entries(factory))[0]
    async with panel_client(factory, _owner_settings()) as client:
        _, page = await _post(
            client, UNDO.format(newest.version_id), {}, version=await _version(factory)
        )
    assert "حواست باشه" not in _confirm_text(page)


# --------------------------------------------------------------------------- #
# Parity: the web undo and the bot's real handler leave the same state
# --------------------------------------------------------------------------- #


def _catalog(snap: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in snap.items() if k not in ("log", "version")}


@pytest.mark.asyncio
async def test_a_web_undo_leaves_the_state_log_and_version_the_bots_handler_leaves(
    twin, monkeypatch: pytest.MonkeyPatch
) -> None:
    web_f, bot_f = twin
    await _history(web_f)
    await _history(bot_f)
    _bot_screen(monkeypatch)
    assert await _full(web_f) == await _full(bot_f)
    first = await _full(web_f)
    version = await _version(web_f)

    # undo the price edit (third from the top) on both sides
    target_web = (await _entries(web_f))[3]
    target_bot = (await _entries(bot_f))[3]
    assert target_web.description == target_bot.description == "پلن ویرایش شد"
    async with panel_client(web_f, _owner_settings()) as client:
        status, page = await _post(
            client,
            UNDO.format(target_web.version_id),
            {"confirm": "1"},
            version=version,
        )
    assert status == 200
    await _bot_undo(bot_f, target_bot.version_id)
    after = await _full(web_f)
    assert after == await _full(bot_f)
    assert after["version"] == (version or 0) + 1
    assert _catalog(after) != _catalog(first)
    # the undo is its own entry, with the note the bot writes, on top of the log
    assert after["log"][0] == "برگشت: پلن ویرایش شد"
    assert after["log"][1:] == first["log"]
    assert "برگشت: پلن ویرایش شد" in html_lib.unescape(page)
    assert texts.admin_store_undone in page

    # the entry right under the undo is the state before the undo
    top_web = (await _entries(web_f))[0]
    top_bot = (await _entries(bot_f))[0]
    assert top_web.description == "برگشت: پلن ویرایش شد" and top_web.is_current
    async with panel_client(web_f, _owner_settings()) as client:
        status, _ = await _post(
            client,
            UNDO.format(top_web.version_id),
            {"confirm": "1"},
            version=await _version(web_f),
        )
    assert status == 200
    await _bot_undo(bot_f, top_bot.version_id)
    again = await _full(web_f)
    assert again == await _full(bot_f)
    assert _catalog(again) == _catalog(first), "undoing the undo restores the state before it"
    assert again["log"][0] == "برگشت: برگشت: پلن ویرایش شد"


@pytest.mark.asyncio
async def test_after_an_undo_the_store_screen_shows_the_restored_catalog(seeded) -> None:
    factory, groups = seeded
    await _history(factory)
    price_edit = (await _entries(factory))[3]
    async with factory() as s:
        view = await get_catalog_view(s)
        p50 = next(p for p in view.packages if p.title_fa == "50 گیگ")
        assert retail_cell(list(view.prices), p50) == 160_000
    async with panel_client(factory, _owner_settings()) as client:
        status, _ = await _post(
            client,
            UNDO.format(price_edit.version_id),
            {"confirm": "1"},
            version=await _version(factory),
        )
        assert status == 200
        status, page = await _get(client, f"/panel/store/g/{groups['ویژه']}")
    assert status == 200
    async with factory() as s:
        view = await get_catalog_view(s)
        p50 = next(p for p in view.packages if p.title_fa == "50 گیگ")
        assert retail_cell(list(view.prices), p50) == 150_000
    assert "150" in page.replace(",", "") or "۱۵۰" in page


# --------------------------------------------------------------------------- #
# Stale: pruned, another edit landed, a double submit
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_entry_pruned_meanwhile_is_refused_as_stale_and_nothing_is_written(seeded) -> None:
    factory, _ = seeded
    await _history(factory, extra=4)  # nine versions: eight archived, five kept
    entries = await _entries(factory)
    oldest = entries[-1]
    assert oldest.description == "seed"
    old = datetime.now(UTC) - timedelta(days=30)
    async with factory() as s:
        await s.execute(update(StoreVersion).values(published_at=old, updated_at=old))
        await s.commit()
        await _prune_archived_store_versions(s)
        await s.commit()
    assert oldest.version_id not in {e.version_id for e in await _entries(factory)}
    before, rows = await _full(factory), await _version_rows(factory)
    async with panel_client(factory, _owner_settings()) as client:
        version = await _version(factory)
        for form in ({}, {"confirm": "1"}):
            status, body = await _post(
                client, UNDO.format(oldest.version_id), form, version=version
            )
            assert status == 409 and texts.admin_store_change_gone in body
        # an id that never existed is the same refusal, never another entry
        status, body = await _post(client, UNDO.format(10**9), {"confirm": "1"}, version=version)
        assert status == 409 and texts.admin_store_change_gone in body
    assert await _full(factory) == before and await _version_rows(factory) == rows


@pytest.mark.asyncio
async def test_a_form_drawn_before_another_edit_is_refused_and_nothing_is_merged(seeded) -> None:
    factory, groups = seeded
    await _history(factory)
    target = (await _entries(factory))[2]
    drawn_at = await _version(factory)
    p100 = await _plan_slug(factory, "100 گیگ")
    async with panel_client(factory, _owner_settings()) as client:
        # the bot (or another tab) edits meanwhile
        status, _ = await _post(
            client,
            f"/panel/store/g/{groups['ویژه']}/plans/{p100}/price",
            {"price": "271000"},
            version=drawn_at,
        )
        assert status == 200
        before, rows = await _full(factory), await _version_rows(factory)
        for form in ({}, {"confirm": "1"}):
            status, body = await _post(
                client, UNDO.format(target.version_id), form, version=drawn_at
            )
            assert status == 409 and TEXT_STALE in body
    assert await _full(factory) == before and await _version_rows(factory) == rows


@pytest.mark.asyncio
async def test_the_same_confirm_sent_twice_undoes_once(seeded) -> None:
    factory, _ = seeded
    await _history(factory)
    target = (await _entries(factory))[3]
    version = await _version(factory)
    log_before = len(await _log(factory))
    async with panel_client(factory, _owner_settings()) as client:
        first = await _post(
            client, UNDO.format(target.version_id), {"confirm": "1"}, version=version
        )
        after_first = await _full(factory)
        second = await _post(
            client, UNDO.format(target.version_id), {"confirm": "1"}, version=version
        )
    assert (first[0], second[0]) == (200, 409), second[1]
    assert TEXT_STALE in second[1]
    assert await _full(factory) == after_first
    assert len(await _log(factory)) == log_before + 1


@pytest.mark.asyncio
async def test_two_confirms_at_the_same_moment_undo_once(seeded) -> None:
    factory, _ = seeded
    await _history(factory)
    target = (await _entries(factory))[3]
    version = await _version(factory)
    log_before = len(await _log(factory))
    async with panel_client(factory, _owner_settings()) as client:
        results = await asyncio.gather(
            _post(client, UNDO.format(target.version_id), {"confirm": "1"}, version=version),
            _post(client, UNDO.format(target.version_id), {"confirm": "1"}, version=version),
        )
    assert sorted(status for status, _ in results) == [200, 409]
    assert len(await _log(factory)) == log_before + 1


@pytest.mark.asyncio
async def test_an_entry_keeps_its_id_when_later_edits_land(seeded) -> None:
    """The panel names an entry by its version row: publishing archives it in place."""
    factory, groups = seeded
    await _history(factory)
    entries = await _entries(factory)
    ids = {e.description + str(e.version): e.version_id for e in entries}
    p100 = await _plan_slug(factory, "100 گیگ")
    async with panel_client(factory, _owner_settings()) as client:
        status, _ = await _post(
            client,
            f"/panel/store/g/{groups['ویژه']}/plans/{p100}/price",
            {"price": "271000"},
            version=await _version(factory),
        )
    assert status == 200
    later = {e.description + str(e.version): e.version_id for e in await _entries(factory)}
    assert all(later[key] == value for key, value in ids.items())


# --------------------------------------------------------------------------- #
# The guard
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("form", [{}, {"confirm": "1"}], ids=["ask", "confirm"])
async def test_the_undo_needs_login_admin_owner_and_the_current_version(
    seeded, form: dict[str, str]
) -> None:
    factory, _ = seeded
    await _history(factory)
    url = UNDO.format((await _entries(factory))[2].version_id)
    version = await _version(factory)
    before, rows = await _full(factory), await _version_rows(factory)
    async with panel_client(factory, _owner_settings()) as client:
        assert (await client.post(url, data={**form, "version": str(version)})).status == 401
        stranger = {INIT_HEADER: init_data(STRANGER)}
        assert (
            await client.post(url, data={**form, "version": str(version)}, headers=stranger)
        ).status == 403
        assert (await client.get(url, headers=OWNER)).status == 405
        assert (await client.get(f"{url}?version={version}&confirm=1", headers=OWNER)).status == 405
        status, body = await _post(client, url, form, version=(version or 0) - 1)
        assert status == 409 and TEXT_STALE in body
        status, _ = await _post(client, url, form, version=None)
        assert status == 409, "no version at all is as stale as a wrong one"
        status, _ = await _post(client, url, form, version=(version or 0) + 1)
        assert status == 409, "a version from the future names nothing either"
    assert await _full(factory) == before and await _version_rows(factory) == rows
    async with panel_client(factory, panel_settings()) as client:  # admin, not an owner
        status, body = await _post(client, url, form, version=version)
        assert status == 403 and texts.admin_store_owner_only in body
    assert await _full(factory) == before and await _version_rows(factory) == rows


@pytest.mark.asyncio
async def test_a_position_in_the_list_is_not_an_entry(seeded) -> None:
    """The route takes a number; ``0`` or ``1`` is a row id that is nobody's entry, not 'the newest'."""
    factory, _ = seeded
    await _history(factory)
    before = await _full(factory)
    async with panel_client(factory, _owner_settings()) as client:
        version = await _version(factory)
        ids = {e.version_id for e in await _entries(factory)}
        for position in (0, 1, 2):
            if position in ids:
                continue
            status, body = await _post(
                client, UNDO.format(position), {"confirm": "1"}, version=version
            )
            assert status == 409 and texts.admin_store_change_gone in body
        status, _ = await _post(client, UNDO.format("abc"), {"confirm": "1"}, version=version)
        assert status == 404
    assert await _full(factory) == before


@pytest.mark.asyncio
async def test_a_version_id_past_64_bits_in_the_undo_url_is_a_404(seeded) -> None:
    factory, version = seeded
    before = await _full(factory)
    big = "9" * 30
    async with panel_client(factory, _owner_settings()) as client:
        status, _ = await _post(client, UNDO.format(big), {"confirm": "1"}, version=version)
        assert status == 404
    assert await _full(factory) == before


@pytest.mark.asyncio
async def test_the_undo_route_is_the_only_new_post_under_changes(seeded) -> None:
    from tests.unit.test_web_panel_store_plans import PLAN_CANONICALS

    factory, _ = seeded
    async with panel_client(factory, _owner_settings()) as client:
        posts = {
            r.resource.canonical
            for r in client.app.router.routes()
            if r.method == "POST" and r.resource and "/changes" in r.resource.canonical
        }
        gets = {
            r.resource.canonical
            for r in client.app.router.routes()
            if r.method == "GET" and r.resource and "/changes" in r.resource.canonical
        }
    assert posts == {"/panel/store/changes/{version_id}/undo"} == posts & PLAN_CANONICALS
    assert gets == {"/panel/store/changes"}


# --------------------------------------------------------------------------- #
# The owner's own typing in a note
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_title_with_html_in_a_note_is_escaped_on_the_log_and_the_confirm_page(
    seeded,
) -> None:
    factory, _ = seeded
    await _history(factory, title="<script>alert(1)</script>")
    entries = await _entries(factory)
    created = next(e for e in entries if "ساخته شد" in e.description)
    assert "<script>" in created.description, "the note really carries the typed title"
    async with panel_client(factory, _owner_settings()) as client:
        _, log = await _get(client, CHANGES)
        _, ask = await _post(
            client, UNDO.format(created.version_id), {}, version=await _version(factory)
        )
        _, done = await _post(
            client,
            UNDO.format(created.version_id),
            {"confirm": "1"},
            version=await _version(factory),
        )
    for page in (log, ask, done):
        assert "<script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in log
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in ask
    # the undo's own note quotes it too, and the log after the undo escapes that
    assert "&lt;script&gt;" in done and "<script>" not in done


# --------------------------------------------------------------------------- #
# Reseller prices are keyed on the plan slug
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_reseller_price_keyed_on_the_slug_still_resolves_after_an_undo(seeded) -> None:
    factory, _ = seeded
    await add_user(factory, 777)
    slug = await _plan_slug(factory, "50 گیگ")
    async with factory() as s:
        from sqlalchemy import select as _select

        from database.models import User

        shop = (await s.execute(_select(User).where(User.telegram_id == 777))).scalar_one()
        await set_plan_price(s, shop.id, slug, 120_000, duration_days=30, tier=DEFAULT_TIER)
        await s.commit()
        shop_id = int(shop.id)
    await _history(factory)
    price_edit = (await _entries(factory))[3]

    async def package_ids() -> set[int]:
        async with factory() as s:
            return {
                int(i)
                for i in await s.scalars(select(StorePackage.id).where(StorePackage.slug == slug))
            }

    ids_before = await package_ids()
    async with panel_client(factory, _owner_settings()) as client:
        status, _ = await _post(
            client,
            UNDO.format(price_edit.version_id),
            {"confirm": "1"},
            version=await _version(factory),
        )
    assert status == 200
    async with factory() as s:
        view = await get_catalog_view(s)
        pkg = next(p for p in view.packages if str(p.slug) == slug)
        retail = retail_cell(list(view.prices), pkg)
        assert pkg.id not in ids_before, "the undo mints new row ids; only the slug survives"
        quote = await quote_ready_plan(
            s,
            user_id=shop_id,
            package_slug=slug,
            retail_price_toman=int(retail or 0),
            duration_days=30,
            tier=DEFAULT_TIER,
        )
    assert retail == 150_000
    assert quote.reseller_price_toman == 120_000 and quote.price_toman == 120_000
