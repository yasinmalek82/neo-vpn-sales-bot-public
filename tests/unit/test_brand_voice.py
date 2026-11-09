"""One voice, enforced.

Before Phase 2 this file's copy switched register exactly where it mattered
most: the browse screens spoke «تو» and the money screens («payment_instructions»,
«payment_receipt_received») switched to «شما». BRAND.md rule 8 names that the
worst of both options, so these tests fail the build rather than let it drift
back one string at a time.

Admin copy is deliberately exempt. Only the owner reads it, and the risk of
rewriting 218 operational strings outweighs the consistency gain.
"""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bot.config import Settings
from bot.texts.fa import PersianTexts
from services.accounts import AccountOverview
from services.brand import (
    SIGNATURE_BRAND_FA,
    get_contact_facts,
    support_line,
    with_support,
)
from services.service_card import build_service_card_caption
from services.ux_format import has_emoji

# A Persian letter on either side means the match is part of a longer word:
# «شماره کارت» must not trip the «شما» rule.
_PERSIAN = r"؀-ۿ‌"

FORMAL_MARKERS = (
    "کنید",
    "بزنید",
    "بفرستید",
    "بمانید",
    "ببینید",
    "بگیرید",
    "نمایید",
    "لطفاً",
    "لطفا",
    "گرامی",
    "عزیز",
    "شما",
    "می‌شود",
    "می‌گردد",
    "می‌دهید",
    "دارید",
    "هستید",
)

SUPERLATIVES = ("بهترین", "قوی‌ترین", "بی‌نظیر", "فوق‌العاده", "بی‌رقیب", "تضمینی")


def customer_strings() -> dict[str, str]:
    """Every string a customer can see. ``admin_*`` is out of scope by decision."""
    return {
        name: value
        for name, value in vars(PersianTexts).items()
        if not name.startswith("_") and not name.startswith("admin_") and isinstance(value, str)
    }


def _count_emoji(text: str) -> int:
    return sum(1 for ch in text if has_emoji(ch))


def test_there_is_something_to_check() -> None:
    """A refactor that empties the collection must not turn these into no-ops."""
    assert len(customer_strings()) > 50


@pytest.mark.parametrize("marker", FORMAL_MARKERS)
def test_no_customer_string_uses_the_formal_register(marker: str) -> None:
    pattern = re.compile(rf"(?<![{_PERSIAN}]){re.escape(marker)}(?![{_PERSIAN}])")
    offenders = [name for name, value in customer_strings().items() if pattern.search(value)]
    assert not offenders, f"«{marker}» in: {offenders}"


@pytest.mark.parametrize("word", SUPERLATIVES)
def test_no_customer_string_makes_an_unprovable_claim(word: str) -> None:
    """BRAND.md rule 5: the customer says these, not us."""
    offenders = [name for name, value in customer_strings().items() if word in value]
    assert not offenders, f"«{word}» in: {offenders}"


# BRAND.md rule 9: promise what we do, not the outcome. «در سریع‌ترین زمان
# ممکن» is deliberately absent: the owner keeps it, next to a concrete promise.
ABSOLUTE_PROMISES = ("بدون قطعی", "قطع نمی‌شه", "قطع نمی‌شود", "۱۰۰٪", "۱۰۰ درصد")


@pytest.mark.parametrize("phrase", ABSOLUTE_PROMISES)
def test_no_customer_string_promises_an_outcome(phrase: str) -> None:
    offenders = [name for name, value in customer_strings().items() if phrase in value]
    assert not offenders, f"«{phrase}» in: {offenders}"


# BRAND.md rule 3, in its mechanical form: **at most one emoji per non-empty
# line.** Nothing caps the whole message: the owner lifted «three per message»
# on 2026-10-03, because a multi-line card reads better when one emoji names
# each line. A one-line message still gets one emoji at most, simply because it
# has one line.
#
# Relevance is the other half of the rule and no test here can check it: 📅 on
# a renewal is information, ✅ on everything is noise. The glossary test in
# `test_customer_copy_consistency.py` pins the concept → emoji pairs instead.
#
# A ✅/❌ legend heading two lists needs no exemption: it passes because each
# mark sits on its own line.


def _worst_line(text: str) -> tuple[str, int]:
    """The line carrying the most emoji, for a failure message worth reading."""
    lines = [line for line in text.splitlines() if line.strip()] or [text]
    worst = max(lines, key=_count_emoji)
    return worst, _count_emoji(worst)


def test_no_customer_string_crowds_one_line() -> None:
    """Many emoji spread over many lines is fine; two on one line is not."""
    offenders = {
        name: _worst_line(value)
        for name, value in customer_strings().items()
        if _worst_line(value)[1] > 1
    }
    assert not offenders, f"more than one emoji on a single line: {offenders}"


def test_the_line_cap_still_catches_a_crowded_line() -> None:
    """Mutation check for the per-line rule: two emoji on one line must be seen."""
    assert _worst_line("📅 مدت ⏳\nبدون ایموجی")[1] == 2
    assert _worst_line("📅 مدت\n⏳ باقی‌مانده")[1] == 1


def test_no_customer_string_shouts() -> None:
    """BRAND.md rule 4: at most one «!», and only for genuinely good news."""
    offenders = {
        name: value.count("!") + value.count("!")
        for name, value in customer_strings().items()
        if value.count("!") + value.count("!") > 1
    }
    assert not offenders, f"too many exclamation marks: {offenders}"


# --- the composed message, which is what the customer actually receives -----


def _expiring_soon() -> AccountOverview:
    return AccountOverview(
        has_account=True,
        has_orders=True,
        stale=False,
        status="active",
        expire=datetime.now(UTC) + timedelta(days=2),
        used_traffic=2 * 1024**3,
        data_limit=10 * 1024**3,
        subscription_url="https://panel.example.com/sub/neo_1",
        panel_username="neo_u1",
    )


def _assert_one_emoji_per_line(text: str) -> None:
    worst, count = _worst_line(text)
    assert count <= 1, f"{count} emoji on one line: {worst!r}"


def test_a_card_with_no_intro_keeps_the_warning_emoji() -> None:
    caption = build_service_card_caption(_expiring_soon(), warn_days=7)
    assert "⚠️" in caption
    _assert_one_emoji_per_line(caption)


@pytest.mark.parametrize(
    "intro",
    [
        "سفارش #۱۲ آماده شد ✅",
        "تست رایگان فعال شد ✅ (۱ روز / ۵ گیگ)",
        "⚠️ سرویست به‌زودی منقضی می‌شه (۲ روز مونده)",
    ],
)
def test_an_intro_that_already_has_an_emoji_makes_the_badge_yield(intro: str) -> None:
    """The renewal reminder used to print ⚠️ twice in one message.

    The cap on the whole message is gone, but the badge still yields: an intro
    that already carries an emoji is saying the same thing, and the badge text
    below it stays without its emoji.
    """
    caption = build_service_card_caption(_expiring_soon(), warn_days=7, intro=intro)
    _assert_one_emoji_per_line(caption)
    assert caption.count("⚠️") <= 1, caption
    # The warning itself is not lost — only its emoji is.
    assert "به‌زودی منقضی" in caption


def test_the_three_real_intros_are_within_the_rule() -> None:
    """Guards the actual strings, not just hand-written samples."""
    for intro in (
        PersianTexts.payment_ready_intro.format(order_id=12),
        PersianTexts.trial_ready_intro.format(days=1, gb=5, group="حجمی ویژه"),
        PersianTexts.renewal_reminder_intro.format(days=2),
    ):
        caption = build_service_card_caption(_expiring_soon(), warn_days=7, intro=intro)
        _assert_one_emoji_per_line(caption)
        assert caption.count("⚠️") <= 1, caption


# --- support line ----------------------------------------------------------


def test_the_support_screen_now_names_a_human() -> None:
    """It used to say «پیام خود را برای ادمین ارسال کنید» and name nobody."""
    settings = Settings(
        BOT_TOKEN="t", SUPPORT_HANDLE="neo_network_support", SUPPORT_HOURS_FA="۹ صبح تا ۱۲ شب"
    )
    body = with_support(PersianTexts.support, settings)
    assert "@neo_network_support" in body
    assert "۹ صبح تا ۱۲ شب" in body


def test_an_unconfigured_handle_says_less_rather_than_printing_an_empty_at() -> None:
    settings = Settings(BOT_TOKEN="t", SUPPORT_HANDLE="", SUPPORT_HOURS_FA="")
    assert support_line(get_contact_facts(settings)) == ""
    assert with_support(PersianTexts.support, settings) == PersianTexts.support


def test_hours_are_optional() -> None:
    settings = Settings(BOT_TOKEN="t", SUPPORT_HANDLE="@s", SUPPORT_HOURS_FA="")
    assert support_line(get_contact_facts(settings)) == "پشتیبانی: @s"


# --- admin copy must not leak into a customer's chat -----------------------
#
# The `admin_` prefix is the whole basis of the exemption above: those strings
# are skipped because only the owner reads them. That holds right up until an
# admin action DMs the customer about it — which is exactly how four wallet and
# service notices ended up written in the formal register, invisible to every
# rule in this file, because their names began with `admin_`.
#
# They are `notify_*` now, so the rules above cover them. This test defends the
# convention itself: nothing named `admin_*` may be sent to a customer.


def _enclosing_functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]


def _texts_attributes(node: ast.AST) -> set[str]:
    """Every ``texts.<name>`` / ``PersianTexts.<name>`` read inside ``node``."""
    found: set[str] = set()
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Attribute)
            and isinstance(sub.value, ast.Name)
            and sub.value.id in {"texts", "PersianTexts"}
        ):
            found.add(sub.attr)
    return found


def _resolve_text_argument(
    expr: ast.expr, scope: ast.FunctionDef | ast.AsyncFunctionDef | None
) -> set[str]:
    """Names the ``text=`` argument can evaluate to.

    A bare ``texts.x`` is read directly. ``tmpl.format(...)`` where ``tmpl`` is a
    local reaches back for the assignments to that local in the same function —
    the wallet notice is written that way (credit or debit, chosen by an
    ``if`` expression), and a reader that stopped at the call site would see a
    plain ``Name`` and conclude, wrongly, that no copy was involved.
    """
    names = _texts_attributes(expr)
    roots = {
        sub.id
        for sub in ast.walk(expr)
        if isinstance(sub, ast.Name) and sub.id not in {"texts", "PersianTexts"}
    }
    if roots and scope is not None:
        for stmt in ast.walk(scope):
            if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in roots for t in stmt.targets
            ):
                names |= _texts_attributes(stmt.value)
    return names


def _customer_bound_sends(source: str) -> list[tuple[int, set[str]]]:
    """``(line, text names)`` for each ``send_message`` aimed at a customer.

    A send is treated as owner-bound when its target mentions «admin» — that is
    how the provision-failure notice to `settings.admin_telegram_ids` is told
    apart from the one to `failed_order.user.telegram_id`.
    """
    tree = ast.parse(source)
    scope_of: dict[int, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for fn in _enclosing_functions(tree):
        for sub in ast.walk(fn):
            scope_of[id(sub)] = fn

    sends: list[tuple[int, set[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "send_message"):
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        target = kwargs.get("chat_id") or (node.args[0] if node.args else None)
        if target is None:
            continue
        if "admin" in ast.unparse(target).lower():
            continue
        text = kwargs.get("text") or (node.args[1] if len(node.args) > 1 else None)
        if text is None:
            continue
        sends.append((node.lineno, _resolve_text_argument(text, scope_of.get(id(node)))))
    return sends


_HANDLERS = Path(__file__).resolve().parents[2] / "bot" / "handlers"
ADMIN_HANDLERS = (_HANDLERS / "admin.py", _HANDLERS / "admin_wallet.py")


def test_the_scan_finds_the_sends_it_is_meant_to_check() -> None:
    """Guards the scanner, not the code: a rename that breaks it must go red."""
    sends = [
        send
        for path in ADMIN_HANDLERS
        for send in _customer_bound_sends(path.read_text(encoding="utf-8"))
    ]
    assert len(sends) >= 5, sends
    named = {name for _, names in sends for name in names}
    # The conditional-local case the resolver exists for.
    assert {"notify_wallet_credit", "notify_wallet_debit"} <= named, named


def test_no_admin_string_is_sent_to_a_customer() -> None:
    offenders = {
        (path.name, line): sorted(n for n in names if n.startswith("admin_"))
        for path in ADMIN_HANDLERS
        for line, names in _customer_bound_sends(path.read_text(encoding="utf-8"))
        if any(n.startswith("admin_") for n in names)
    }
    assert not offenders, (
        "admin copy is exempt from the voice rules because only the owner reads it — "
        f"these reach a customer instead, so rename them `notify_*`: {offenders}"
    )


def test_every_notify_string_is_covered_by_the_customer_rules() -> None:
    """The rename is only worth anything if `customer_strings()` picks them up."""
    covered = customer_strings()
    notify = [n for n in vars(PersianTexts) if n.startswith("notify_")]
    assert notify, "no notify_* copy left — did a rename undo the convention?"
    assert all(name in covered for name in notify), notify


# --- the brand says its own name ------------------------------------------


def test_the_bot_names_itself_where_a_customer_anchors() -> None:
    """The first message and the menu they return to, and nowhere else.

    Both screens used to name nobody: someone could buy, renew and ask for
    support without ever learning whose bot this is, and a forwarded screenshot
    pointed nowhere. A name is not a claim, so this is the one thing copy may
    assert without a number behind it — but it earns nothing by repetition, so
    it stays on these two.
    """
    assert SIGNATURE_BRAND_FA in PersianTexts.welcome_new
    assert SIGNATURE_BRAND_FA in PersianTexts.main_menu

    elsewhere = [
        name
        for name, value in customer_strings().items()
        if SIGNATURE_BRAND_FA in value and name not in {"welcome_new", "main_menu"}
    ]
    assert not elsewhere, f"the name earns nothing by repetition: {elsewhere}"
