"""Colour and layout rules from `bot/keyboards/style.py`, checked on every keyboard.

Bot API 9.4 gave buttons three colours. Three colours are only information if
the same colour always means the same thing, and BRAND.md §7 is the reason:
«اگه همه‌جا نعنایی باشه، هیچ‌جا تأکید نیست». One accent per screen — otherwise
it is decoration, and decoration that shifts meaning per screen is worse than
plain buttons.

The builders are discovered by reflection rather than listed, so a keyboard
added next month is covered without anyone remembering to add it here.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import typing
from collections.abc import Iterator

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import bot.keyboards
from bot.keyboards import style

Builder = tuple[str, str, InlineKeyboardMarkup]


_SCALARS: dict[object, object] = {bool: True, int: 1, float: 1.0, str: "x"}


def _dummy(annotation: object) -> object:
    """A value the builder will accept, guessed from the annotation.

    Deliberately crude. The aim is to *render* each keyboard, not to render it
    meaningfully — a wrong-but-typed argument still exercises every append.
    """
    origin = typing.get_origin(annotation)
    if origin in (list, set, frozenset, tuple, dict):
        return typing.cast(type, origin)()
    if origin is not None:
        args = typing.get_args(annotation)
        if type(None) in args:
            # `X | None` — None is valid and exercises the absent branch.
            return None
        return _dummy(args[0])
    return _SCALARS.get(annotation)  # type: ignore[arg-type]


def _iter_keyboards() -> Iterator[Builder]:
    for info in pkgutil.iter_modules(bot.keyboards.__path__):
        module = importlib.import_module(f"bot.keyboards.{info.name}")
        for name, fn in vars(module).items():
            if name.startswith("_") or not inspect.isfunction(fn):
                continue
            if fn.__module__ != module.__name__ or inspect.iscoroutinefunction(fn):
                continue
            signature = inspect.signature(fn)
            try:
                hints = typing.get_type_hints(fn)
            except Exception:
                continue
            if hints.get("return") is not InlineKeyboardMarkup:
                continue
            kwargs = {
                param.name: (
                    param.default
                    if param.default is not inspect.Parameter.empty
                    else _dummy(hints.get(param.name, inspect.Parameter.empty))
                )
                for param in signature.parameters.values()
                if param.kind
                in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY, param.POSITIONAL_ONLY)
            }
            try:
                markup = fn(**kwargs)
            except Exception:
                # Needs a real domain object. Those keyboards are covered by
                # their own tests; this sweep is for the ones that render bare.
                continue
            if isinstance(markup, InlineKeyboardMarkup):
                yield module.__name__, name, markup


KEYBOARDS = list(_iter_keyboards())


def _buttons(markup: InlineKeyboardMarkup) -> list[InlineKeyboardButton]:
    return [button for row in markup.inline_keyboard for button in row]


def test_the_sweep_actually_reaches_the_keyboards() -> None:
    """Without this, a reflection bug turns every test below into a no-op."""
    assert len(KEYBOARDS) >= 25, [f"{m}.{n}" for m, n, _ in KEYBOARDS]
    modules = {module for module, _, _ in KEYBOARDS}
    assert {"bot.keyboards.admin", "bot.keyboards.account", "bot.keyboards.main_menu"} <= modules


@pytest.mark.parametrize(
    "module,name,markup", KEYBOARDS, ids=lambda v: v if isinstance(v, str) else ""
)
def test_at_most_one_primary_button_per_screen(
    module: str, name: str, markup: InlineKeyboardMarkup
) -> None:
    primaries = [b.text for b in _buttons(markup) if b.style == style.PRIMARY]
    assert len(primaries) <= 1, f"{module}.{name} has {len(primaries)} accents: {primaries}"


@pytest.mark.parametrize(
    "module,name,markup", KEYBOARDS, ids=lambda v: v if isinstance(v, str) else ""
)
def test_every_style_is_one_the_rules_define(
    module: str, name: str, markup: InlineKeyboardMarkup
) -> None:
    """A style outside the three has no agreed meaning, so it carries none."""
    allowed = {None, style.PRIMARY, style.SUCCESS, style.DANGER}
    unknown = {b.style for b in _buttons(markup)} - allowed
    assert not unknown, f"{module}.{name} uses {unknown}"


def test_turning_colours_off_removes_every_style() -> None:
    """`BUTTON_COLORS_ENABLED=false` must be a complete off switch.

    A builder that passes a style constant directly instead of through
    `styled()` would keep its colour here, and the escape hatch would be a lie
    on launch day — which is the only day it exists for.
    """
    style.set_enabled(False)
    try:
        for module, name, markup in _iter_keyboards():
            styles = {b.style for b in _buttons(markup)}
            assert styles <= {None}, f"{module}.{name} stayed coloured: {styles}"
    finally:
        style.set_enabled(True)


def test_the_off_switch_test_would_notice_a_colour() -> None:
    """Proves the assertion above can fail — it is checking a set of Nones."""
    style.set_enabled(True)
    try:
        coloured = [
            f"{m}.{n}"
            for m, n, markup in _iter_keyboards()
            if any(b.style is not None for b in _buttons(markup))
        ]
        assert coloured, "no keyboard renders a colour at all — the sweep proves nothing"
    finally:
        style.set_enabled(True)
