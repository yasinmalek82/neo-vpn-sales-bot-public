"""BRAND.md §4's closing block, and the one place it must not appear.

«پست‌های فوروارد شده منبعشون گم نمی‌شه» is the whole reason for the block, so
the test that matters is not «does it render» but «is it on the messages that
travel, and off the one message where it would cost the referrer their credit».
"""

from __future__ import annotations

import pytest

from services.brand import BrandFacts, CatalogFacts, brand_signature, with_signature
from services.referral import build_invite_pitch

BODY = "سرور فنلاند از ساعت ۲ قطعه.\nداریم روش کار می‌کنیم."


def test_the_block_is_exactly_what_brand_md_specifies() -> None:
    assert brand_signature("neo_bot") == "─────\nنئو نتورک · @neo_bot"


@pytest.mark.parametrize("handle", ["neo_bot", "@neo_bot", "  @neo_bot  "])
def test_the_handle_is_normalized_however_it_arrives(handle: str) -> None:
    assert brand_signature(handle).endswith("· @neo_bot")


def test_an_unknown_handle_signs_nothing_rather_than_a_bare_at() -> None:
    """Same rule as `support_line`: say less, never print a placeholder."""
    assert brand_signature("") == ""
    assert with_signature(BODY, "") == BODY


def test_signing_is_idempotent() -> None:
    """The owner edits a draft and resubmits; the preview is rendered twice."""
    once = with_signature(BODY, "neo_bot")
    assert with_signature(once, "neo_bot") == once
    assert once.count("نئو نتورک") == 1


def test_an_owner_who_typed_the_signature_by_hand_does_not_get_a_second() -> None:
    hand_written = f"{BODY}\n\n─────\nنئو نتورک · @neo_bot"
    assert with_signature(hand_written, "neo_bot") == hand_written


def test_the_body_is_not_altered_above_the_rule() -> None:
    signed = with_signature(BODY, "neo_bot")
    assert signed.startswith(BODY)
    assert signed.splitlines()[0] == "سرور فنلاند از ساعت ۲ قطعه."


def test_trailing_whitespace_does_not_become_a_gap() -> None:
    assert with_signature(f"{BODY}\n\n\n", "neo_bot") == with_signature(BODY, "neo_bot")


# --- the exception, which is the point ------------------------------------


def _facts() -> BrandFacts:
    return BrandFacts(
        support_handle="@neo",
        support_hours="۹ تا ۲۴",
        channel_handle="@neo_ch",
        catalog=CatalogFacts(cheapest_price_toman=90_000, plan_count=2, group_count=2),
    )


def test_the_invite_pitch_is_not_signed() -> None:
    """A `@handle` under the referral link is an unattributed way in.

    The pitch ends with `?start=<code>`. Printing the bare bot handle beneath it
    offers the reader a route that carries no referral code — the referrer loses
    the credit the message exists to earn, and neither of them finds out why.
    """
    pitch = build_invite_pitch(
        _facts(),
        invite_link="https://t.me/neo_bot?start=ABC123",
        referred_discount_percent=5,
    )
    assert "─────" not in pitch
    assert pitch.rstrip().endswith("?start=ABC123")


# --- where it gets attached -----------------------------------------------
#
# Signed at *compose* time, not at delivery. «همان‌طور که می‌نویسی برای مشتری
# ارسال می‌شود» is a promise `admin_cast_prompt_body` makes to the owner, so
# the stored body, the preview and what the customer receives are one string.


class _StubBot:
    async def get_me(self) -> object:  # pragma: no cover - not reached
        raise AssertionError("BOT_USERNAME was set; Telegram should not be asked")


class _StubMessage:
    def __init__(self, bot: object | None) -> None:
        self.bot = bot


@pytest.mark.asyncio
async def test_compose_signs_the_body_the_owner_typed() -> None:
    from bot.config import Settings
    from bot.handlers.admin import _signed

    settings = Settings(BOT_TOKEN="t", BOT_USERNAME="neo_bot")
    signed = await _signed(BODY, _StubMessage(_StubBot()), settings)  # type: ignore[arg-type]
    assert signed == with_signature(BODY, "neo_bot")


@pytest.mark.asyncio
async def test_a_missing_bot_still_lets_the_announcement_out() -> None:
    """Unsigned is a blemish; a blocked outage notice is a real cost.

    BRAND.md rule 6 makes the outage message the most important thing the brand
    sends, so nothing about the signature may stand between the owner and it.
    """
    from bot.config import Settings
    from bot.handlers.admin import _signed

    settings = Settings(BOT_TOKEN="t", BOT_USERNAME="neo_bot")
    assert await _signed(BODY, _StubMessage(None), settings) == BODY  # type: ignore[arg-type]
