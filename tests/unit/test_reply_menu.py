"""The bottom keyboard: matching, precedence, and the bug that motivated it.

A customer migrating from a market bot arrives with *its* reply keyboard still
on screen. One tapped «خرید سرویس 🛍» and this bot answered nothing at all. That
is why matching is done on normalized words rather than exact labels: neither
side's emoji can break navigation.
"""

from __future__ import annotations

import pytest

from bot.keyboards.reply_menu import (
    ACCOUNT,
    ACCOUNT_RESELLER,
    ADMIN,
    BUY,
    REFERRAL,
    SUPPORT,
    TRIAL,
    WALLET,
    ReplyRoot,
    main_reply_keyboard,
    match_root,
    navigation_root,
    normalize_label,
)


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        (BUY, ReplyRoot.BUY),
        (ACCOUNT, ReplyRoot.ACCOUNT),
        (WALLET, ReplyRoot.WALLET),
        (REFERRAL, ReplyRoot.REFERRAL),
        (SUPPORT, ReplyRoot.SUPPORT),
        (ADMIN, ReplyRoot.ADMIN),
    ],
)
def test_our_own_labels_route_to_their_root(label: str, expected: str) -> None:
    assert match_root(label) == expected


def test_the_previous_bots_label_is_understood() -> None:
    """The exact text that got silence in production."""
    assert match_root("خرید سرویس 🛍") == ReplyRoot.BUY


def test_buy_wins_over_account_when_a_label_contains_both_words() -> None:
    """«خرید سرویس» is a buy button, not a services button."""
    assert match_root("خرید سرویس") == ReplyRoot.BUY
    assert match_root("سرویس‌های من") == ReplyRoot.ACCOUNT


@pytest.mark.parametrize(
    "variant",
    ["🛒 خرید اشتراک", "خرید اشتراک", "🎉 خرید اشتراک", "خرید  اشتراک", "خرید‌اشتراک"],
)
def test_changing_or_dropping_the_emoji_does_not_break_navigation(variant: str) -> None:
    assert match_root(variant) == ReplyRoot.BUY


def test_normalization_keeps_only_words() -> None:
    assert normalize_label("🏦 کیف پول") == "کیفپول"
    assert normalize_label("") == ""
    assert normalize_label("🛒") == ""


def test_an_emoji_only_message_is_not_a_navigation_tap() -> None:
    assert match_root("🛒") is None
    assert match_root("👍") is None


def test_unrelated_text_is_left_to_the_real_handlers() -> None:
    for text in ("سلام", "myvpn", "SAVE15", "12345"):
        assert match_root(text) is None


def test_a_long_message_is_never_treated_as_a_button() -> None:
    """A typed sentence that happens to contain «خرید» must reach its own handler."""
    typed = "سلام، من می‌خواستم بپرسم خرید اشتراک برای دو دستگاه چطوریه؟"
    assert len(typed) > 30
    assert match_root(typed) is None


# --- keyboard shape --------------------------------------------------------


def _labels(markup) -> list[str]:  # type: ignore[no-untyped-def]
    return [button.text for row in markup.keyboard for button in row]


def test_the_bottom_keyboard_carries_the_whole_main_menu() -> None:
    """It replaced the inline menu outright, so nothing may be missing from it."""
    from bot.keyboards.main_menu import main_menu_keyboard

    labels = _labels(main_reply_keyboard(is_admin=False))
    assert labels == [BUY, TRIAL, ACCOUNT, WALLET, REFERRAL, SUPPORT]

    inline = {
        button.text for row in main_menu_keyboard(is_admin=False).inline_keyboard for button in row
    }
    for entry in inline:
        root = match_root(entry)
        assert root is not None, f"«{entry}» has no bottom-keyboard equivalent"


def test_an_admin_also_sees_the_admin_root() -> None:
    assert ADMIN in _labels(main_reply_keyboard(is_admin=True))


def test_the_keyboard_is_persistent_and_sized_to_its_buttons() -> None:
    """Otherwise it collapses behind the paperclip and stops being navigation."""
    markup = main_reply_keyboard(is_admin=False)
    assert markup.resize_keyboard is True
    assert markup.is_persistent is True


# --- precedence ------------------------------------------------------------


def test_the_reply_router_outranks_every_state_scoped_handler(
    router_names: list[str],
) -> None:
    """Tapping «کیف پول» while the bot waits for a receipt must show the wallet."""
    names = router_names

    assert names[0] == "common", "/cancel still comes first"
    assert names[1] == "reply_menu"
    assert names[-1] == "fallback"
    for flow in ("purchase", "payment", "account", "wallet", "admin"):
        assert names.index("reply_menu") < names.index(flow)


def test_every_root_has_a_handler() -> None:
    """A button with no handler would fall through to «این پیام رو نفهمیدم»."""
    import bot.handlers.reply_menu as module

    handled = set()
    for observer in module.router.observers["message"].handlers:
        for label, root in (
            (BUY, ReplyRoot.BUY),
            (ACCOUNT, ReplyRoot.ACCOUNT),
            (WALLET, ReplyRoot.WALLET),
            (REFERRAL, ReplyRoot.REFERRAL),
            (SUPPORT, ReplyRoot.SUPPORT),
            (ADMIN, ReplyRoot.ADMIN),
        ):
            for f in observer.filters or ():
                callback = getattr(f, "callback", None)
                if callable(callback) and getattr(callback, "__name__", "") == "_check":
                    try:
                        from types import SimpleNamespace

                        if callback(SimpleNamespace(text=label)):
                            handled.add(root)
                    except Exception:
                        pass
    assert handled == {
        ReplyRoot.BUY,
        ReplyRoot.ACCOUNT,
        ReplyRoot.WALLET,
        ReplyRoot.REFERRAL,
        ReplyRoot.SUPPORT,
        ReplyRoot.ADMIN,
    }


def test_inside_a_flow_only_an_exact_bottom_label_navigates() -> None:
    assert navigation_root(WALLET, in_flow=True) == ReplyRoot.WALLET
    assert navigation_root(ACCOUNT_RESELLER, in_flow=True) == ReplyRoot.ACCOUNT
    for typed in ("سرویست فعال شد", "خرید سرویس 🛍", "تست", "کیف پول"):
        assert navigation_root(typed, in_flow=True) is None, typed


def test_outside_a_flow_the_keyword_guess_still_catches_the_old_keyboard() -> None:
    assert navigation_root("خرید سرویس 🛍", in_flow=False) == ReplyRoot.BUY
    assert navigation_root(BUY, in_flow=False) == ReplyRoot.BUY
    assert navigation_root("سلام", in_flow=False) is None
