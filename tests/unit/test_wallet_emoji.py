"""The wallet is 🏦 everywhere the bot shows it (owner, 2026-10-03).

Unicode has no plain wallet emoji; the owner picked the bank, which is also the
emoji of the admin «شارژ کیف پول» button. One emoji per concept: the old purse
and briefcase must not come back, and the bank's old job (the card's bank name)
moved to 🏛.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bot.keyboards.reply_menu import (
    WALLET,
    WALLET_LEGACY_LABELS,
    ReplyRoot,
    main_reply_keyboard,
    navigation_root,
)
from bot.texts.fa import PersianTexts
from database.models.report import TOPIC_TITLES_FA, TOPIC_WALLET
from services.ux_format import has_emoji, wallet_menu_label

ROOT = Path(__file__).resolve().parents[2]
RETIRED = ("\U0001f45b", "\U0001f4bc")  # purse, briefcase


def _sources() -> list[Path]:
    return [
        p
        for top in ("bot", "services", "scheduler", "database")
        for p in (ROOT / top).rglob("*.py")
    ]


def test_the_retired_wallet_emoji_appears_nowhere_in_the_code() -> None:
    offenders = [
        f"{p.relative_to(ROOT)}: {emoji!r}"
        for p in _sources()
        for emoji in RETIRED
        if emoji in p.read_text(encoding="utf-8")
    ]
    assert not offenders, offenders


def test_the_scan_sees_the_files_it_is_meant_to_check() -> None:
    names = {p.name for p in _sources()}
    assert {"fa.py", "reply_menu.py", "report.py", "ux_format.py"} <= names


def test_every_wallet_label_in_the_code_carries_the_bank() -> None:
    """An emoji directly before «کیف پول» that is not 🏦 is a second wallet icon."""
    label = re.compile(r"(\S+) (?:تغییر |شارژ )?کیف پول")
    offenders: list[str] = []
    for p in _sources():
        for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            for m in label.finditer(line):
                if has_emoji(m.group(1)) and "\U0001f3e6" not in m.group(1):
                    offenders.append(f"{p.relative_to(ROOT)}:{lineno}: {m.group(0)}")
    assert not offenders, offenders


def test_the_surfaces_that_name_the_wallet_use_the_bank() -> None:
    assert WALLET.startswith("\U0001f3e6")
    assert TOPIC_TITLES_FA[TOPIC_WALLET].startswith("\U0001f3e6")
    assert wallet_menu_label(None).startswith("\U0001f3e6")
    assert wallet_menu_label(1000).startswith("\U0001f3e6")
    assert PersianTexts.wallet_menu.startswith("\U0001f3e6")
    keyboard = main_reply_keyboard()
    assert WALLET in [b.text for row in keyboard.keyboard for b in row]


@pytest.mark.parametrize("label", WALLET_LEGACY_LABELS)
@pytest.mark.parametrize("in_flow", [False, True])
def test_the_old_bottom_button_still_opens_the_wallet(label: str, in_flow: bool) -> None:
    """A chat still showing the old keyboard sends the old label as plain text."""
    assert navigation_root(label, in_flow=in_flow) == ReplyRoot.WALLET
