"""Expected delivery presentation, independent of the production middleware.

Only use this for visible report text; stored data, buttons and callback values
keep their own exact assertions. The strict Telegram fake is deliberately not
changed by this helper, so disabling delivery normalization still fails tests.
"""

from __future__ import annotations

from unicodedata import decimal

from tests.e2e.harness.htmlcheck import plain


def report_plain(text: str) -> str:
    separators = {"٬": ",", "٫": "."}
    return "".join(
        str(decimal(char)) if char.isdecimal() else separators.get(char, char)
        for char in plain(text)
    )


def final_amount_line(text: str) -> str:
    """Read the unique net-price field, independent of where the CTA is placed."""
    lines = [line for line in text.splitlines() if line.startswith("💰 مبلغ نهایی سفارش: ")]
    assert len(lines) == 1, text
    return lines[0]
