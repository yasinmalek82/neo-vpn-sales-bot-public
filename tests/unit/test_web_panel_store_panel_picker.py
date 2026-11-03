"""The live panel-group picker on a group's «🖥 پنل» section.

The owner used to type ids («11, 12»); now a button shows the panel's own groups as
checkboxes. What these defend, each with a test that goes red when the real line breaks:

* the fragment lists the panel's groups with the group's own ones checked, a chosen id
  the panel no longer lists is shown checked and marked, and a disabled one is marked;
* saving through the picker leaves exactly the state, log and refusal the typed box
  leaves for the same ids (the premium field, a stale ``version``, services that would
  be stranded and an empty choice included): the server cannot tell the two forms apart;
* the panel not answering (an error, a timeout, no key, an empty list) is the bot's one
  line with the typed box left alone, never a 500, and nothing is written;
* the page itself never asks the panel, the new GET never writes, and it is guarded as
  the store's other owner routes are;
* the read snapshot is let go before the panel is awaited;
* the bot's picker and the web read the same function and show the same rows.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiohttp import FormData
from aiohttp.test_utils import TestClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api_client.exceptions import PasarGuardError
from api_client.models import GroupResponse, GroupSimple, GroupsResponse, GroupsSimpleResponse
from bot.handlers import store_admin as ha
from bot.keyboards.store_admin import StoreCB, store_panel_group_picker_keyboard
from bot.texts import texts
from database.base import Base
from services import panel_group_list
from services.store_admin import MAX_PANEL_GROUPS, get_catalog_view
from services.store_edit import TEXT_STALE
from tests.unit.test_web_panel_store_settings import (
    _bot_settings,
    _callback,
    _fsm,
    _groups,
    _live_services,
    _log,
    _owner_settings,
    _section,
    _seed,
    _snapshot,
    _version,
    _version_rows,
)
from tests.unit.web_panel_helpers import (
    ENV_ADMIN,
    INIT_HEADER,
    STRANGER,
    init_data,
    panel_client,
    panel_settings,
)
from tests.unit.web_personal_helpers import make_person, personal_client

Factory = tuple[async_sessionmaker[AsyncSession], str]
OWNER = {INIT_HEADER: init_data(ENV_ADMIN)}
MAIN, PREMIUM = "panel-groups", "premium-groups"


# --------------------------------------------------------------------------- #
# A fake panel that lists groups
# --------------------------------------------------------------------------- #


class FakeGroupsPanel:
    """Stands in for ``PasarGuardClient``: ``list_groups`` answers, fails or hangs as told."""

    def __init__(
        self,
        groups: list[tuple[int, str, bool]] | None = None,
        *,
        error: Exception | None = None,
        simple_only: bool = False,
        probe: Callable[[], int] | None = None,
    ) -> None:
        self.groups = groups or []
        self.error = error
        self.simple_only = simple_only
        self.probe = probe
        self.calls: list[str] = []
        self.checked_out_during_call: list[int] = []
        self.closed = False

    def _enter(self, name: str) -> None:
        self.calls.append(name)
        if self.probe is not None:
            self.checked_out_during_call.append(self.probe())

    async def list_groups(self, **_kw: Any) -> GroupsResponse:
        self._enter("list_groups")
        if self.error is not None:
            raise self.error
        if self.simple_only:
            from api_client.exceptions import PasarGuardAuthError

            raise PasarGuardAuthError("forbidden", status_code=403)
        rows = [GroupResponse(id=i, name=n, is_disabled=off) for i, n, off in self.groups]
        return GroupsResponse(groups=rows, total=len(rows))

    async def list_groups_simple(self, **_kw: Any) -> GroupsSimpleResponse:
        self._enter("list_groups_simple")
        rows = [GroupSimple(id=i, name=n) for i, n, _off in self.groups]
        return GroupsSimpleResponse(groups=rows, total=len(rows))

    async def aclose(self) -> None:
        self.closed = True


PANEL = [
    (11, "tunnel-a", False),
    (12, "tunnel-b", False),
    (22, "direct", True),
    (14, "spare", False),
]


def use_groups_panel(monkeypatch: pytest.MonkeyPatch, panel: FakeGroupsPanel) -> FakeGroupsPanel:
    async def fake_client(*_a: Any, **_kw: Any) -> FakeGroupsPanel:
        return panel

    monkeypatch.setattr(panel_group_list, "get_pasarguard_client", fake_client)
    return panel


def no_panel_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """What the real factory does when no key is configured: the client will not be built."""

    async def broken(*_a: Any, **_kw: Any) -> Any:
        raise ValueError("PasarGuard api_key is required (non-empty)")

    monkeypatch.setattr(panel_group_list, "get_pasarguard_client", broken)


# --------------------------------------------------------------------------- #
# Fixtures and small helpers
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def seeded(
    two_sessions: Factory,
) -> Any:
    factory, _ = two_sessions
    await _seed(factory)
    yield factory, await _groups(factory)


@pytest_asyncio.fixture
async def twin(two_sessions: Factory, tmp_path: Path) -> Any:
    """The picker's database and a second one for the typed box, seeded the same."""
    picker_factory, _ = two_sessions
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'typed_side.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    typed_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _seed(picker_factory)
        await _seed(typed_factory)
        yield picker_factory, typed_factory
    finally:
        await engine.dispose()


def _pick_url(slug: str, which: str = MAIN, query: str = "") -> str:
    return f"/panel/store/g/{slug}/settings/{which}/pick{query}"


async def _open(
    client: TestClient, slug: str, which: str = MAIN, *, query: str = "", headers: Any = OWNER
) -> tuple[int, str]:
    resp = await client.get(_pick_url(slug, which, query), headers=headers)
    return resp.status, await resp.text()


ROW = re.compile(
    r'<label class="check"><input type="checkbox" name="ids" value="(\d+)"( checked)?>'
    r'<span>(.*?)</span><span class="lat">(\d+)</span></label>'
)


def _rows(html: str) -> list[tuple[int, bool, str]]:
    """``[(id, checked, label)]`` in the order the fragment lists them."""
    out = []
    for value, checked, label, shown in ROW.findall(html):
        assert value == shown, "the id in the muted span is the checkbox's value"
        out.append((int(value), bool(checked), label))
    return out


async def _submit(client: TestClient, html: str, chosen: list[int]) -> tuple[int, str]:
    """What the browser sends when the fragment's form is submitted with ``chosen`` checked."""
    form = re.search(r'<form class="subset" hx-post="([^"]+)"', html)
    version = re.search(r'name="version" value="([^"]*)"', html)
    assert form and version, html
    data = FormData()
    data.add_field("version", version.group(1))
    for gid in chosen:
        data.add_field("ids", str(gid))
    resp = await client.post(form.group(1), data=data, headers=OWNER)
    return resp.status, await resp.text()


async def _typed(
    client: TestClient, slug: str, which: str, ids: str, *, version: int | None
) -> tuple[int, str]:
    data = {"ids": ids}
    if version is not None:
        data["version"] = str(version)
    resp = await client.post(f"/panel/store/g/{slug}/settings/{which}", data=data, headers=OWNER)
    return resp.status, await resp.text()


# --------------------------------------------------------------------------- #
# What the fragment shows
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_fragment_lists_the_panels_groups_with_the_groups_own_checked(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, slugs = seeded  # ویژه sells from panel group 11
    panel = use_groups_panel(monkeypatch, FakeGroupsPanel(PANEL))
    async with panel_client(factory, _owner_settings()) as client:
        status, html = await _open(client, slugs["ویژه"])
    assert status == 200
    assert [(gid, checked) for gid, checked, _ in _rows(html)] == [
        (11, True),
        (12, False),
        (22, False),
        (14, False),
    ]
    assert "tunnel-a" in html and "tunnel-b" in html
    assert ">22<" in html and "direct ⛔" in html, "a disabled group is marked as in the bot"
    assert panel.calls == ["list_groups"] and panel.closed
    assert 'hx-post="/panel/store/g/' + slugs["ویژه"] + '/settings/panel-groups"' in html
    assert "ذخیره" in html and "checkbox" in html


@pytest.mark.asyncio
async def test_the_premium_fragment_checks_the_premium_groups_not_the_main_ones(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, slugs = seeded  # ویژه: main 11, premium 21
    use_groups_panel(
        monkeypatch, FakeGroupsPanel([(11, "a", False), (21, "premium-a", False), (22, "b", False)])
    )
    async with panel_client(factory, _owner_settings()) as client:
        status, html = await _open(client, slugs["ویژه"], PREMIUM)
    assert status == 200
    assert [(gid, c) for gid, c, _ in _rows(html)] == [(11, False), (21, True), (22, False)]
    assert 'hx-post="/panel/store/g/' + slugs["ویژه"] + '/settings/premium-groups"' in html


@pytest.mark.asyncio
async def test_an_id_the_panel_no_longer_lists_is_shown_checked_and_marked(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, slugs = seeded  # ویژه has 11; the panel lists only 12 and 22
    use_groups_panel(monkeypatch, FakeGroupsPanel([(12, "tunnel-b", False), (22, "direct", True)]))
    async with panel_client(factory, _owner_settings()) as client:
        _, html = await _open(client, slugs["ویژه"])
    rows = _rows(html)
    assert [(gid, c) for gid, c, _ in rows] == [(12, False), (22, False), (11, True)]
    missing = next(label for gid, _, label in rows if gid == 11)
    assert texts.admin_store_pick_missing == "در پنل نیست"
    assert missing.startswith(texts.admin_store_pick_missing)


@pytest.mark.asyncio
async def test_a_key_that_may_only_read_the_simple_list_still_gets_a_picker(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, slugs = seeded
    panel = use_groups_panel(monkeypatch, FakeGroupsPanel(PANEL, simple_only=True))
    async with panel_client(factory, _owner_settings()) as client:
        status, html = await _open(client, slugs["ویژه"])
    assert status == 200 and panel.calls == ["list_groups", "list_groups_simple"]
    assert [gid for gid, _, _ in _rows(html)] == [11, 12, 22, 14]


@pytest.mark.asyncio
async def test_the_fragment_carries_the_version_of_the_page_it_was_asked_from(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, slugs = seeded
    use_groups_panel(monkeypatch, FakeGroupsPanel(PANEL))
    version = await _version(factory)
    async with panel_client(factory, _owner_settings()) as client:
        _, same = await _open(client, slugs["ویژه"], query=f"?version={version}")
        _, older = await _open(client, slugs["ویژه"], query=f"?version={(version or 0) - 1}")
        _, none = await _open(client, slugs["ویژه"])
        _, junk = await _open(client, slugs["ویژه"], query="?version=abc")
    assert f'name="version" value="{version}"' in same
    assert f'name="version" value="{(version or 0) - 1}"' in older
    assert f'name="version" value="{version}"' in none, "no version asked: the current one"
    assert f'name="version" value="{version}"' in junk


# --------------------------------------------------------------------------- #
# The panel not answering: the bot's one line, the typed box stays, nothing written
# --------------------------------------------------------------------------- #


def _panel_failures() -> list[tuple[str, Callable[[pytest.MonkeyPatch], FakeGroupsPanel | None]]]:
    def error(mp: pytest.MonkeyPatch) -> FakeGroupsPanel:
        return use_groups_panel(mp, FakeGroupsPanel(error=PasarGuardError("panel said no")))

    def timeout(mp: pytest.MonkeyPatch) -> FakeGroupsPanel:
        return use_groups_panel(mp, FakeGroupsPanel(error=TimeoutError("slow")))

    def empty(mp: pytest.MonkeyPatch) -> FakeGroupsPanel:
        return use_groups_panel(mp, FakeGroupsPanel([]))

    def unconfigured(mp: pytest.MonkeyPatch) -> None:
        no_panel_key(mp)

    return [("error", error), ("timeout", timeout), ("empty", empty), ("no key", unconfigured)]


@pytest.mark.asyncio
@pytest.mark.parametrize("which", [MAIN, PREMIUM])
@pytest.mark.parametrize(
    ("name", "arrange"), _panel_failures(), ids=["error", "timeout", "empty", "no_key"]
)
async def test_a_panel_that_does_not_answer_is_one_line_and_nothing_else(
    seeded, monkeypatch: pytest.MonkeyPatch, name: str, arrange: Any, which: str
) -> None:
    factory, slugs = seeded
    panel = arrange(monkeypatch)
    before, rows = await _snapshot(factory), await _version_rows(factory)
    async with panel_client(factory, _owner_settings()) as client:
        status, html = await _open(client, slugs["ویژه"], which)
        page = await client.get(f"/panel/store/g/{slugs['ویژه']}", headers=OWNER)
        page_html = await page.text()
    assert status == 200, name
    assert texts.web_store_pick_unreachable in html
    assert "بفرست" not in html, "the page asks to type the ids, the bot asks to send them"
    assert "<form" not in html and "checkbox" not in html and "<button" not in html
    assert html.count("<p") == 1, "one line"
    assert 'name="ids"' in page_html, "the typed box is still on the page"
    assert await _snapshot(factory) == before and await _version_rows(factory) == rows
    if panel is not None:
        assert panel.closed, "the client is closed even when the call failed"


# --------------------------------------------------------------------------- #
# Parity: the picker's form leaves what the typed box leaves
# --------------------------------------------------------------------------- #


async def _picker_save(
    factory: Any, slug: str, which: str, chosen: list[int], mp: pytest.MonkeyPatch
) -> tuple[int, str]:
    use_groups_panel(mp, FakeGroupsPanel(PANEL + [(21, "premium-a", False)]))
    async with panel_client(factory, _owner_settings()) as client:
        _, html = await _open(client, slug, which, query=f"?version={await _version(factory)}")
        return await _submit(client, html, chosen)


async def _typed_save(factory: Any, slug: str, which: str, ids: str) -> tuple[int, str]:
    async with panel_client(factory, _owner_settings()) as client:
        return await _typed(client, slug, which, ids, version=await _version(factory))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("group", "which", "chosen", "typed"),
    [
        ("ویژه", MAIN, [11, 12], "11, 12"),
        ("ساده", MAIN, [14], "14"),
        ("ویژه", PREMIUM, [21, 22], "21, 22"),
        ("ویژه", PREMIUM, [], ""),  # tiers go off: the same extra line
        ("ساده", MAIN, [], ""),  # off sale: the same warning
    ],
)
async def test_saving_through_the_picker_leaves_what_the_typed_box_leaves(
    twin, monkeypatch: pytest.MonkeyPatch, group: str, which: str, chosen: list[int], typed: str
) -> None:
    web_factory, typed_factory = twin
    slugs = await _groups(web_factory)
    before = await _snapshot(web_factory)
    picked_status, picked_html = await _picker_save(
        web_factory, slugs[group], which, chosen, monkeypatch
    )
    typed_status, typed_html = await _typed_save(typed_factory, slugs[group], which, typed)
    assert picked_status == typed_status == 200, picked_html
    after = await _snapshot(web_factory)
    assert after != before, "the write must have changed something for the comparison to mean much"
    assert after == await _snapshot(typed_factory)
    assert _section(picked_html, "panel") == _section(typed_html, "panel")
    assert texts.admin_store_saved in picked_html


@pytest.mark.asyncio
async def test_the_picker_is_refused_where_the_typed_box_is_refused_for_stranded_services(
    twin, monkeypatch: pytest.MonkeyPatch
) -> None:
    web_factory, typed_factory = twin
    for factory in (web_factory, typed_factory):
        await _live_services(factory, "50 گیگ", 3)
    slugs = await _groups(web_factory)
    before, rows = await _snapshot(web_factory), await _version_rows(web_factory)
    status, html = await _picker_save(web_factory, slugs["ویژه"], MAIN, [14], monkeypatch)
    typed_status, typed_html = await _typed_save(typed_factory, slugs["ویژه"], MAIN, "14")
    assert status == typed_status == 400
    assert "۳ سرویس فعال را روی گروه قبلی جا می‌گذارد" in _section(html, "panel")
    assert _section(html, "panel") == _section(typed_html, "panel")
    assert await _snapshot(web_factory) == before == await _snapshot(typed_factory)
    assert await _version_rows(web_factory) == rows, "and not even a draft is left behind"


@pytest.mark.asyncio
async def test_the_picker_that_strands_nothing_saves_like_the_typed_box(
    twin, monkeypatch: pytest.MonkeyPatch
) -> None:
    web_factory, typed_factory = twin
    for factory in (web_factory, typed_factory):
        await _live_services(factory, "50 گیگ", 3)
    slugs = await _groups(web_factory)
    # the same ids again, and a group no service sits in
    same, _ = await _picker_save(web_factory, slugs["ویژه"], MAIN, [11], monkeypatch)
    other, _ = await _picker_save(web_factory, slugs["ساده"], MAIN, [14], monkeypatch)
    assert (same, other) == (200, 200)
    await _typed_save(typed_factory, slugs["ویژه"], MAIN, "11")
    await _typed_save(typed_factory, slugs["ساده"], MAIN, "14")
    assert await _snapshot(web_factory) == await _snapshot(typed_factory)


@pytest.mark.asyncio
async def test_a_picker_drawn_before_another_edit_is_refused_like_a_stale_typed_form(
    twin, monkeypatch: pytest.MonkeyPatch
) -> None:
    web_factory, typed_factory = twin
    slugs = await _groups(web_factory)
    use_groups_panel(monkeypatch, FakeGroupsPanel(PANEL))
    drawn_at = await _version(web_factory)
    async with panel_client(web_factory, _owner_settings()) as client:
        _, html = await _open(client, slugs["ویژه"], query=f"?version={drawn_at}")
        # another tab edits the group meanwhile
        status, _ = await _typed(client, slugs["ساده"], MAIN, "14", version=drawn_at)
        assert status == 200 and await _version(web_factory) != drawn_at
        after_other_edit = await _snapshot(web_factory)
        picked_status, picked_html = await _submit(client, html, [12])
    async with panel_client(typed_factory, _owner_settings()) as client:
        await _typed(client, slugs["ساده"], MAIN, "14", version=drawn_at)
        typed_status, _ = await _typed(client, slugs["ویژه"], MAIN, "12", version=drawn_at)
    assert picked_status == typed_status == 409
    assert TEXT_STALE in picked_html
    assert await _snapshot(web_factory) == after_other_edit, "nothing was merged"


@pytest.mark.asyncio
async def test_checkboxes_go_through_the_same_parser_and_refuse_what_it_refuses(seeded) -> None:
    factory, slugs = seeded
    before, rows = await _snapshot(factory), await _version_rows(factory)
    version = await _version(factory)
    url = f"/panel/store/g/{slugs['ویژه']}/settings/panel-groups"
    too_many = [str(i) for i in range(1, MAX_PANEL_GROUPS + 2)]
    async with panel_client(factory, _owner_settings()) as client:
        for values in (["11", "x"], ["0"], ["11", "²"], too_many):
            data = FormData()
            data.add_field("version", str(version))
            for v in values:
                data.add_field("ids", v)
            resp = await client.post(url, data=data, headers=OWNER)
            assert resp.status == 400, values
    assert await _snapshot(factory) == before and await _version_rows(factory) == rows


@pytest.mark.asyncio
async def test_the_typed_box_with_one_value_is_read_exactly_as_before(seeded) -> None:
    factory, slugs = seeded
    async with panel_client(factory, _owner_settings()) as client:
        status, _ = await _typed(
            client, slugs["ساده"], MAIN, "14، 15 14", version=await _version(factory)
        )
    assert status == 200
    async with factory() as s:
        view = await get_catalog_view(s)
    assert next(c for c in view.categories if c.slug == slugs["ساده"]).panel_group_ids == [14, 15]


# --------------------------------------------------------------------------- #
# The page itself, the GET, and who may ask
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_settings_page_never_asks_the_panel_and_offers_the_button(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, slugs = seeded
    panel = use_groups_panel(monkeypatch, FakeGroupsPanel(PANEL))
    version = await _version(factory)
    async with panel_client(factory, _owner_settings()) as client:
        for slug in slugs.values():
            resp = await client.get(f"/panel/store/g/{slug}", headers=OWNER)
            assert resp.status == 200
            html = await resp.text()
            section = _section(html, "panel")
            assert 'name="ids"' in section, "the typed box stays"
            assert section.count("نمایش گروه‌های پنل") == (2 if slug == slugs["ویژه"] else 1)
            assert (
                f'hx-get="/panel/store/g/{slug}/settings/panel-groups/pick?version={version}"'
                in section
            )
            assert ("premium-groups/pick" in section) == (slug == slugs["ویژه"])
            assert "checkbox" not in section, "no list until it is asked for"
    assert panel.calls == [], "drawing the settings page makes no panel call"


@pytest.mark.asyncio
async def test_a_non_owner_admin_sees_no_picker_button(seeded) -> None:
    factory, slugs = seeded
    async with panel_client(factory, panel_settings()) as client:
        resp = await client.get(f"/panel/store/g/{slugs['ویژه']}", headers=OWNER)
        html = await resp.text()
    assert resp.status == 200 and "/pick" not in html and "نمایش گروه‌های پنل" not in html


@pytest.mark.asyncio
async def test_asking_for_the_picker_writes_nothing(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, slugs = seeded
    before, rows, log = await _snapshot(factory), await _version_rows(factory), await _log(factory)
    for arrange in (
        lambda mp: use_groups_panel(mp, FakeGroupsPanel(PANEL)),
        lambda mp: use_groups_panel(mp, FakeGroupsPanel(error=PasarGuardError("x"))),
        no_panel_key,
    ):
        arrange(monkeypatch)
        async with panel_client(factory, _owner_settings()) as client:
            for slug in slugs.values():
                for which in (MAIN, PREMIUM):
                    await _open(client, slug, which)
    assert await _snapshot(factory) == before
    assert await _version_rows(factory) == rows and await _log(factory) == log


@pytest.mark.asyncio
async def test_the_read_snapshot_is_let_go_before_the_panel_is_awaited(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLAUDE.md: no read snapshot across the network (it dies with «database is locked»)."""
    factory, slugs = seeded
    held = {"n": 0}
    sync_engine = factory.kw["bind"].sync_engine
    event.listen(sync_engine, "checkout", lambda *_a: held.__setitem__("n", held["n"] + 1))
    event.listen(sync_engine, "checkin", lambda *_a: held.__setitem__("n", held["n"] - 1))
    panel = use_groups_panel(monkeypatch, FakeGroupsPanel(PANEL, probe=lambda: held["n"]))
    async with panel_client(factory, _owner_settings()) as client:
        status, _ = await _open(client, slugs["ویژه"])
    assert status == 200 and panel.calls
    assert panel.checked_out_during_call == [0], "a connection was still held while the panel ran"


@pytest.mark.asyncio
async def test_nobody_but_a_store_owner_gets_the_picker_and_the_panel_is_not_asked(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, slugs = seeded
    panel = use_groups_panel(monkeypatch, FakeGroupsPanel(PANEL))
    slug = slugs["ویژه"]
    async with panel_client(factory, _owner_settings()) as client:
        for which in (MAIN, PREMIUM):
            # unsigned, and signed by someone who is not an admin
            assert (await client.get(_pick_url(slug, which))).status == 401
            stranger = {INIT_HEADER: init_data(STRANGER)}
            assert (await client.get(_pick_url(slug, which), headers=stranger)).status == 403
    # an admin who is not a store owner reads the page but gets the owner line here
    async with panel_client(factory, panel_settings()) as client:
        for which in (MAIN, PREMIUM):
            status, html = await _open(client, slug, which)
            assert status == 403 and texts.admin_store_owner_only in html
            assert "checkbox" not in html
    assert panel.calls == []


@pytest.mark.asyncio
async def test_a_customer_and_a_reseller_are_refused_the_picker(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, slugs = seeded
    panel = use_groups_panel(monkeypatch, FakeGroupsPanel(PANEL))
    await make_person(factory, 4001, name="مشتری")
    await make_person(factory, 4002, name="نماینده", reseller=True)
    async with personal_client(factory, public=True, settings=_owner_settings()) as client:
        for tg in (4001, 4002):
            headers = {INIT_HEADER: init_data(tg)}
            for which in (MAIN, PREMIUM):
                status, html = await _open(client, slugs["ویژه"], which, headers=headers)
                assert status == 403 and "checkbox" not in html, (tg, which)
    assert panel.calls == []


@pytest.mark.asyncio
async def test_an_unknown_group_and_a_hidden_premium_control_are_refused_before_the_panel(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, slugs = seeded
    panel = use_groups_panel(monkeypatch, FakeGroupsPanel(PANEL))
    async with panel_client(factory, _owner_settings()) as client:
        status, html = await _open(client, "no-such-group")
        assert status == 404 and "گروه محصول یافت نشد." in html
        # «نامحدود» has no tiers, so the premium control is not on its page
        status, html = await _open(client, slugs["نامحدود"], PREMIUM)
        assert status == 409 and TEXT_STALE in html
        # the main control is always there
        status, _ = await _open(client, slugs["نامحدود"], MAIN)
        assert status == 200
    assert panel.calls == ["list_groups"], "only the one valid request reached the panel"


@pytest.mark.asyncio
async def test_the_only_get_routes_under_the_settings_are_the_two_pickers(seeded) -> None:
    """A GET route added under a group's settings later must be named (and guarded) here."""
    factory, slugs = seeded
    async with panel_client(factory, _owner_settings()) as client:
        gets = {
            r.resource.canonical
            for r in client.app.router.routes()
            if r.method == "GET"
            and r.resource
            and r.resource.canonical.startswith("/panel/store/g/{group_slug}/settings")
        }
        # the write routes of the same fields still answer a GET with 405, as before
        for which in (MAIN, PREMIUM):
            resp = await client.get(
                f"/panel/store/g/{slugs['ویژه']}/settings/{which}", headers=OWNER
            )
            assert resp.status == 405
    assert gets == {
        "/panel/store/g/{group_slug}/settings/panel-groups/pick",
        "/panel/store/g/{group_slug}/settings/premium-groups/pick",
    }


# --------------------------------------------------------------------------- #
# The bot's picker and the web read one function and show the same rows
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_bots_picker_and_the_web_show_the_same_rows(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drives the bot's real handler (not its fetch) against the same fake panel."""
    factory, slugs = seeded
    use_groups_panel(monkeypatch, FakeGroupsPanel([(12, "tunnel-b", False), (22, "direct", True)]))
    async with panel_client(factory, _owner_settings()) as client:
        _, html = await _open(client, slugs["ویژه"])  # 11 is chosen and the panel lacks it
    web = [(gid, checked, label) for gid, checked, label in _rows(html)]

    screen = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    monkeypatch.setattr(ha, "_tg_message", lambda _cb: screen)
    async with factory() as s:
        view = await get_catalog_view(s)
        cat_id = int(next(c.id for c in view.categories if c.slug == slugs["ویژه"]))
        await ha.store_cat_edit_groups(  # type: ignore[arg-type]
            _callback(f"{StoreCB.CAT_GROUPS}{cat_id}"), _bot_settings(), None, s, _fsm()
        )
    markup = screen.edit_text.await_args.kwargs["reply_markup"]
    buttons = [
        b.text
        for row in markup.inline_keyboard
        for b in row
        if (b.callback_data or "").startswith(StoreCB.GROUP_PICK + str(cat_id))
        and not (b.callback_data or "").startswith(StoreCB.GROUP_PICK_SAVE)
    ]
    assert len(buttons) == len(web) == 3
    for text, (gid, checked, label) in zip(buttons, web, strict=True):
        assert text == f"{'✅' if checked else '▫️'} {label.replace(' ⛔', '')} • #{gid}" + (
            " ⛔" if label.endswith("⛔") else ""
        )


def test_the_bots_keyboard_still_reads_its_rows_from_the_shared_function() -> None:
    markup = store_panel_group_picker_keyboard(5, [(11, "a", False), (12, "b", True)], [12, 99])
    labels = [b.text for row in markup.inline_keyboard for b in row][:3]
    assert labels == ["▫️ a • #11", "✅ b • #12 ⛔", "✅ در پنل نیست • #99 ⛔"]


def test_picker_rows_adds_every_chosen_id_the_panel_dropped_once_each() -> None:
    rows = panel_group_list.picker_rows([(1, "a", False)], [1, 7, 8])
    assert rows == [
        (1, "a", False),
        (7, texts.admin_store_pick_missing, True),
        (8, texts.admin_store_pick_missing, True),
    ]
