"""The shop's note about their own customer: «reza | 09123456789».

Two things this has to get right, and both are about finding a service again
six weeks later:

* the **name** is Latin, because it becomes the panel username's prefix, so
  `reza` in the shop's list and `reza_a7f3` in PasarGuard are visibly one
  service;
* the **phone** is stored as bare digits and never reaches the panel name, so
  «چهار رقم آخر» works and no username carries eleven stray digits.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import database.models  # noqa: F401  (registers mappers)
from database.base import Base
from database.models import User
from database.models.pasarguard_account import PasarGuardAccount
from services.accounts import list_services_page
from services.customer_label import (
    MAX_LABEL_CHARS,
    MAX_NAME_CHARS,
    build_label,
    normalize_phone,
    sanitize_customer_name,
    search_patterns,
    split_label,
)

# --- the name --------------------------------------------------------------


def test_the_name_is_normalized_the_way_a_panel_username_needs() -> None:
    """One rule with `sanitize_service_name`, so `reza_mohammadi` in the list
    and `reza_mohammadi_a7f3` in the panel are recognisably one service."""
    assert sanitize_customer_name("  Reza   Mohammadi  ") == "reza_mohammadi"


@pytest.mark.parametrize("raw", ["", "   ", "\n\t", "رضا", "!!!"])
def test_a_name_with_no_latin_form_is_refused(raw: str) -> None:
    """The caller asks again rather than filing a service under a name the
    panel cannot hold."""
    with pytest.raises(ValueError):
        sanitize_customer_name(raw)


def test_the_separator_cannot_survive_inside_a_name() -> None:
    """Otherwise `split_label` reads half the name back as a phone number.

    Free here rather than enforced: `|` is not in `[a-z0-9_]`, so the panel
    sanitizer removes it on the way past.
    """
    cleaned = sanitize_customer_name("reza | 0912")
    assert "|" not in cleaned
    assert split_label(build_label(cleaned, None)).phone is None


def test_a_long_name_is_cut_not_refused() -> None:
    """A shop mid-sale should not be argued with about length."""
    cleaned = sanitize_customer_name("r" * 200)
    assert len(cleaned) == MAX_NAME_CHARS


def test_the_name_becomes_the_prefix_of_a_real_panel_username() -> None:
    """The point of making it Latin at all."""
    from services.service_names import build_panel_username

    username = build_panel_username(sanitize_customer_name("Reza Mohammadi"))
    assert username.startswith("reza_mohammadi_")
    assert len(username.removeprefix("reza_mohammadi_")) == 4


# --- the phone -------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["09123456789", "۰۹۱۲۳۴۵۶۷۸۹", "0912-345-6789", "۰۹۱۲ ۳۴۵ ۶۷۸۹", "٠٩١٢٣٤٥٦٧٨٩"],
)
def test_every_way_of_writing_one_number_stores_the_same_digits(raw: str) -> None:
    assert normalize_phone(raw) == "09123456789"


@pytest.mark.parametrize("raw", ["", "بعداً می‌فرستم", "-", "   "])
def test_something_that_is_not_a_number_is_refused(raw: str) -> None:
    """Stored anyway, it renders as a dangling «|» and matches no search."""
    with pytest.raises(ValueError):
        normalize_phone(raw)


# --- composing and reading back -------------------------------------------


def test_the_shape_the_owner_asked_for() -> None:
    assert build_label("reza", "09123456789") == "reza | 09123456789"


def test_no_phone_means_no_dangling_separator() -> None:
    assert build_label("reza", None) == "reza"


def test_a_label_fits_the_column_it_is_stored_in() -> None:
    """`display_name` is String(64). Silent truncation by the database would
    cut the phone, which is the half that has to stay exact."""
    label = build_label("r" * 60, "09123456789")
    assert len(label) <= MAX_LABEL_CHARS
    assert label.endswith("09123456789")


def test_the_number_wins_when_there_is_no_room() -> None:
    long_number = "9" * MAX_LABEL_CHARS
    assert build_label("reza", long_number) == long_number


@pytest.mark.parametrize(
    "label,name,phone",
    [
        ("reza | 09123456789", "reza", "09123456789"),
        ("reza", "reza", None),
        ("", "", None),
        (None, "", None),
    ],
)
def test_reading_a_label_back(label: str | None, name: str, phone: str | None) -> None:
    parsed = split_label(label)
    assert parsed.name == name
    assert parsed.phone == phone


def test_a_label_written_before_this_shape_existed_still_reads() -> None:
    """A migrated service filed as «reza» simply has no phone."""
    parsed = split_label("reza")
    assert parsed.name == "reza"
    assert parsed.phone is None
    assert not parsed.has_phone


def test_round_trip() -> None:
    for name, phone in [("reza_mohammadi", "09123456789"), ("ali", None), ("reza2", "02188776655")]:
        parsed = split_label(build_label(name, phone))
        assert (parsed.name, parsed.phone) == (name, phone)


# --- search ----------------------------------------------------------------


def test_an_ascii_query_needs_only_one_pattern() -> None:
    assert search_patterns("6789") == ["%6789%"]


def test_a_persian_digit_query_is_also_tried_as_ascii() -> None:
    """A shop on a Persian keyboard types «۶۷۸۹»; the phone was stored as
    `6789`. The raw form is kept too, for anything filed before this rule."""
    assert search_patterns("۶۷۸۹") == ["%۶۷۸۹%", "%6789%"]


def test_an_empty_query_searches_for_nothing() -> None:
    assert search_patterns("  ") == []


# --- the feature the owner asked to keep -----------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _shop_with_customers(session: AsyncSession) -> User:
    shop = User(telegram_id=900, referral_code="R900", is_reseller=True)
    session.add(shop)
    await session.flush()
    for n, label in enumerate(
        [
            build_label("reza", "09121116789"),
            build_label("karimi", "09359996789"),
            build_label("moradi", "09121110000"),
            build_label("no_phone", None),
        ],
        start=1,
    ):
        session.add(
            PasarGuardAccount(
                user_id=shop.id,
                panel_user_id=8000 + n,
                panel_username=f"neo_{n:03d}",
                display_name=label,
            )
        )
    await session.flush()
    return shop


@pytest.mark.asyncio
async def test_the_last_four_digits_find_every_matching_customer(
    session: AsyncSession,
) -> None:
    """«الانم اینجوری هست فکر میکنم فقط این حفظ بشه حتما» — the owner's one
    request about search, so it gets a test that would notice it breaking."""
    shop = await _shop_with_customers(session)
    page = await list_services_page(session, shop.id, query="6789")
    names = sorted(row.display_name or "" for row in page.rows)
    assert names == ["karimi | 09359996789", "reza | 09121116789"]


@pytest.mark.asyncio
async def test_searching_with_persian_digits_finds_the_same_people(
    session: AsyncSession,
) -> None:
    """A shop typing on a Persian keyboard gets Persian digits by default.

    `LIKE` does not know ۶ and 6 are the same digit, so without the second
    pattern this returns nothing at all — and the shop concludes the customer
    was never registered.
    """
    shop = await _shop_with_customers(session)
    page = await list_services_page(session, shop.id, query="۶۷۸۹")
    assert page.total == 2


@pytest.mark.asyncio
async def test_searching_by_name_still_works(session: AsyncSession) -> None:
    shop = await _shop_with_customers(session)
    page = await list_services_page(session, shop.id, query="karimi")
    assert page.total == 1
    assert page.rows[0].display_name == "karimi | 09359996789"


@pytest.mark.asyncio
async def test_a_customer_with_no_phone_is_still_listed(session: AsyncSession) -> None:
    shop = await _shop_with_customers(session)
    page = await list_services_page(session, shop.id)
    assert any((row.display_name or "") == "no_phone" for row in page.rows)


# --- the label never reaches the panel -------------------------------------


def test_the_phone_never_reaches_the_panel_username() -> None:
    """Why the two are asked **separately**.

    One box taking «reza 09123456789» sanitizes to `reza_09123456789`, and
    every username the shop later has to find in PasarGuard carries eleven
    stray digits. The split is the fix, not a tidier prompt.
    """
    from services.service_names import build_panel_username

    label = build_label(sanitize_customer_name("reza"), normalize_phone("09123456789"))
    username = build_panel_username(split_label(label).name)
    assert "09123456789" not in username
    assert username.startswith("reza_")


def test_an_anonymous_panel_username_carries_nothing_of_the_buyer() -> None:
    from services.service_names import build_anonymous_panel_username

    names = {build_anonymous_panel_username() for _ in range(200)}
    assert len(names) == 200, "a repeat in 200 draws is not a random suffix"
    for name in names:
        assert name.startswith("neo_")
        suffix = name.removeprefix("neo_")
        assert len(suffix) == 8
        # `islower()` is False for an all-digit string, and the alphabet is
        # [a-z0-9] — so roughly one run in a hundred drew `12093441` and failed
        # a test that had nothing to say about it.
        assert suffix.isalnum() and suffix == suffix.lower()


# --- the prompt tells the truth about what happens -------------------------


@pytest.mark.parametrize("example", ["ali12", "mahdi", "ws1_ksdf"])
def test_every_good_example_survives_untouched(example: str) -> None:
    """The ✅ list promises «this works». It has to actually work."""
    from bot.texts import texts

    assert example in texts.username_rules
    assert sanitize_customer_name(example) == example


@pytest.mark.parametrize("example", ["محسن", "علی"])
def test_every_bad_example_is_actually_refused(example: str) -> None:
    """A ❌ list the bot then accepts is the bot lying about itself."""
    from bot.texts import texts

    assert example in texts.username_rules
    with pytest.raises(ValueError):
        sanitize_customer_name(example)


@pytest.mark.parametrize("raw", ["ali_", "tele@", "_mahdi", "Ali-Reza"])
def test_what_a_competitor_calls_invalid_is_cleaned_here_not_refused(raw: str) -> None:
    """Why the ❌ list is shorter than it could be.

    A competitor's prompt lists these as wrong. Ours accepts all four and
    tidies them, so listing them would be a rule the bot does not keep. The
    prompt says it strips them instead, and `purchase_service_name_cleaned`
    shows the result.
    """
    from bot.texts import texts

    cleaned = sanitize_customer_name(raw)
    assert cleaned and cleaned != raw
    assert raw not in texts.username_rules
