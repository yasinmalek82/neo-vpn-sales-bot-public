"""Blu parsing must never confuse balance, debits or malformed money with a credit."""

from datetime import UTC, datetime

import pytest

SMS = "بلو\nواریز پول\nمشتری عزیز، 1,280,000ریال به حساب شما نشست.\nموجودی: 9,999,999 ریال\n۱۷:۲۸\n۱۴۰۵.۰۸.۱۱"


def test_blu_extracts_credit_not_balance_and_converts_jalali() -> None:
    from services.bank_sms import parse_blu_sms

    credit = parse_blu_sms(SMS)
    assert credit.amount_toman == 128_000
    assert credit.occurred_at == datetime(2026, 11, 2, 13, 58, tzinfo=UTC)


@pytest.mark.parametrize(
    "message",
    [
        SMS.replace("واریز پول", "برداشت پول"),
        SMS.replace("نشست.", "برداشت شد."),
        SMS.replace("1,280,000", "1,280,001"),
        SMS.replace("1,280,000", "1,28,000"),
        SMS.replace("1,280,000", "-1,280,000"),
        SMS.replace("1,280,000", "0"),
        SMS.replace("۱۴۰۵.۰۸.۱۱", "۱۴۰۵.۱۳.۱۱"),
        SMS.replace("۱۷:۲۸", "۲۵:۲۸"),
        SMS + "\nمتن اضافی",
        SMS.replace("ریال", "تومان"),
    ],
)
def test_blu_rejects_other_or_ambiguous_formats(message: str) -> None:
    from services.bank_sms import BankSmsError, parse_blu_sms

    with pytest.raises(BankSmsError):
        parse_blu_sms(message)


def test_persian_amount_and_whitespace_are_supported() -> None:
    from services.bank_sms import parse_blu_sms

    assert parse_blu_sms(SMS.replace("1,280,000", "۱٬۲۸۰٬۰۰۰")).amount_toman == 128_000
