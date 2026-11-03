"""``parse_toman`` stops at SQLite's 64-bit integer: a storage bound, not a price cap.

Thirty nines parsed as an integer, reached the price column and raised an
``OverflowError`` out of the database driver: an unhandled error in the bot's
price prompt and a 500 on the panel's price form. Both now answer with the
invalid-input line. Any value that fits the column is still accepted: this is
not a limit on what a plan may cost.
"""

from __future__ import annotations

import pytest

from bot.texts import texts
from services.store_admin import StoreAdminError
from services.store_edit import parse_toman, set_package_price

INT64_MAX = 2**63 - 1


def test_a_value_beyond_sqlites_64_bit_integer_is_not_a_number() -> None:
    assert parse_toman("9" * 30) is None
    assert parse_toman(str(INT64_MAX + 1)) is None
    assert parse_toman("۹" * 30) is None


def test_a_superscript_digit_is_not_a_number_and_does_not_raise() -> None:
    assert parse_toman("²") is None


def test_the_largest_value_the_column_holds_is_still_accepted() -> None:
    """The bound is the storage's, so nothing the database can hold is refused."""
    assert parse_toman(str(INT64_MAX)) == INT64_MAX
    assert parse_toman("150,000") == 150_000
    assert parse_toman("۱۵۰٬۰۰۰") == 150_000
    assert parse_toman("0") == 0


@pytest.mark.asyncio
async def test_the_bots_price_edit_answers_a_huge_number_with_the_invalid_input_line() -> None:
    with pytest.raises(StoreAdminError) as raised:
        await set_package_price(None, 1, "9" * 30)  # type: ignore[arg-type]
    assert str(raised.value) == texts.admin_store_invalid_input
