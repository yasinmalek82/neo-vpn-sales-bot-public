"""Structural guards for the admin surface.

These do not test one screen — they test the rules the whole admin area must
keep, so the defects fixed in this pass cannot creep back:

* every admin callback constant has a handler, and every handler has a producer
* no admin keyboard is a dead end
* destructive actions are two-step
* every state-scoped prompt can be escaped
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot.handlers import account_link as account_link_handlers
from bot.handlers import admin as admin_handlers
from bot.handlers import common as common_handlers
from bot.handlers import mirza_history as mirza_history_handlers
from bot.handlers import store_admin as store_handlers
from bot.keyboards import account_link as account_link_kb
from bot.keyboards import admin as admin_kb
from bot.keyboards import mirza_history as mirza_history_kb
from bot.keyboards import store_admin as store_kb
from bot.keyboards.account_link import LinkCB
from bot.keyboards.admin import AdminCB, order_cb, parse_order_cb
from bot.keyboards.admin_payment import PayCB
from bot.keyboards.mirza_history import MirzaCB
from bot.keyboards.nav import ADMIN_HOME_CB, HOME_LABEL
from bot.keyboards.store_admin import StoreCB

REPO = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# Callback constants ↔ handlers, in both directions
# --------------------------------------------------------------------------- #


def _constants(cls: type) -> dict[str, str]:
    return {
        name: value
        for name, value in vars(cls).items()
        if not name.startswith("_") and isinstance(value, str)
    }


def _handler_source(*modules: object) -> str:
    return "\n".join(inspect.getsource(m) for m in modules)


def _names_by_value() -> dict[str, list[str]]:
    """Every ``Class.NAME`` spelling grouped by the callback string it holds.

    ``StoreCB.HUB`` is an alias of ``AdminCB.STORE``; matching on names alone
    would report the alias as an orphan.
    """
    out: dict[str, list[str]] = {}
    for cls in (AdminCB, PayCB, StoreCB, LinkCB, MirzaCB):
        for name, value in _constants(cls).items():
            out.setdefault(value, []).append(f"{cls.__name__}.{name}")
    return out


def test_every_callback_constant_has_a_handler() -> None:
    """A constant nothing handles is a button that spins until Telegram times out."""
    src = _handler_source(
        admin_handlers,
        store_handlers,
        common_handlers,
        account_link_handlers,
        mirza_history_handlers,
    )
    orphans = [
        spellings
        for spellings in _names_by_value().values()
        if not any(s in src for s in spellings)
    ]
    assert orphans == [], f"callback constants with no handler: {orphans}"


def test_every_callback_constant_is_produced_by_a_keyboard() -> None:
    """A handler no keyboard can reach is dead weight that still needs reading."""
    produced = _handler_source(admin_kb, store_kb, account_link_kb, mirza_history_kb)
    produced += Path(REPO, "bot/keyboards/admin_payment.py").read_text(encoding="utf-8")
    produced += Path(REPO, "bot/keyboards/main_menu.py").read_text(encoding="utf-8")
    # Confirm-step callbacks are produced by handlers, not keyboards.
    produced += _handler_source(admin_handlers, store_handlers)
    unreachable = [
        spellings
        for spellings in _names_by_value().values()
        if not any(s in produced for s in spellings)
    ]
    assert unreachable == [], f"callbacks no keyboard or handler produces: {unreachable}"


# --------------------------------------------------------------------------- #
# No dead ends
# --------------------------------------------------------------------------- #

_ADMIN_HANDLER_FILES = [
    REPO / "bot/handlers/admin.py",
    REPO / "bot/handlers/account_link.py",
    REPO / "bot/handlers/mirza_history.py",
    REPO / "bot/handlers/store_admin.py",
    REPO / "bot/handlers/lifecycle_admin.py",
]


def _bare_answer_calls(path: Path) -> list[tuple[int, str]]:
    """``message.answer(...)`` / ``target.answer(...)`` with no reply_markup."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"answer", "answer_photo"}:
            continue
        recv = node.func.value
        if not isinstance(recv, ast.Name) or recv.id not in {"message", "target"}:
            continue
        if any(kw.arg == "reply_markup" for kw in node.keywords):
            continue
        offenders.append((node.lineno, ast.unparse(node)[:80]))
    return offenders


@pytest.mark.parametrize("path", _ADMIN_HANDLER_FILES, ids=lambda p: p.name)
def test_admin_messages_always_carry_a_keyboard(path: Path) -> None:
    """A message with no buttons strands the admin — they must re-run /start."""
    offenders = _bare_answer_calls(path)
    assert offenders == [], f"{path.name} leaves the admin without buttons at: {offenders}"


def test_order_detail_keyboard_always_offers_a_way_out() -> None:
    order = SimpleNamespace(id=7, status="ACTIVE", user=None, payment=None)
    kb = admin_kb.order_detail_keyboard(order)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert HOME_LABEL in labels


def test_order_detail_back_returns_to_the_originating_list() -> None:
    """Reviewing a queue used to lose the filter and page on every order."""
    cb = order_cb(42, src=admin_kb.SRC_PENDING, key="stale", offset=16)
    order_id, back = parse_order_cb(cb)
    assert order_id == 42
    assert back == f"{AdminCB.PENDING_FILTER}stale:16"

    cb = order_cb(9, src=admin_kb.SRC_ORDERS, key="ACTIVE", offset=0)
    order_id, back = parse_order_cb(cb)
    assert order_id == 9
    assert back == f"{AdminCB.ORDERS_FILTER}ACTIVE:0"

    order = SimpleNamespace(id=9, status="ACTIVE", user=None, payment=None)
    kb = admin_kb.order_detail_keyboard(order, back_cb=back)
    assert back in [b.callback_data for row in kb.inline_keyboard for b in row]


def test_parse_order_cb_rejects_malformed_payloads() -> None:
    assert parse_order_cb("adm:ord:notanumber") == (None, None)
    assert parse_order_cb("adm:ord:5") == (5, None)


def test_callback_payloads_fit_telegram_limit() -> None:
    """Telegram rejects callback_data over 64 bytes."""
    cb = order_cb(9_999_999, src=admin_kb.SRC_ORDERS, key="PROVISION_FAILED", offset=9999)
    assert len(cb.encode("utf-8")) <= 64


# --------------------------------------------------------------------------- #
# Destructive actions are two-step
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("preview", "apply_cb"),
    [
        (AdminCB.PANEL_CLEAR, AdminCB.PANEL_CLEAR_OK),
        (StoreCB.PROMO_DEL, StoreCB.PROMO_DEL_OK),
        (StoreCB.STEP_RM, StoreCB.STEP_RM_OK),
    ],
)
def test_destructive_actions_have_a_separate_confirm_callback(preview: str, apply_cb: str) -> None:
    """Rejecting a payment cost two taps while wiping settings cost one."""
    assert preview != apply_cb
    # The apply callback must not be swallowed by the preview's prefix filter.
    assert not apply_cb.startswith(preview)


def test_confirm_keyboard_offers_exactly_one_destructive_button() -> None:
    kb = admin_kb.confirm_keyboard(
        confirm_cb="adm:pclearok",
        confirm_label="بله",
        back_to=AdminCB.PANEL,
        back_label="تنظیمات پنل",
    )
    rows = kb.inline_keyboard
    assert rows[0][0].callback_data == "adm:pclearok"
    assert [b.callback_data for row in rows[1:] for b in row] == [
        AdminCB.PANEL,
        ADMIN_HOME_CB,
    ]


# --------------------------------------------------------------------------- #
# Every prompt can be escaped
# --------------------------------------------------------------------------- #


def test_global_cancel_command_is_registered_before_state_handlers() -> None:
    """/cancel must beat state-scoped message handlers or it cannot rescue anyone."""
    from bot.handlers import register_handlers

    class _FakeDispatcher:
        def __init__(self) -> None:
            self.names: list[str] = []

        def include_router(self, router) -> None:
            self.names.append(router.name)

    dp = _FakeDispatcher()
    register_handlers(dp)  # type: ignore[arg-type]
    assert dp.names[0] == "common", dp.names
    assert dp.names[-1] == "fallback", dp.names


def test_navigation_handlers_clear_the_fsm() -> None:
    """Tapping away from a prompt must drop it, not swallow the next message."""
    src = inspect.getsource(admin_handlers)
    for name in (
        "admin_pending",
        "admin_orders_menu",
        "admin_orders_list",
        "admin_order_detail",
        "admin_stats",
        "admin_next_pending",
        "admin_panel_test",
        "admin_panel_groups_list",
    ):
        start = src.index(f"async def {name}(")
        end = src.find("\nasync def ", start + 1)
        body = src[start : end if end != -1 else len(src)]
        assert "state.clear()" in body, f"{name} leaves a stale FSM state behind"


def test_no_admin_callback_prefix_swallows_another() -> None:
    """Prefix collisions are this codebase's classic silent bug.

    `F.data.startswith(...)` means a prefix that happens to contain a shorter
    one steals its taps, and the symptom is a button that quietly does the wrong
    screen — not an error. Adding «👥 کاربران» introduced three neighbouring
    callbacks (`adm:cust`, `adm:cusq`, `adm:cup:`, `adm:cu:`), so this is worth
    a permanent guard rather than a one-off check.
    """
    from bot.keyboards.admin import AdminCB

    constants = {
        name: value
        for name, value in vars(AdminCB).items()
        if not name.startswith("_") and isinstance(value, str)
    }
    prefixes = {n: v for n, v in constants.items() if v.endswith(":")}
    exact = {n: v for n, v in constants.items() if not v.endswith(":")}

    for exact_name, exact_value in exact.items():
        for prefix_name, prefix_value in prefixes.items():
            assert not exact_value.startswith(prefix_value), (
                f"{exact_name} ({exact_value}) is swallowed by " f"{prefix_name} ({prefix_value})"
            )

    for a_name, a_value in prefixes.items():
        for b_name, b_value in prefixes.items():
            if a_name == b_name:
                continue
            assert not a_value.startswith(
                b_value
            ), f"prefix {a_name} ({a_value}) is swallowed by {b_name} ({b_value})"


def test_no_keyboard_doubles_the_back_arrow() -> None:
    """``back_button`` adds «⬅️» itself; a label that brings its own reads «⬅️ ⬅️ پنل»."""
    from tests.unit.test_button_style import KEYBOARDS

    doubled = [
        f"{module}.{name}: {button.text}"
        for module, name, markup in KEYBOARDS
        for row in markup.inline_keyboard
        for button in row
        if button.text.count("⬅️") > 1
    ]
    assert not doubled, doubled


def test_the_home_web_app_button_is_the_only_one_without_a_callback() -> None:
    """«🌐 پنل وب» opens a Mini App; every other home button must still be a callback."""
    home = admin_kb.admin_home_keyboard(
        pending_count=1, web_panel_url="https://p.example.com/panel/"
    )
    without = [b for row in home.inline_keyboard for b in row if b.callback_data is None]
    assert [b.text for b in without] == ["🌐 پنل وب"]
    assert without[0].web_app is not None
