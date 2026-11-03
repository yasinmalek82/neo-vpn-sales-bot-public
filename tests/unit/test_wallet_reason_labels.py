"""No wallet ledger reason code reaches a screen raw (smoke row ۷.۹)."""

from __future__ import annotations

import re

from services import customer_lookup, finance, wallet
from services.wallet import WALLET_REASON_OTHER_FA, wallet_reason_label

_LATIN = re.compile(r"[A-Za-z_]")


def _reason_codes() -> set[str]:
    """Every reason constant the services declare, found by name."""
    codes = {"reseller_trial"}  # written as a literal in services/trial.py
    for module in (wallet, finance, customer_lookup):
        for name, value in vars(module).items():
            if "REASON" in name and name.isupper() and isinstance(value, str):
                if not _LATIN.search(value):
                    continue  # a Persian label constant, not a code
                codes.add(value)
    return codes


def test_every_declared_reason_code_has_its_own_persian_label() -> None:
    codes = _reason_codes()
    assert {"checkout_debit", "checkout_refund", "retry_debit", "admin_manual_credit"} <= codes
    for code in codes:
        label = wallet_reason_label(code)
        assert not _LATIN.search(label), f"{code} is shown as {label!r}"
        assert label != WALLET_REASON_OTHER_FA, f"{code} has no label of its own"


def test_an_unknown_code_is_never_shown_raw() -> None:
    for code in ("some_new_reason", "", None):
        label = wallet_reason_label(code)
        assert label == WALLET_REASON_OTHER_FA and not _LATIN.search(label)
