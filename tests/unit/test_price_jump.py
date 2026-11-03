"""A price 10 times the current one, or a tenth of it, is asked about once.

A warning, never a refusal, and one function (``services/store_edit.py::set_package_price``)
behind both the panel's price cell and the bot's typed price. Defended here:

* the rule's two boundaries are exact (at the boundary asks, one toman short does not);
* no current price means nothing to compare with;
* the panel asks with the old and the new price, writes nothing while asking, and
  saves once on the second request;
* the bot asks the same thing in a confirm step (FSM state, a keyboard), saves on «بله»
  and a second tap saves nothing more.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.handlers import store_admin as ha
from bot.keyboards.store_admin import StoreCB
from bot.states import AdminStates
from bot.texts import texts
from services import store_edit
from services.store_admin import get_catalog_view
from services.store_dashboard import retail_cell
from services.store_edit import PRICE_JUMP_FACTOR, price_jump, set_package_price
from tests.unit.test_web_panel_store_settings import (
    _bot_settings,
    _callback,
    _fsm,
    _log,
    _owner_settings,
    _seed,
    _snapshot,
    _typed,
    _version,
    _version_rows,
)
from tests.unit.web_panel_helpers import ENV_ADMIN, INIT_HEADER, init_data, panel_client

Factory = tuple[async_sessionmaker[AsyncSession], str]
OWNER = {INIT_HEADER: init_data(ENV_ADMIN)}
OLD = 150_000


@pytest_asyncio.fixture
async def seeded(two_sessions: Factory) -> Any:
    factory, _ = two_sessions
    await _seed(factory)
    async with factory() as s:
        view = await get_catalog_view(s)
        plan = next(p for p in view.packages if p.title_fa == "50 گیگ")
        groups = {c.title_fa: str(c.slug) for c in view.categories}
        assert retail_cell(list(view.prices), plan) == OLD
        yield factory, groups["ویژه"], str(plan.slug), int(plan.id)


def test_the_factor_is_a_named_constant_of_ten() -> None:
    assert PRICE_JUMP_FACTOR == 10


@pytest.mark.parametrize(
    ("old", "new", "asks"),
    [
        (OLD, OLD * 10, True),  # exactly ten times
        (OLD, OLD * 10 - 1, False),  # one toman short of it
        (OLD, OLD * 10 + 1, True),
        (OLD, OLD // 10, True),  # exactly a tenth
        (OLD, OLD // 10 + 1, False),  # one toman above a tenth
        (OLD, OLD // 10 - 1, True),
        (OLD, OLD, False),
        (OLD, OLD + 1, False),
        (OLD, 1, True),
        (15, 150, True),
        (15, 149, False),
        (15, 2, False),  # 1/10 of 15 is 1.5: 2 is above it
        (15, 1, True),
        (None, 5_000_000, False),  # no current price: nothing to compare with
        (0, 5_000_000, False),
    ],
)
def test_the_boundaries_of_the_rule(old: int | None, new: int, asks: bool) -> None:
    assert price_jump(old, new) is asks


# --------------------------------------------------------------------------- #
# The panel
# --------------------------------------------------------------------------- #


async def _post_price(
    client: Any, group: str, plan: str, price: str, *, version: int | None, confirm: bool = False
) -> tuple[int, str]:
    data = {"price": price, "version": str(version)}
    if confirm:
        data["confirm"] = "1"
    resp = await client.post(f"/panel/store/g/{group}/plans/{plan}/price", data=data, headers=OWNER)
    return resp.status, await resp.text()


@pytest.mark.asyncio
async def test_the_panel_asks_with_the_old_and_new_price_and_writes_nothing_while_asking(
    seeded,
) -> None:
    factory, group, plan, _ = seeded
    before = await _snapshot(factory)
    rows = await _version_rows(factory)
    async with panel_client(factory, _owner_settings()) as client:
        status, html = await _post_price(
            client, group, plan, "1500000", version=await _version(factory)
        )
    assert status == 200
    text = html.replace("&#39;", "'")
    assert "قیمت فعلی: ۱۵۰٬۰۰۰ تومان" in text
    assert "قیمت تازه: ۱٬۵۰۰٬۰۰۰ تومان" in text
    assert 'name="confirm" value="1"' in html and 'name="price" value="1500000"' in html
    assert await _snapshot(factory) == before, "asking saved nothing and logged nothing"
    assert await _version_rows(factory) == rows, "asking did not leave a draft either"


@pytest.mark.asyncio
async def test_the_second_request_saves_once(seeded) -> None:
    factory, group, plan, _ = seeded
    async with panel_client(factory, _owner_settings()) as client:
        version = await _version(factory)
        first, _ = await _post_price(client, group, plan, "1500000", version=version, confirm=True)
        second, html = await _post_price(
            client, group, plan, "1500000", version=version, confirm=True
        )
    assert (first, second) == (200, 409)
    assert (await _log(factory))[0] == "پلن ویرایش شد"
    assert len([line for line in await _log(factory) if line == "پلن ویرایش شد"]) == 1
    async with factory() as s:
        view = await get_catalog_view(s)
    assert retail_cell(list(view.prices), next(p for p in view.packages if p.slug == plan)) == (
        1_500_000
    )
    assert texts.admin_store_saved in html or "فروشگاه در این فاصله" in html


@pytest.mark.asyncio
@pytest.mark.parametrize("price", ["1499999", "15001", "300000", "٣٠٠٠٠٠"])
async def test_a_price_inside_the_band_saves_without_asking(seeded, price: str) -> None:
    factory, group, plan, _ = seeded
    async with panel_client(factory, _owner_settings()) as client:
        status, html = await _post_price(
            client, group, plan, price, version=await _version(factory)
        )
    assert status == 200 and 'name="confirm"' not in html
    assert (await _log(factory))[0] == "پلن ویرایش شد"


@pytest.mark.asyncio
@pytest.mark.parametrize("price", ["1500000", "15000"])
async def test_the_boundary_asks_on_the_panel(seeded, price: str) -> None:
    factory, group, plan, _ = seeded
    before = await _snapshot(factory)
    async with panel_client(factory, _owner_settings()) as client:
        _status, html = await _post_price(
            client, group, plan, price, version=await _version(factory)
        )
    assert 'name="confirm" value="1"' in html
    assert await _snapshot(factory) == before


@pytest.mark.asyncio
async def test_a_bad_price_is_still_the_invalid_input_line_not_an_ask(seeded) -> None:
    factory, group, plan, _ = seeded
    async with panel_client(factory, _owner_settings()) as client:
        status, html = await _post_price(
            client, group, plan, "abc", version=await _version(factory)
        )
    assert status == 400 and texts.admin_store_invalid_input in html


@pytest.mark.asyncio
async def test_a_plan_with_no_current_price_is_never_asked_about(seeded) -> None:
    from sqlalchemy import delete

    from database.models.store import StorePackagePrice

    factory, group, plan, _ = seeded
    async with factory() as s:
        await s.execute(delete(StorePackagePrice).where(StorePackagePrice.package_slug == plan))
        await s.commit()
    async with panel_client(factory, _owner_settings()) as client:
        status, html = await _post_price(
            client, group, plan, "99999999", version=await _version(factory)
        )
    assert status == 200 and 'name="confirm"' not in html
    assert (await _log(factory))[0] == "پلن ویرایش شد"


# --------------------------------------------------------------------------- #
# The bot
# --------------------------------------------------------------------------- #


async def _type_price(session: AsyncSession, pkg_id: int, text: str) -> tuple[Any, Any]:
    fsm = _fsm()
    await fsm.set_state(AdminStates.store_waiting_pkg_price)
    await fsm.update_data(store_pkg_id=pkg_id)
    typed = _typed(text)
    await ha.store_pkg_fsm_input(typed, fsm, _bot_settings(), None, session)
    return fsm, typed


@pytest.mark.asyncio
async def test_the_bot_asks_in_a_confirm_step_and_saves_on_yes_once(seeded, monkeypatch) -> None:
    factory, _group, plan, pkg_id = seeded
    screen = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    monkeypatch.setattr(ha, "_tg_message", lambda _cb: screen)
    before = await _snapshot(factory)
    async with factory() as s:
        fsm, typed = await _type_price(s, pkg_id, "1500000")
        assert await fsm.get_state() == AdminStates.store_waiting_pkg_price_confirm.state
        ask = typed.answer.await_args
        assert "قیمت فعلی: ۱۵۰٬۰۰۰ تومان" in ask.args[0]
        assert "۱٬۵۰۰٬۰۰۰ تومان" in ask.args[0]
        buttons = [b for row in ask.kwargs["reply_markup"].inline_keyboard for b in row]
        assert StoreCB.PKG_PRICE_OK in [b.callback_data for b in buttons]
    assert await _snapshot(factory) == before, "asking saved nothing"

    async with factory() as s:
        cb = _callback(StoreCB.PKG_PRICE_OK)
        await ha.store_pkg_price_confirm(cb, _bot_settings(), None, fsm, s)
        assert await fsm.get_state() is None
    after = await _snapshot(factory)
    assert after != before and after["log"][0] == "پلن ویرایش شد"  # type: ignore[index]
    async with factory() as s:
        cb2 = _callback(StoreCB.PKG_PRICE_OK)
        await ha.store_pkg_price_confirm(cb2, _bot_settings(), None, fsm, s)
    assert cb2.answer.await_args.kwargs.get("show_alert") is True, "a second tap is a stale button"
    assert await _snapshot(factory) == after, "and saved nothing more"
    async with factory() as s:
        view = await get_catalog_view(s)
    assert retail_cell(list(view.prices), next(p for p in view.packages if p.slug == plan)) == (
        1_500_000
    )


@pytest.mark.asyncio
async def test_the_bot_saves_a_price_inside_the_band_without_the_extra_step(
    seeded, monkeypatch
) -> None:
    factory, _group, _plan, pkg_id = seeded
    async with factory() as s:
        fsm, typed = await _type_price(s, pkg_id, "200000")
        assert await fsm.get_state() is None
    assert (await _log(factory))[0] == "پلن ویرایش شد"
    assert texts.admin_store_saved in typed.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_the_services_default_asks_so_a_caller_cannot_forget(seeded) -> None:
    """Fix the shared function, not each caller: the default is to ask."""
    factory, _group, _plan, pkg_id = seeded
    async with factory() as s:
        with pytest.raises(store_edit.PriceJumpNeedsConfirm):
            await set_package_price(s, pkg_id, "1500000")
        await s.rollback()
        await set_package_price(s, pkg_id, "1500000", confirmed=True)
    assert (await _log(factory))[0] == "پلن ویرایش شد"
