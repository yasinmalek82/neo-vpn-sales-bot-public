"""The invite text a referrer forwards to a stranger.

This string leaves the bot and lands in someone else's chat, where an
unprovable number is the most expensive thing we could send. So the rule from
BRAND.md is enforced here rather than trusted: a claim whose fact is missing is
**dropped**, never softened into an adjective.
"""

from __future__ import annotations

import pytest

from services.brand import BrandFacts, CatalogFacts
from services.referral import build_invite_pitch, build_invite_share_url
from services.ux_format import has_emoji

LINK = "https://t.me/neo_bot?start=ABC123"


def _facts(
    *,
    price: int | None = None,
    channel: str = "",
    support: str = "",
    hours: str = "",
) -> BrandFacts:
    return BrandFacts(
        support_handle=support,
        support_hours=hours,
        channel_handle=channel,
        catalog=CatalogFacts(cheapest_price_toman=price, plan_count=1, group_count=2),
    )


def test_bare_deployment_claims_nothing_but_still_invites() -> None:
    """No channel, no price, no support configured — the text must still work."""
    pitch = build_invite_pitch(_facts(), invite_link=LINK, referred_discount_percent=5)
    assert LINK in pitch
    assert "تومان" not in pitch
    assert "کانال" not in pitch
    assert "پشتیبانی" not in pitch


def test_price_is_quoted_only_when_the_catalog_has_one() -> None:
    with_price = build_invite_pitch(
        _facts(price=90_000), invite_link=LINK, referred_discount_percent=5
    )
    assert "۹۰٬۰۰۰" in with_price

    without = build_invite_pitch(_facts(), invite_link=LINK, referred_discount_percent=5)
    assert "شروع می‌شه" not in without


def test_support_line_needs_both_handle_and_hours() -> None:
    handle_only = build_invite_pitch(
        _facts(support="@neo"), invite_link=LINK, referred_discount_percent=5
    )
    assert "پشتیبانی" not in handle_only

    both = build_invite_pitch(
        _facts(support="@neo", hours="۹ صبح تا ۱۲ شب"),
        invite_link=LINK,
        referred_discount_percent=5,
    )
    assert "۹ صبح تا ۱۲ شب" in both


def test_the_link_comes_last() -> None:
    """BRAND.md §6: a message that opens with a link reads as an ad and is skipped."""
    pitch = build_invite_pitch(
        _facts(price=90_000, channel="@neo_ch"), invite_link=LINK, referred_discount_percent=5
    )
    assert pitch.rstrip().endswith(LINK)
    assert not pitch.lstrip().startswith("http")


def test_discount_is_stated_only_when_there_is_one() -> None:
    with_discount = build_invite_pitch(_facts(), invite_link=LINK, referred_discount_percent=5)
    assert "۵٪" in with_discount

    none = build_invite_pitch(_facts(), invite_link=LINK, referred_discount_percent=0)
    assert "٪" not in none
    assert LINK in none


@pytest.mark.parametrize("percent", [0, 5, 20])
def test_brand_voice_rules_hold(percent: int) -> None:
    pitch = build_invite_pitch(
        _facts(price=90_000, channel="@neo_ch", support="@neo", hours="۹ تا ۲۴"),
        invite_link=LINK,
        referred_discount_percent=percent,
    )
    assert not has_emoji(pitch), "BRAND.md rule 3: at most one emoji, and this text needs none"
    assert "!" not in pitch and "！" not in pitch, "BRAND.md rule 4: no exclamation marks"
    for superlative in ("بهترین", "قوی‌ترین", "بی‌نظیر", "فوق‌العاده", "عالی‌ترین"):
        assert superlative not in pitch, f"BRAND.md rule 5 forbids «{superlative}»"
    assert "شما" not in pitch, "BRAND.md rule 8: «تو», consistently"


def test_no_double_blank_line_when_nothing_is_configured() -> None:
    """A bare deployment left two blank lines stacked where the facts would be."""
    pitch = build_invite_pitch(_facts(), invite_link=LINK, referred_discount_percent=5)
    assert "\n\n\n" not in pitch


def test_price_carries_its_unit() -> None:
    """«قیمت از ۹۰٬۰۰۰ شروع می‌شه» is a number with no unit — toman or rial?"""
    pitch = build_invite_pitch(_facts(price=90_000), invite_link=LINK, referred_discount_percent=5)
    assert "۹۰٬۰۰۰ تومان" in pitch


def test_the_share_url_carries_the_referrers_own_link_and_the_discount() -> None:
    from urllib.parse import parse_qs, urlsplit

    url = build_invite_share_url(invite_link=LINK, referred_discount_percent=5)
    assert url.startswith("https://t.me/share/url?url=")
    shared = parse_qs(urlsplit(url).query)
    assert shared["url"] == [LINK]
    assert "۵٪" in shared["text"][0]
    assert LINK not in shared["text"][0], "Telegram already puts the link above the text"


def test_the_share_text_promises_no_discount_when_there_is_none() -> None:
    from urllib.parse import parse_qs, urlsplit

    url = build_invite_share_url(invite_link=LINK, referred_discount_percent=0)
    text = parse_qs(urlsplit(url).query)["text"][0]
    assert "تخفیف" not in text and "٪" not in text
