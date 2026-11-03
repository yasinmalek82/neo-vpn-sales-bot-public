"""Keyboard composition tests."""

from bot.keyboards.main_menu import MenuCallback, main_menu_keyboard
from services.ux_format import wallet_menu_label


def _labels(is_admin: bool, wallet_balance: int | None = None) -> list[str]:
    kb = main_menu_keyboard(is_admin=is_admin, wallet_balance=wallet_balance)
    return [btn.text for row in kb.inline_keyboard for btn in row]


def test_admin_sees_management_button() -> None:
    labels = _labels(True)
    assert any("خرید اشتراک" in text for text in labels)
    assert any("سرویس‌های من" in text for text in labels)
    assert any("کیف پول" in text for text in labels)
    assert any("دعوت دوستان" in text for text in labels)
    assert any("پشتیبانی" in text for text in labels)
    assert any("مدیریت" in text for text in labels)
    # Every entry is badged, but *which* badge is a taste call that changes —
    # `normalize_label` strips emoji precisely so it can. Assert on the words.
    from services.ux_format import has_emoji

    assert all(has_emoji(text) for text in labels)
    kb = main_menu_keyboard(is_admin=True)
    assert any(btn.callback_data == MenuCallback.ADMIN for row in kb.inline_keyboard for btn in row)


def test_non_admin_hides_management() -> None:
    labels = _labels(False)
    assert not any("مدیریت" in text for text in labels)


def test_wallet_balance_on_menu_button() -> None:
    labels = _labels(False, wallet_balance=123_000)
    assert any(text == wallet_menu_label(123_000) for text in labels)
    assert any("۱۲۳٬۰۰۰" in text for text in labels)
    # Optional param keeps legacy label when omitted (tests / callers without balance).
    assert any(text.endswith("کیف پول من") for text in _labels(False))


def test_the_buy_button_stands_alone_on_the_first_row() -> None:
    """`bot/keyboards/style.py` layout rule 1.

    It used to share row 0 with «سرویس‌های من», which made the shop and the
    shelf read as two equal choices. On a phone a full-width button is an
    accent, so the one action the screen exists for gets the whole row.
    """
    rows = main_menu_keyboard(is_admin=False).inline_keyboard
    assert len(rows[0]) == 1
    assert rows[0][0].callback_data == MenuCallback.BUY


def test_main_menu_pairs_the_rest_and_closes_with_support() -> None:
    rows = main_menu_keyboard(is_admin=False).inline_keyboard
    assert len(rows[0]) == 1
    assert [b.callback_data for b in rows[0]] == [MenuCallback.BUY]
    assert [b.callback_data for b in rows[1]] == [MenuCallback.TRIAL, MenuCallback.ACCOUNT]
    assert [b.callback_data for b in rows[2]] == [MenuCallback.WALLET, MenuCallback.REFERRAL]
    assert [b.callback_data for b in rows[3]] == [MenuCallback.SUPPORT]

    admin_rows = main_menu_keyboard(is_admin=True).inline_keyboard
    assert [b.callback_data for b in admin_rows[0]] == [MenuCallback.BUY]
    assert [b.callback_data for b in admin_rows[1]] == [MenuCallback.TRIAL, MenuCallback.ACCOUNT]
    assert [b.callback_data for b in admin_rows[2]] == [MenuCallback.WALLET, MenuCallback.REFERRAL]
    assert [b.callback_data for b in admin_rows[3]] == [MenuCallback.SUPPORT, MenuCallback.ADMIN]


def test_trial_is_offered_to_everyone() -> None:
    """It used to render only on the empty «سرویس‌های من» screen.

    That made it unreachable for anyone holding a service — the owner could not
    find it at all during live testing. Whether the trial was already used is
    answered *after* the tap, not by hiding the button.
    """
    for is_admin in (False, True):
        entries = {
            button.callback_data
            for row in main_menu_keyboard(is_admin=is_admin).inline_keyboard
            for button in row
        }
        assert MenuCallback.TRIAL in entries
