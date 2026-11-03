"""The panel's customers screen: search, the card, and the wallet add or deduct.

The wallet change is money. The tests below pin, one by one: the request key
(the same confirm sent twice moves the balance once and writes one report), the
report living in the ledger's transaction, the bot's own validation and refusal
texts, who may use it, and that the card offers nothing the bot keeps for itself.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.handlers import admin as bot_admin
from bot.texts import texts
from database.models import Order, OrderStatus, Plan, User
from database.models.plan import DataLimitResetStrategy
from database.models.report import ReportOutbox
from database.models.wallet import WalletEntryType, WalletLedgerEntry
from services.customer_card import card_facts
from services.customer_lookup import (
    USER_PAGE_SIZE,
    adjust_wallet_manually,
    get_customer_card,
    list_users,
)
from services.ux_format import fa_toman
from tests.unit.web_panel_helpers import (
    DB_ADMIN,
    ENV_ADMIN,
    INIT_HEADER,
    STRANGER,
    add_user,
    init_data,
    panel_client,
    panel_settings,
)
from web.app import mount_web_panel
from web.routes.customers import TEXT_REPLAYED

Factory = tuple[async_sessionmaker[AsyncSession], str]
HEAD = {INIT_HEADER: init_data(ENV_ADMIN)}


def _head(user_id: int = ENV_ADMIN) -> dict[str, str]:
    return {INIT_HEADER: init_data(user_id)}


async def seed_user(
    factory: async_sessionmaker[AsyncSession],
    telegram_id: int,
    *,
    balance: int = 0,
    first_name: str | None = None,
    username: str | None = "",
    **flags: bool,
) -> int:
    async with factory() as session:
        user = User(
            telegram_id=telegram_id,
            username=f"u{telegram_id}" if username == "" else username,
            first_name=first_name if first_name is not None else f"N{telegram_id}",
            language_code="fa",
            referral_code=f"R{telegram_id}",
            wallet_balance_toman=balance,
            created_at=datetime(2026, 9, 30, 9, 0, tzinfo=UTC),
            **flags,
        )
        session.add(user)
        await session.commit()
        return int(user.id)


async def ledger(factory: async_sessionmaker[AsyncSession]) -> list[WalletLedgerEntry]:
    async with factory() as session:
        return list((await session.scalars(select(WalletLedgerEntry))).all())


async def reports(factory: async_sessionmaker[AsyncSession]) -> list[ReportOutbox]:
    async with factory() as session:
        return list((await session.scalars(select(ReportOutbox))).all())


async def balance_of(factory: async_sessionmaker[AsyncSession], user_id: int) -> int:
    async with factory() as session:
        return int(
            await session.scalar(select(User.wallet_balance_toman).where(User.id == user_id)) or 0
        )


async def confirm(client, uid: int, amount: str, direction: str = "add", headers=None):
    return await client.post(
        f"/panel/customers/{uid}/wallet/confirm",
        data={"amount": amount, "direction": direction},
        headers=headers or HEAD,
    )


def hidden(html: str, name: str) -> str:
    found = re.search(rf'name="{name}" value="([^"]*)"', html)
    assert found, f"no hidden field {name} in the confirm page"
    return found.group(1)


async def apply(client, uid: int, html: str, headers=None):
    """Post exactly what the confirm page's form posts."""
    return await client.post(
        f"/panel/customers/{uid}/wallet/apply",
        data={"amount": hidden(html, "amount"), "token": hidden(html, "token")},
        headers=headers or HEAD,
    )


# --------------------------------------------------------------------------- #
# Search, list and card
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_card_shows_the_figures_the_bots_card_shows(two_sessions: Factory) -> None:
    factory, _ = two_sessions
    uid = await seed_user(factory, 701, balance=0, first_name="علی")
    async with factory() as session:
        await adjust_wallet_manually(
            session, user_id=uid, amount_toman=1_234_567, admin_telegram_id=1, stamp="a"
        )
        await adjust_wallet_manually(
            session, user_id=uid, amount_toman=-34_567, admin_telegram_id=1, stamp="b"
        )
        plan = Plan(
            name="p",
            description="p",
            data_limit_gb=5,
            duration_days=30,
            price_toman=0,
            data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
            group_ids=[],
            hwid_limit=1,
            is_active=True,
        )
        session.add(plan)
        await session.flush()
        session.add(
            Order(
                user_id=uid,
                plan_id=plan.id,
                status=OrderStatus.ACTIVE,
                price_toman=90_000,
                discount_toman=0,
                force_new=True,
            )
        )
        await session.commit()
        card = await get_customer_card(session, uid)
    facts = card_facts(card, web=True)
    assert facts.wallet == fa_toman(1_200_000) and facts.ledger, "the seed must be visible"

    async with panel_client(factory) as client:
        resp = await client.get(f"/panel/customers/{uid}", headers=HEAD)
        assert resp.status == 200
        html = await resp.text()
    for figure in (
        facts.name,
        facts.status,
        facts.role,
        facts.wallet,
        facts.topped_up,
        facts.spent,
        facts.orders_total,
        facts.orders_paid,
        facts.last_order,
        facts.joined,
        facts.referral_code,
        facts.services_count,
        facts.trial,
        facts.handle,
        str(facts.telegram_id),
    ):
        assert figure in html, figure
    for row in facts.ledger:
        assert f"{row.sign}{row.amount}" in html and row.label in html
    assert texts.admin_customer_ledger_title.rstrip(":") in html


@pytest.mark.asyncio
async def test_the_card_has_no_subscription_link_and_no_unlisted_control(
    two_sessions: Factory,
) -> None:
    factory, _ = two_sessions
    uid = await seed_user(factory, 702, is_reseller=True, is_banned=True)
    async with panel_client(factory) as client:
        html = await (await client.get(f"/panel/customers/{uid}", headers=HEAD)).text()
    assert "http" not in html.replace("https://telegram.org", "")
    assert texts.admin_customer_status_banned in html and texts.admin_customer_role_reseller in html
    # The only controls: back to the list, the wallet form, and (a reseller only) the price sheet.
    hx = set(re.findall(r'hx-(?:get|post)="([^"]+)"', html))
    assert hx == {
        "/panel/customers",
        f"/panel/customers/{uid}/wallet",
        f"/panel/resellers/{uid}",
    }
    assert "<form" not in html and "<button" not in html


@pytest.mark.asyncio
async def test_the_price_sheet_link_is_for_a_reseller_only(two_sessions: Factory) -> None:
    factory, _ = two_sessions
    plain = await seed_user(factory, 704)
    async with panel_client(factory) as client:
        html = await (await client.get(f"/panel/customers/{plain}", headers=HEAD)).text()
    assert "/panel/resellers" not in html
    hx = set(re.findall(r'hx-(?:get|post)="([^"]+)"', html))
    assert hx == {"/panel/customers", f"/panel/customers/{plain}/wallet"}


@pytest.mark.asyncio
async def test_a_name_with_html_is_escaped_everywhere(two_sessions: Factory) -> None:
    factory, _ = two_sessions
    evil = '<script>alert("x")</script>'
    uid = await seed_user(factory, 703, first_name=evil, username="a<b>")
    async with panel_client(factory) as client:
        pages = [
            await (await client.get("/panel/customers", headers=HEAD)).text(),
            await (await client.get("/panel/customers?q=703", headers=HEAD)).text(),
            await (await client.get(f"/panel/customers/{uid}", headers=HEAD)).text(),
        ]
        sent = await confirm(client, uid, "1000")
        pages.append(await sent.text())
        search = await client.get("/panel/customers", params={"q": evil}, headers=HEAD)
        pages.append(await search.text())
    for html in pages:
        assert "<script>alert" not in html and "a<b>" not in html
        assert "&lt;script&gt;" in html or evil not in html


@pytest.mark.asyncio
async def test_the_list_pages_and_filters_like_the_bots(two_sessions: Factory) -> None:
    factory, _ = two_sessions
    total = USER_PAGE_SIZE + 3
    for n in range(total):
        await seed_user(factory, 800 + n, first_name=f"Cust{n:02d}", balance=1000 if n == 0 else 0)
    async with factory() as session:
        first = await list_users(session)
        second = await list_users(session, offset=USER_PAGE_SIZE)
    async with panel_client(factory) as client:
        page1 = await (await client.get("/panel/customers", headers=HEAD)).text()
        page2 = await (
            await client.get("/panel/customers", params={"o": str(USER_PAGE_SIZE)}, headers=HEAD)
        ).text()
        rich = await (
            await client.get("/panel/customers", params={"f": "balance"}, headers=HEAD)
        ).text()
        junk = await client.get("/panel/customers", params={"f": "zzz", "o": "-4"}, headers=HEAD)
        assert junk.status == 200, "an unknown filter and offset fall back, like the bot's"
    assert all(row.label in page1 for row in first.rows) and "Cust00" not in page1
    assert all(row.label in page2 for row in second.rows) and "Cust00" in page2
    assert f"o={first.next_offset}" in page1 and "o=0" in page2
    assert "Cust00" in rich and "Cust05" not in rich
    assert fa_toman(1000) in rich


@pytest.mark.asyncio
async def test_search_finds_by_telegram_id_and_username_and_says_when_nothing(
    two_sessions: Factory,
) -> None:
    factory, _ = two_sessions
    await seed_user(factory, 4242, first_name="Zed", username="zedzed")
    await seed_user(factory, 4343, first_name="Other")
    async with panel_client(factory) as client:
        by_id = await (
            await client.get("/panel/customers", params={"q": "4242"}, headers=HEAD)
        ).text()
        by_name = await (
            await client.get("/panel/customers", params={"q": "@zedzed"}, headers=HEAD)
        ).text()
        none = await (
            await client.get("/panel/customers", params={"q": "nobody"}, headers=HEAD)
        ).text()
    assert "Zed" in by_id and "Other" not in by_id
    assert "Zed" in by_name and "Other" not in by_name
    assert texts.admin_customer_none.format(query="nobody") in none


# --------------------------------------------------------------------------- #
# Who may use it
# --------------------------------------------------------------------------- #

ROUTES = [
    ("GET", "/panel/customers"),
    ("GET", "/panel/customers?q=x"),
    ("GET", "/panel/customers/{uid}"),
    ("GET", "/panel/customers/{uid}/wallet"),
    ("POST", "/panel/customers/{uid}/wallet/confirm"),
    ("POST", "/panel/customers/{uid}/wallet/apply"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_every_customers_path_is_guarded(
    two_sessions: Factory, method: str, path: str
) -> None:
    """No header, a forged one, a stranger and a banned admin get no page and no form."""
    factory, _ = two_sessions
    uid = await seed_user(factory, 710, balance=5000)
    await add_user(factory, STRANGER)
    await add_user(factory, DB_ADMIN, is_admin=True, is_banned=True)
    url = path.format(uid=uid)
    forms = {"amount": "1000", "direction": "add", "token": "x"}
    async with panel_client(factory) as client:
        for headers, status in (
            ({}, 401),
            ({INIT_HEADER: init_data(ENV_ADMIN, token="9:other")}, 401),
            (_head(STRANGER), 403),
            (_head(DB_ADMIN), 403),
        ):
            resp = await client.request(method, url, data=forms, headers=headers)
            assert resp.status == status, (method, url, headers)
            body = await resp.text()
            assert "<form" not in body and "کیف پول" not in body
    assert await ledger(factory) == [] and await balance_of(factory, uid) == 5000


@pytest.mark.asyncio
async def test_a_second_configured_admin_may_adjust_like_the_bot(two_sessions: Factory) -> None:
    """The bot's `_deny_if_not_admin` lets every admin adjust a wallet; the panel does the same."""
    factory, _ = two_sessions
    await add_user(factory, DB_ADMIN, is_admin=True)
    uid = await seed_user(factory, 711)
    settings = panel_settings(ADMIN_TELEGRAM_IDS=f"{ENV_ADMIN},{DB_ADMIN}")
    async with panel_client(factory, settings) as client:
        page = await confirm(client, uid, "2000", headers=_head(DB_ADMIN))
        assert page.status == 200
        done = await apply(client, uid, await page.text(), headers=_head(DB_ADMIN))
        assert done.status == 200
    (entry,) = await ledger(factory)
    assert entry.idempotency_key.startswith(f"admin_adjust:{DB_ADMIN}:{uid}:web:")


# --------------------------------------------------------------------------- #
# The form and its checks: the bot's checks, the bot's words
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_get_never_changes_anything(two_sessions: Factory) -> None:
    factory, _ = two_sessions
    uid = await seed_user(factory, 720, balance=7000)
    async with panel_client(factory) as client:
        for path in ("", "/wallet"):
            resp = await client.get(f"/panel/customers/{uid}{path}", headers=HEAD)
            assert resp.status == 200
        for path in ("/wallet/confirm", "/wallet/apply"):
            resp = await client.get(f"/panel/customers/{uid}{path}", headers=HEAD)
            assert resp.status == 405
    assert await ledger(factory) == [] and await reports(factory) == []
    assert await balance_of(factory, uid) == 7000


@pytest.mark.asyncio
async def test_the_form_states_the_current_balance(two_sessions: Factory) -> None:
    factory, _ = two_sessions
    uid = await seed_user(factory, 721, balance=7000)
    async with panel_client(factory) as client:
        html = await (await client.get(f"/panel/customers/{uid}/wallet", headers=HEAD)).text()
    assert fa_toman(7000) in html
    assert 'name="amount"' in html and 'value="deduct"' in html and 'value="add"' in html


HUGE = str(2**63)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("amount", "direction", "expected"),
    [
        ("0", "add", texts.admin_panel_invalid),
        ("-500", "add", texts.admin_panel_invalid),
        ("-500", "deduct", texts.admin_panel_invalid),
        ("abc", "add", texts.admin_panel_invalid),
        ("", "add", texts.admin_panel_invalid),
        ("1e5", "add", texts.admin_panel_invalid),
        ("12.5", "add", texts.admin_panel_invalid),
        ("²", "add", texts.admin_panel_invalid),
        (HUGE, "add", texts.admin_panel_invalid),
        ("9" * 40, "deduct", texts.admin_panel_invalid),
        ("500", "sideways", texts.admin_panel_invalid),
        ("500", "", texts.admin_panel_invalid),
        ("9000", "deduct", texts.admin_wallet_insufficient.format(wallet=fa_toman(5000))),
    ],
)
async def test_a_bad_amount_is_refused_in_the_bots_words_and_writes_nothing(
    two_sessions: Factory, amount: str, direction: str, expected: str
) -> None:
    factory, _ = two_sessions
    uid = await seed_user(factory, 722, balance=5000)
    async with panel_client(factory) as client:
        resp = await confirm(client, uid, amount, direction)
        html = await resp.text()
    assert resp.status == 200
    assert expected in html
    assert 'name="token"' not in html, "a refused amount gets no confirm page and no key"
    assert await ledger(factory) == [] and await reports(factory) == []
    assert await balance_of(factory, uid) == 5000


@pytest.mark.asyncio
async def test_a_credit_that_would_overflow_the_balance_is_refused(two_sessions: Factory) -> None:
    factory, _ = two_sessions
    uid = await seed_user(factory, 723, balance=2**63 - 10)
    async with panel_client(factory) as client:
        html = await (await confirm(client, uid, "11")).text()
    assert texts.admin_panel_invalid in html and 'name="token"' not in html
    assert await ledger(factory) == []


@pytest.mark.asyncio
async def test_persian_digits_and_separators_are_accepted_like_the_bots(
    two_sessions: Factory,
) -> None:
    factory, _ = two_sessions
    uid = await seed_user(factory, 724, balance=100_000)
    async with panel_client(factory) as client:
        html = await (await confirm(client, uid, "۵۰٬۰۰۰", "deduct")).text()
    assert fa_toman(50_000) in html and fa_toman(50_000) in html.split("موجودی بعد از اعمال")[1]


@pytest.mark.asyncio
async def test_an_unknown_customer_is_not_found_on_every_step(two_sessions: Factory) -> None:
    factory, _ = two_sessions
    async with panel_client(factory) as client:
        assert (await client.get("/panel/customers/9999", headers=HEAD)).status == 404
        assert (await client.get("/panel/customers/9999/wallet", headers=HEAD)).status == 404
        assert (await confirm(client, 9999, "1000")).status == 404
        resp = await client.post(
            "/panel/customers/9999/wallet/apply",
            data={"amount": "1000", "token": "x"},
            headers=HEAD,
        )
        assert resp.status == 404
    assert await ledger(factory) == []


@pytest.mark.asyncio
async def test_an_18_digit_id_matching_no_user_is_a_404_on_the_customer_card(
    two_sessions: Factory,
) -> None:
    factory, _ = two_sessions
    big_18 = "9" * 18
    async with panel_client(factory) as client:
        assert (await client.get(f"/panel/customers/{big_18}", headers=HEAD)).status == 404


@pytest.mark.asyncio
async def test_a_19_digit_id_past_63_bits_is_a_404_on_the_customer_card(
    two_sessions: Factory,
) -> None:
    """19 nines is above 2^63: a bound one digit wider would hand it to the driver (a 500)."""
    factory, _ = two_sessions
    async with panel_client(factory) as client:
        assert (await client.get(f"/panel/customers/{'9' * 19}", headers=HEAD)).status == 404


# --------------------------------------------------------------------------- #
# The confirm page, and what the apply writes
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_confirm_page_states_before_and_after_and_writes_nothing(
    two_sessions: Factory,
) -> None:
    factory, _ = two_sessions
    uid = await seed_user(factory, 730, first_name="رضا", balance=60_000)
    async with panel_client(factory) as client:
        add = await (await confirm(client, uid, "25000", "add")).text()
        sub = await (await confirm(client, uid, "25000", "deduct")).text()
    assert "رضا" in add and fa_toman(60_000) in add
    assert fa_toman(85_000) in add.split("موجودی بعد از اعمال")[1]
    assert fa_toman(35_000) in sub.split("موجودی بعد از اعمال")[1]
    assert "شارژ" in add and "کسر" in sub
    assert hidden(add, "amount") == "25000" and hidden(sub, "amount") == "-25000"
    assert await ledger(factory) == [] and await reports(factory) == []
    assert await balance_of(factory, uid) == 60_000


@pytest.mark.asyncio
async def test_the_result_equals_what_the_confirm_page_promised(two_sessions: Factory) -> None:
    factory, _ = two_sessions
    uid = await seed_user(factory, 731, balance=60_000)
    async with panel_client(factory) as client:
        page = await (await confirm(client, uid, "25000", "deduct")).text()
        promised = page.split("موجودی بعد از اعمال")[1].split("</b>")[0]
        done = await (await apply(client, uid, page)).text()
    assert await balance_of(factory, uid) == 35_000
    assert fa_toman(35_000) in promised
    assert texts.admin_wallet_done.format(wallet=fa_toman(35_000)) in done
    assert TEXT_REPLAYED not in done


@pytest.mark.asyncio
async def test_a_credit_writes_one_entry_one_report_and_the_balance(two_sessions: Factory) -> None:
    factory, _ = two_sessions
    uid = await seed_user(factory, 732, balance=1000)
    async with panel_client(factory) as client:
        page = await (await confirm(client, uid, "4000")).text()
        await apply(client, uid, page)
    (entry,) = await ledger(factory)
    assert entry.entry_type is WalletEntryType.credit
    assert (entry.reason, entry.amount_toman, entry.balance_after_toman) == (
        "admin_manual_credit",
        4000,
        5000,
    )
    assert entry.note == f"admin={ENV_ADMIN}"
    assert re.fullmatch(
        rf"admin_adjust:{ENV_ADMIN}:{uid}:web:[0-9a-f]{{16}}", entry.idempotency_key
    )
    (report,) = await reports(factory)
    assert report.event_key.startswith("wallet") and str(entry.id) in report.event_key
    assert await balance_of(factory, uid) == 5000


# --------------------------------------------------------------------------- #
# The request key: the same confirm sent twice moves the balance once
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("direction", "start", "end"), [("add", 10_000, 15_000), ("deduct", 5000, 0)]
)
async def test_the_same_confirm_sent_twice_moves_the_balance_once(
    two_sessions: Factory, direction: str, start: int, end: int
) -> None:
    factory, _ = two_sessions
    uid = await seed_user(factory, 740, balance=start)
    async with panel_client(factory) as client:
        page = await (await confirm(client, uid, "5000", direction)).text()
        first = await (await apply(client, uid, page)).text()
        second = await (await apply(client, uid, page)).text()
        third = await (await apply(client, uid, page)).text()
    assert await balance_of(factory, uid) == end
    assert len(await ledger(factory)) == 1 and len(await reports(factory)) == 1
    assert TEXT_REPLAYED not in first
    for answer in (second, third):
        assert TEXT_REPLAYED in answer, "a repeat says it was already recorded"
        assert texts.admin_wallet_done.format(wallet=fa_toman(end)) in answer, "and shows the truth"
        assert texts.admin_wallet_insufficient.split(":")[0] not in answer, "never a false refusal"


@pytest.mark.asyncio
async def test_two_deliveries_at_the_same_moment_move_the_balance_once(
    two_sessions: Factory,
) -> None:
    factory, _ = two_sessions
    uid = await seed_user(factory, 741, balance=0)
    async with panel_client(factory) as client:
        page = await (await confirm(client, uid, "5000")).text()
        answers = await asyncio.gather(*(apply(client, uid, page) for _ in range(4)))
        assert [a.status for a in answers] == [200] * 4
        bodies = [await a.text() for a in answers]
    assert await balance_of(factory, uid) == 5000
    assert len(await ledger(factory)) == 1 and len(await reports(factory)) == 1
    done = texts.admin_wallet_done.format(wallet=fa_toman(5000))
    assert all(done in body for body in bodies)
    assert sum(TEXT_REPLAYED in body for body in bodies) == 3


@pytest.mark.asyncio
async def test_a_second_confirm_page_is_a_second_adjustment(two_sessions: Factory) -> None:
    """The key tells requests apart: a deliberate repeat from a new page still applies."""
    factory, _ = two_sessions
    uid = await seed_user(factory, 742, balance=0)
    async with panel_client(factory) as client:
        for _ in range(2):
            page = await (await confirm(client, uid, "3000")).text()
            await apply(client, uid, page)
    assert await balance_of(factory, uid) == 6000 and len(await ledger(factory)) == 2


@pytest.mark.asyncio
async def test_a_key_minted_for_one_customer_cannot_adjust_another(two_sessions: Factory) -> None:
    factory, _ = two_sessions
    a = await seed_user(factory, 750, balance=1000)
    b = await seed_user(factory, 751, balance=1000)
    async with panel_client(factory) as client:
        page = await (await confirm(client, a, "5000")).text()
        resp = await apply(client, b, page)  # A's token and amount, posted at B
        html = await resp.text()
    assert texts.stale_button in html
    assert await ledger(factory) == [] and await reports(factory) == []
    assert await balance_of(factory, a) == 1000 and await balance_of(factory, b) == 1000


@pytest.mark.asyncio
async def test_a_key_cannot_carry_a_different_amount_or_another_admin(
    two_sessions: Factory,
) -> None:
    factory, _ = two_sessions
    await add_user(factory, DB_ADMIN, is_admin=True)
    uid = await seed_user(factory, 752, balance=1000)
    settings = panel_settings(ADMIN_TELEGRAM_IDS=f"{ENV_ADMIN},{DB_ADMIN}")
    async with panel_client(factory, settings) as client:
        page = await (await confirm(client, uid, "5000")).text()
        token = hidden(page, "token")
        for data, headers in (
            ({"amount": "9000", "token": token}, HEAD),  # edited amount
            ({"amount": "-5000", "token": token}, HEAD),  # flipped direction
            ({"amount": "5000", "token": token}, _head(DB_ADMIN)),  # another admin's session
            ({"amount": "5000", "token": "0" * 16 + "." + "0" * 32}, HEAD),
            ({"amount": "5000", "token": ""}, HEAD),
            ({"amount": "5000"}, HEAD),
        ):
            resp = await client.post(
                f"/panel/customers/{uid}/wallet/apply", data=data, headers=headers
            )
            assert texts.stale_button in await resp.text(), data
    assert await ledger(factory) == [] and await balance_of(factory, uid) == 1000


@pytest.mark.asyncio
async def test_a_key_made_up_by_hand_is_refused(two_sessions: Factory) -> None:
    factory, _ = two_sessions
    uid = await seed_user(factory, 753, balance=1000)
    async with panel_client(factory) as client:
        resp = await client.post(
            f"/panel/customers/{uid}/wallet/apply",
            data={"amount": "5000", "token": "deadbeefdeadbeef.deadbeefdeadbeefdeadbeefdeadbeef"},
            headers=HEAD,
        )
        assert texts.stale_button in await resp.text()
    assert await ledger(factory) == []


@pytest.mark.asyncio
async def test_if_the_balance_moved_since_the_page_the_result_shows_the_truth(
    two_sessions: Factory,
) -> None:
    factory, _ = two_sessions
    uid = await seed_user(factory, 754, balance=50_000)
    async with panel_client(factory) as client:
        credit_page = await (await confirm(client, uid, "10000")).text()
        deduct_page = await (await confirm(client, uid, "40000", "deduct")).text()
        # Meanwhile the bot (or another admin) takes 45 000.
        async with factory() as session:
            await adjust_wallet_manually(
                session, user_id=uid, amount_toman=-45_000, admin_telegram_id=1, stamp="bot"
            )
            await session.commit()
        # A credit still applies; the result shows the real balance, not the page's 60 000.
        done = await (await apply(client, uid, credit_page)).text()
        assert fa_toman(60_000) in credit_page.split("موجودی بعد از اعمال")[1]
        assert texts.admin_wallet_done.format(wallet=fa_toman(15_000)) in done
        # A deduction that no longer fits is refused with the bot's apply-time text.
        refused = await (await apply(client, uid, deduct_page)).text()
    assert "موجودی کافی نیست" in refused and fa_toman(15_000) in refused
    assert await balance_of(factory, uid) == 15_000
    assert len(await ledger(factory)) == 2, "the bot's entry and the one credit; nothing else"


# --------------------------------------------------------------------------- #
# The report is part of the ledger's transaction
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_failure_after_the_ledger_write_leaves_neither_entry_nor_report(
    two_sessions: Factory,
) -> None:
    from services.customer_lookup import apply_wallet_adjustment

    factory, _ = two_sessions
    uid = await seed_user(factory, 760, balance=1000)
    boom = AsyncMock(side_effect=RuntimeError("outbox down"))
    async with factory() as session:
        with (
            patch("services.report_outbox.enqueue_report", boom),
            pytest.raises(RuntimeError, match="outbox down"),
        ):
            await apply_wallet_adjustment(
                session, user_id=uid, amount_toman=5000, admin_telegram_id=1, stamp="web:x"
            )
    boom.assert_awaited_once()  # the ledger row had been written when this ran
    assert await ledger(factory) == [] and await reports(factory) == []
    assert await balance_of(factory, uid) == 1000


@pytest.mark.asyncio
async def test_the_report_row_is_written_before_the_commit(two_sessions: Factory) -> None:
    """Same transaction: the outbox row is in the session when the ledger row is, before commit."""
    from services.customer_lookup import apply_wallet_adjustment

    factory, _ = two_sessions
    uid = await seed_user(factory, 761, balance=0)
    seen: dict[str, int] = {}
    real_commit = AsyncSession.commit

    async def spying_commit(self: AsyncSession) -> None:
        seen["ledger"] = int(
            await self.scalar(select(func.count()).select_from(WalletLedgerEntry)) or 0
        )
        seen["outbox"] = int(await self.scalar(select(func.count()).select_from(ReportOutbox)) or 0)
        await real_commit(self)

    async with factory() as session:
        with patch.object(AsyncSession, "commit", spying_commit):
            await apply_wallet_adjustment(
                session, user_id=uid, amount_toman=5000, admin_telegram_id=1, stamp="web:y"
            )
    assert seen == {"ledger": 1, "outbox": 1}


# --------------------------------------------------------------------------- #
# Parity with the bot
# --------------------------------------------------------------------------- #

_VOLATILE = ("آیدی", "نام", "زمان", "نام کاربری")


def _stable_lines(body: str) -> list[str]:
    return [ln for ln in body.splitlines() if not ln.strip().lstrip("• ").startswith(_VOLATILE)]


@pytest.mark.asyncio
@pytest.mark.parametrize(("amount", "direction"), [("5000", "add"), ("3000", "deduct")])
async def test_a_web_adjustment_and_the_bots_leave_the_same_trail(
    two_sessions: Factory, amount: str, direction: str
) -> None:
    factory, _ = two_sessions
    web_uid = await seed_user(factory, 770, balance=10_000, first_name="Same", username="same_a")
    bot_uid = await seed_user(factory, 771, balance=10_000, first_name="Same", username="same_a2")
    signed = int(amount) if direction == "add" else -int(amount)

    async with panel_client(factory) as client:
        page = await (await confirm(client, web_uid, amount, direction)).text()
        await apply(client, web_uid, page)

    # The bot's confirm tap: parse, adjust, commit (admin_wallet_apply's body).
    parsed = bot_admin._parse_signed_toman(str(signed))
    async with factory() as session:
        await adjust_wallet_manually(
            session,
            user_id=bot_uid,
            amount_toman=int(parsed),
            admin_telegram_id=ENV_ADMIN,
            stamp="123",
        )
        await session.commit()

    entries = {e.user_id: e for e in await ledger(factory)}
    web_e, bot_e = entries[web_uid], entries[bot_uid]
    assert (
        web_e.entry_type,
        web_e.reason,
        web_e.amount_toman,
        web_e.balance_after_toman,
        web_e.note,
    ) == (
        bot_e.entry_type,
        bot_e.reason,
        bot_e.amount_toman,
        bot_e.balance_after_toman,
        bot_e.note,
    )
    assert await balance_of(factory, web_uid) == await balance_of(factory, bot_uid)
    by_user = {r.link_user_id: r for r in await reports(factory)}
    assert _stable_lines(by_user[web_uid].body) == _stable_lines(by_user[bot_uid].body)
    assert by_user[web_uid].topic_key == by_user[bot_uid].topic_key


@pytest.mark.asyncio
async def test_the_bot_and_the_panel_run_the_same_checks(two_sessions: Factory) -> None:
    """Both call `preview_wallet_adjustment`; here its answers for the same text are the same."""
    from services.customer_lookup import WalletAdjustRefused, preview_wallet_adjustment

    factory, _ = two_sessions
    uid = await seed_user(factory, 772, balance=5000)
    async with factory() as session:
        for raw, expect in (
            ("0", texts.admin_panel_invalid),
            ("x", texts.admin_panel_invalid),
            ("-9000", texts.admin_wallet_insufficient.format(wallet=fa_toman(5000))),
        ):
            with pytest.raises(WalletAdjustRefused) as err:
                await preview_wallet_adjustment(session, user_id=uid, raw_amount=raw)
            assert str(err.value) == expect
        ok = await preview_wallet_adjustment(session, user_id=uid, raw_amount="-2000")
        assert (ok.amount_toman, ok.balance_toman, ok.after_toman) == (-2000, 5000, 3000)


# --------------------------------------------------------------------------- #
# What stays in the bot
# --------------------------------------------------------------------------- #

ALLOWED = {
    ("GET", "/panel/customers"),
    ("GET", "/panel/customers/{user_id}"),
    ("GET", "/panel/customers/{user_id}/wallet"),
    ("POST", "/panel/customers/{user_id}/wallet/confirm"),
    ("POST", "/panel/customers/{user_id}/wallet/apply"),
}


def test_there_are_exactly_these_customer_routes_and_none_for_the_bots_actions() -> None:
    from tests.unit.web_panel_helpers import panel_settings

    app = web.Application()
    mount_web_panel(app, panel_settings(), None)  # type: ignore[arg-type]
    found = {
        (route.method, route.resource.canonical)
        for route in app.router.routes()
        if route.resource is not None
        and "/panel/customers" in route.resource.canonical
        and route.method != "HEAD"
    }
    assert found == ALLOWED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "suffix",
    [
        "/ban",
        "/unban",
        "/cut",
        "/activate",
        "/gift",
        "/renew",
        "/trial",
        "/reseller",
        "/wallet/zero",
    ],
)
async def test_the_bots_own_actions_have_no_panel_route(two_sessions: Factory, suffix: str) -> None:
    factory, _ = two_sessions
    uid = await seed_user(factory, 780, balance=5000)
    async with panel_client(factory) as client:
        for method in ("GET", "POST"):
            resp = await client.request(
                method, f"/panel/customers/{uid}{suffix}", data={"x": "1"}, headers=HEAD
            )
            assert resp.status in (404, 405)
    async with factory() as session:
        user = await session.get(User, uid)
        assert (user.is_banned, user.is_reseller, user.wallet_balance_toman) == (False, False, 5000)


@pytest.mark.asyncio
async def test_an_empty_value_is_a_word_on_the_web_and_a_dash_on_the_bots_card(
    two_sessions: Factory,
) -> None:
    factory, _ = two_sessions
    uid = await seed_user(factory, 790)
    async with factory() as session:
        card = await get_customer_card(session, uid)
    bot = card_facts(card)
    web = card_facts(card, web=True)
    assert bot.trial == "—" and bot.referrer == "—" and bot.last_order == "—"
    assert web.trial == web.referrer == web.last_order == texts.web_customer_empty == "ندارد"
    async with panel_client(factory) as client:
        html = await (await client.get(f"/panel/customers/{uid}", headers=HEAD)).text()
    assert "—" not in html and "ندارد" in html
