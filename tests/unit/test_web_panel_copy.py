"""The personal pages speak the bot's voice: the same checks, on the web copy.

``test_brand_voice.py`` and ``test_customer_copy_consistency.py`` scan every
string the bot's customer can read. The Mini App's customer and reseller pages
are customer copy too, so this file runs **their own checkers** (imported, not
restated) over two sources:

* the static text of each customer-facing template, with the Jinja removed;
* the text of the pages as rendered for seeded people (so a name, a date, a
  figure and a panel line are in the scan, in the states that print them).

Admin copy (``finance.html``) is exempt by decision, like ``admin_*`` strings.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest
import pytest_asyncio

from tests.unit.test_brand_voice import (
    _PERSIAN,
    ABSOLUTE_PROMISES,
    FORMAL_MARKERS,
    SUPERLATIVES,
    _worst_line,
)
from tests.unit.test_customer_copy_consistency import (
    _BAD_PUNCTUATION,
    _CODE,
    _URL,
    GLOSSARY,
    RETIRED_TERMS,
    _label_lines,
    _prose,
    _uses,
    has_latin_digit,
)
from tests.unit.web_panel_helpers import INIT_HEADER, init_data
from tests.unit.web_personal_helpers import (
    Factory,
    FakePanel,
    make_ledger,
    make_person,
    make_sale,
    make_service,
    make_trials,
    panel_user,
    personal_client,
    use_panel,
)
from web.templating import TEMPLATES_DIR

CUSTOMER_TEMPLATES = (
    "shell.html",
    "home.html",
    "_services.html",
    "_wallet.html",
    "me_customer.html",
    "me_reseller.html",
)

_BLOCKS_OF_JINJA = re.compile(r"\{#.*?#\}|\{%.*?%\}", re.S)
_JINJA_EXPRESSION = re.compile(r"\{\{.*?\}\}", re.S)
_SKIPPED = frozenset({"script", "style", "svg"})
_TEXT_ATTRIBUTES = ("aria-label", "title", "alt", "placeholder")
_INLINE = frozenset({"b", "strong", "em", "small", "span", "i", "bdi", "code"})


class _Lines(HTMLParser):
    """Visible text, one entry per block element, plus the attributes a reader hears."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._current = ""
        self._skip = 0

    def _flush(self) -> None:
        text = " ".join(self._current.split())
        if text:
            self.lines.append(text)
        self._current = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED:
            self._skip += 1
            return
        if self._skip:
            return
        if tag not in _INLINE:
            self._flush()
        self.lines.extend(v for k, v in attrs if k in _TEXT_ATTRIBUTES and v)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED:
            self._skip = max(0, self._skip - 1)
        elif not self._skip and tag not in _INLINE:
            self._flush()

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._current += data

    def close(self) -> None:
        super().close()
        self._flush()


def visible_lines(html: str) -> list[str]:
    parser = _Lines()
    parser.feed(html)
    parser.close()
    return parser.lines


def template_text(name: str) -> str:
    raw = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
    raw = _BLOCKS_OF_JINJA.sub("", _JINJA_EXPRESSION.sub("{}", raw))
    return "\n".join(visible_lines(raw))


async def _get(client, tg: int, path: str) -> str:  # type: ignore[no-untyped-def]
    resp = await client.get(path, headers={INIT_HEADER: init_data(tg)})
    assert resp.status == 200, (path, resp.status)
    return await resp.text()


@pytest_asyncio.fixture
async def sources(
    two_sessions: tuple[Factory, str], monkeypatch: pytest.MonkeyPatch
) -> dict[str, str]:
    """Static template text and every page state that prints different lines."""
    factory, _ = two_sessions
    found = {f"template {name}": template_text(name) for name in CUSTOMER_TEMPLATES}

    cust = await make_person(factory, 7001, name="علی", balance=250_000)
    await make_service(factory, cust, username="cust_a", label="گوشی علی", expires_in_days=3)
    await make_service(factory, cust, username="cust_b", label="لپ‌تاپ", expires_in_days=40)
    await make_service(factory, cust, username="cust_c", label="تبلت")
    await make_ledger(factory, cust, amount=250_000, credit=True, reason="topup", after=250_000)
    await make_ledger(
        factory, cust, amount=40_000, credit=False, reason="checkout_debit", after=210_000
    )
    await make_person(factory, 7002, name="تازه")

    shop = await make_person(factory, 7101, name="فروشگاه", reseller=True, balance=64_000)
    await make_sale(factory, shop, price=90_000)
    await make_trials(factory, shop, count=2, quota=5)
    panel_users = {}
    # A name a shop typed is its own text (a phone number in it stays as typed),
    # so the seeded names carry no digits; the scan is about our words.
    for i, letter in enumerate("ابپتثجچحخدذر"):
        await make_service(factory, shop, username=f"shop_{i}", label=f"رضا {letter}")
        panel_users[f"shop_{i}"] = panel_user(f"shop_{i}", used_gb=1.5 * (i % 3))
    paid = await make_person(factory, 7102, name="پولی", reseller=True)
    await make_trials(factory, paid, count=2, quota=2, price=15_000)
    spent = await make_person(factory, 7103, name="تمومی", reseller=True)
    await make_trials(factory, spent, count=1, quota=1, price=0)
    empty_shop = await make_person(factory, 7104, name="خالی", reseller=True)
    assert empty_shop

    users = {
        "cust_a": panel_user("cust_a", used_gb=9.5, limit_gb=10, expires_in_days=3),
        "cust_b": panel_user("cust_b", used_gb=0.3, limit_gb=None),
        "cust_c": TimeoutError("down"),
        **panel_users,
    }
    use_panel(monkeypatch, FakePanel(users))
    async with personal_client(factory) as client:
        found["page customer"] = "\n".join(visible_lines(await _get(client, 7001, "/panel/me")))
        found["page customer (empty)"] = "\n".join(
            visible_lines(await _get(client, 7002, "/panel/me"))
        )
        for name, tg in (
            ("reseller", 7101),
            ("reseller (paid trials)", 7102),
            ("reseller (spent)", 7103),
            ("reseller (empty)", 7104),
        ):
            found[f"page {name}"] = "\n".join(
                visible_lines(await _get(client, tg, "/panel/me/reseller"))
            )
        found["page reseller p2"] = "\n".join(
            visible_lines(await _get(client, 7101, "/panel/me/reseller?offset=10"))
        )
        found["home customer"] = "\n".join(
            visible_lines(await _get(client, 7001, "/panel/me/home"))
        )
        found["home reseller"] = "\n".join(
            visible_lines(await _get(client, 7101, "/panel/me/home"))
        )
    return found


def test_the_scanner_reads_html_the_way_a_person_does() -> None:
    lines = visible_lines(
        '<nav aria-label="بخش‌ها"><svg><title>x</title></svg><p>📅 انقضا: <span>۱</span></p>'
        "<p>دو</p><script>var a = 'شما';</script></nav>"
    )
    assert lines == ["بخش‌ها", "📅 انقضا: ۱", "دو"]
    assert "{}" in template_text("_wallet.html") and "{%" not in template_text("_wallet.html")


@pytest.mark.asyncio
async def test_there_is_something_to_check(sources: dict[str, str]) -> None:
    assert len(sources) >= 12
    joined = "\n".join(sources.values())
    # The states the scan is meant to cover are really in it.
    for needle in (
        "حجم مصرف‌شده",  # a panel read that worked
        "نامحدود",  # an unlimited cap
        "به‌زودی منقضی",  # the expiry warning
        "قبلی",  # the pager
        "سهمیهٔ رایگانت تموم شده",  # a spent quota with a price
        "قیمتی برای تست بیشتر تنظیم نشده",
        "هنوز تراکنشی نداری",
        "هنوز سرویسی نداری",
    ):
        assert needle in joined, needle
    assert sum(len(v.splitlines()) for v in sources.values()) > 200


@pytest.mark.asyncio
async def test_no_web_string_uses_the_formal_register(sources: dict[str, str]) -> None:
    offenders: dict[str, list[str]] = {}
    for marker in FORMAL_MARKERS:
        pattern = re.compile(rf"(?<![{_PERSIAN}]){re.escape(marker)}(?![{_PERSIAN}])")
        for name, text in sources.items():
            if pattern.search(text):
                offenders.setdefault(marker, []).append(name)
    assert not offenders, offenders


@pytest.mark.asyncio
async def test_no_web_string_makes_an_unprovable_claim_or_promises_an_outcome(
    sources: dict[str, str],
) -> None:
    offenders = {
        name: [w for w in (*SUPERLATIVES, *ABSOLUTE_PROMISES) if w in text]
        for name, text in sources.items()
    }
    assert not {n: w for n, w in offenders.items() if w}, offenders


@pytest.mark.asyncio
async def test_no_web_line_crowds_emoji_or_shouts(sources: dict[str, str]) -> None:
    crowded = {
        name: _worst_line(text) for name, text in sources.items() if _worst_line(text)[1] > 1
    }
    assert not crowded, f"more than one emoji on a line: {crowded}"
    shouting = {
        n: t.count("!") + t.count("!")
        for n, t in sources.items()
        if t.count("!") + t.count("!") > 1
    }
    assert not shouting, shouting


@pytest.mark.asyncio
async def test_no_web_string_has_a_latin_digit_in_its_prose(sources: dict[str, str]) -> None:
    offenders = {n: _prose(t) for n, t in sources.items() if has_latin_digit(_prose(t))}
    assert not offenders, offenders


@pytest.mark.asyncio
async def test_no_web_string_has_broken_punctuation_or_spacing(sources: dict[str, str]) -> None:
    offenders: dict[str, list[str]] = {}
    for name, text in sources.items():
        cleaned = _URL.sub("U", _CODE.sub("X", text))
        problems = [label for label, rx in _BAD_PUNCTUATION.items() if rx.search(cleaned)]
        if problems:
            offenders[name] = problems
    assert not offenders, offenders


@pytest.mark.asyncio
async def test_every_glossary_line_on_the_web_carries_its_emoji(sources: dict[str, str]) -> None:
    wrong = {
        (name, line): f"want {GLOSSARY[label]}, got {emoji}"
        for name, text in sources.items()
        for emoji, label, line in _label_lines(text)
        if emoji != GLOSSARY[label]
    }
    assert not wrong, wrong
    seen = {label for text in sources.values() for _, label, _ in _label_lines(text)}
    for label in (
        "وضعیت",
        "انقضا",
        "زمان باقی‌مانده",
        "حجم مصرف‌شده",
        "سقف حجم",
        "مصرف حجم",
        "موجودی کیف پول",
    ):
        assert label in seen, label


@pytest.mark.asyncio
async def test_no_web_string_uses_a_retired_spelling(sources: dict[str, str]) -> None:
    offenders = {
        name: [f"{old} -> {new}" for old, new in RETIRED_TERMS.items() if _uses(old, text)]
        for name, text in sources.items()
    }
    assert not {n: f for n, f in offenders.items() if f}, offenders


@pytest.mark.asyncio
async def test_no_web_page_prints_a_raw_exception_or_code(sources: dict[str, str]) -> None:
    """No `str(exc)`, no reason code (`checkout_debit`), no status key (`ACTIVE`)."""
    joined = "\n".join(sources.values())
    for raw in (
        "checkout_debit",
        "topup",
        "ACTIVE",
        "active",
        "None",
        "Traceback",
        "Error",
        "{{",
        "{%",
    ):
        assert raw not in joined, raw


def test_the_scan_would_catch_each_kind_of_slip() -> None:
    """Mutation check on the scanner itself, so a quiet pass means something."""
    bad = "\n".join(
        visible_lines(
            "<p>لطفاً صبر کنید</p><p>🟢 📅 دو ایموجی</p><p>بهترین سرویس!!</p><p>وضعیت : ok 12</p>"
        )
    )
    assert re.search(rf"(?<![{_PERSIAN}])لطفاً(?![{_PERSIAN}])", bad)
    assert _worst_line(bad)[1] == 2
    assert "بهترین" in bad and bad.count("!") == 2
    assert has_latin_digit(_prose(bad))
    assert _BAD_PUNCTUATION["space before ، ؛ ؟ or :"].search(bad)
    assert [e for e, label, _ in _label_lines("💳 وضعیت: فعال") if e != GLOSSARY[label]]


def test_no_file_under_web_templates_contains_a_dash() -> None:
    """Guard: no file under web/templates/ contains «—» (Piece A2)."""
    offenders: dict[str, list[int]] = {}
    for path in sorted(TEMPLATES_DIR.glob("**/*.html")):
        lines = [
            i + 1
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines())
            if "—" in line
        ]
        if lines:
            offenders[path.name] = lines
    assert not offenders, f"templates contain «—»: {offenders}"


def test_no_web_text_key_in_fa_contains_a_dash() -> None:
    """Guard: no web_… text in bot/texts/fa.py contains «—» (Piece A2)."""
    from bot.texts import texts

    offenders = {
        name: getattr(texts, name)
        for name in dir(texts)
        if name.startswith("web_")
        and isinstance(getattr(texts, name), str)
        and "—" in getattr(texts, name)
    }
    assert not offenders, f"web_… texts contain «—»: {offenders}"
