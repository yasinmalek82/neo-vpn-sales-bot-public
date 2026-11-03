"""Helpers for the reseller scenarios (section ۷ب): the owner's path, as taps."""

from __future__ import annotations

from tests.e2e.harness import Actor, App, Store


async def open_card(admin: Actor, name: str) -> None:
    """/admin → مشتری‌ها → کاربران → the person's card."""
    await admin.send("/admin")
    await tap_starting(admin, "مشتری‌ها")
    await tap_starting(admin, "کاربران")
    await tap_starting(admin, name)


async def grant_reseller(app: App, reseller: Actor) -> Actor:
    """The owner makes ``reseller`` a reseller from the customer card. Returns the admin."""
    admin = app.admin()
    await open_card(admin, reseller.name)
    await tap_starting(admin, "🏪 نماینده کردن")
    return admin


async def open_prices(admin: Actor) -> None:
    await tap_starting(admin, "قیمت‌های نمایندگی")


async def set_rates(admin: Actor, gb: int, day: int) -> None:
    """From the price panel: custom rates, then back to the panel."""
    await tap_starting(admin, "🧩")
    await tap_starting(admin, "هر گیگ")
    await admin.send(str(gb))
    await tap_starting(admin, "هر روز")
    await admin.send(str(day))
    await tap_starting(admin, "⬅️ قیمت")


async def set_trial_terms(admin: Actor, quota: int, extra_price: int) -> None:
    await tap_starting(admin, "🧪")
    await tap_starting(admin, "سهمیه")
    await admin.send(str(quota))
    await tap_starting(admin, "هر تست")
    await admin.send(str(extra_price))
    await tap_starting(admin, "⬅️ قیمت")


async def set_plan_price(
    admin: Actor, group: str, plan_title: str, price: int, *, from_panel: bool = True
) -> None:
    """From the price panel: open «پلن‌های آماده», the group tab, the plan; type the price.

    Typing the price makes the bot edit the admin's own message, which Telegram
    refuses (a finding, pinned by its own scenario); the bot then sends the screen
    as a new message, so the scenarios that are about something else accept it.
    """
    if from_panel:
        await tap_starting(admin, "🎁")
    await tap_starting(admin, group)
    await tap_starting(admin, plan_title)
    await admin.send(str(price))


async def approve_order(app: App, order_id: int) -> None:
    """The admin opens the payment queue and approves the order."""
    admin = app.admin()
    await admin.send("/admin")
    await tap_starting(admin, "پرداخت‌ها")
    await tap_starting(admin, f"#{order_id}")
    await tap_starting(admin, "✅ تایید")


async def charge_wallet(app: App, name: str, amount: int) -> None:
    """The admin tops up someone's wallet from their card."""
    admin = app.admin()
    await open_card(admin, name)
    await tap_starting(admin, "کیف پول")
    await admin.send(str(amount))
    await tap_starting(admin, "تأیید و اعمال")


async def buy_ready(
    shop: Actor, app: App, group: str, plan_title: str, name: str, phone: str | None
) -> int:
    """«خرید اشتراک» → group(s) → plan → customer name → phone → card order. Returns the order id."""
    from bot.keyboards.reply_menu import BUY

    await shop.tap_reply(BUY)
    for title in group.split("/"):
        await shop.tap(title)
    await tap_starting(shop, plan_title)
    await shop.send(name)
    if phone is None:
        await shop.tap("شماره ندارم")
    else:
        await shop.send(phone)
    # No promo question for a reseller: the phone step leads straight to the summary.
    await shop.tap("✅ ثبت سفارش")
    order = await app.latest_order(shop)
    return int(order.id)


async def enable_reseller_trial(app: App, store: Store, *group_keys: str) -> None:
    """Turn «تست نماینده» on for store groups (the group dashboard's switch, via the service)."""
    from services.store_admin import commit_and_activate, update_draft_category

    async with app.session() as session:
        for key in group_keys:
            await update_draft_category(session, store.category_ids[key], reseller_trial=True)
        await commit_and_activate(session, notes="تست نماینده روشن شد")


async def tap_starting(actor: Actor, label: str) -> None:
    """Tap a button on the newest screen only (``actor.tap`` also looks at older ones).

    Exact label first, then the one that starts with it, then the one that contains it.
    """
    buttons = actor.buttons
    for matches in (
        [b for b in buttons if b == label],
        [b for b in buttons if b.startswith(label)],
        [b for b in buttons if label in b],
    ):
        if len(matches) == 1:
            await actor.tap(matches[0])
            return
        assert not matches, f"{actor.name}: {label!r} is ambiguous on screen: {matches}"
    raise AssertionError(f"{actor.name} sees no {label!r} on the newest screen: {buttons}")


async def take_trial(shop: Actor, group_prefix: str, name: str, phone: str | None) -> None:
    """«🎟 تست رایگان» → the group → customer name → phone (or «شماره ندارم»)."""
    await shop.tap_reply("🎟 تست رایگان")
    await tap_starting(shop, group_prefix)
    await shop.send(name)
    if phone is None:
        await shop.tap("شماره ندارم")
    else:
        await shop.send(phone)


# Retail custom volume: 6000 toman/GB up to 50 GB, 25% off a 365-day service.
# Chosen so the reseller formula (rate x GB + rate x days, no discount) crosses
# over retail only on the long service: 1200 x 50 + 500 x 365 = 242 500, retail 225 000.
CUSTOM_PRICING = {
    "currency": "IRT",
    "volume_plan": {
        "volume_options_gb": [30, 50, 100],
        "duration_options_days": [30, 90, 365],
        "tiers": [
            {"max_gb": 50, "price_per_gb_toman": "6000"},
            {"max_gb": None, "price_per_gb_toman": "5000"},
        ],
        "duration_discounts": [{"min_days": 365, "percent": "25"}],
    },
    "unlimited_plan": {
        "user_options": [1, 2],
        "duration_options_months": [1],
        "base_price_first_user_toman": "250000",
        "additional_user_coefficients": ["0.70"],
        "duration_discounts": [],
    },
    "promo_codes": {},
}
RETAIL_CUSTOM_50GB_30D = 300_000
RETAIL_CUSTOM_50GB_365D = 225_000


async def add_custom_volume(app: App, store: Store, group_key: str = "special") -> None:
    """Give a store group the «🧩 خرید سفارشی» builder, as the seed does.

    The admin screens cannot make a ``custom_template`` package or the pricing
    document it reads (only the seed does), so the catalog is edited directly.
    """
    import copy

    from database.models.store import StorePackage, StorePackageKind
    from services.store_admin import commit_and_activate, get_or_create_draft

    async with app.session() as session:
        draft = await get_or_create_draft(session)
        draft.pricing_document = copy.deepcopy(CUSTOM_PRICING)
        category_id = next(
            c.id for c in draft.categories if c.slug == _slug_of(store, group_key, draft)
        )
        session.add(
            StorePackage(
                store_version_id=draft.id,
                category_id=category_id,
                slug="volume_custom",
                kind=StorePackageKind.custom_template,
                title_fa="خرید حجمی سفارشی",
                enabled=True,
                sort_order=99,
                fixed_price_toman=None,
                pricing_ref="volume_plan",
                options={
                    "volume_options_gb": [30, 50, 100],
                    "duration_options_days": [30, 90, 365],
                },
                forbid_extra_discount=False,
                allow_new_sale=True,
                allow_renew=True,
                sales_flags={},
            )
        )
        await session.flush()
        await commit_and_activate(session, notes="خرید سفارشی روشن شد")


def _slug_of(store: Store, group_key: str, draft: object) -> str:
    # The draft's categories are clones: find the one whose title matches.
    title = store.groups[group_key].title
    return next(c.slug for c in draft.categories if c.title_fa == title)  # type: ignore[attr-defined]


async def make_shop(
    app: App,
    name: str = "rez",
    *,
    rates: tuple[int, int] | None = None,
    trial: tuple[int, int] | None = None,
    wallet: int = 0,
    prices: tuple[tuple[str, str, int], ...] = (),
) -> tuple[Actor, Actor]:
    """A reseller, made the way the owner makes one. Returns ``(shop, admin)``.

    ``rates`` = (toman per GB, toman per day); ``trial`` = (monthly quota, price of
    an extra trial); ``prices`` = (group tab, plan title, agreed price) triples.
    The shop sends /start again afterwards: its bottom keyboard only changes then.
    """
    shop = app.customer(name)
    await shop.start()
    admin = await grant_reseller(app, shop)
    await shop.start()
    await open_prices(admin)
    if rates is not None:
        await set_rates(admin, *rates)
    if trial is not None:
        await set_trial_terms(admin, *trial)
    for index, (group, title, price) in enumerate(prices):
        await set_plan_price(admin, group, title, price, from_panel=index == 0)
    if prices:
        await tap_starting(admin, "⬅️ قیمت")
    if wallet:
        await charge_wallet(app, name, wallet)
    return shop, admin


async def deliver_by_card(app: App, shop: Actor, order_id: int) -> None:
    """The shop sends the receipt and the admin approves it."""
    await shop.send_photo()
    await approve_order(app, order_id)


def matrix_prices(button: str) -> tuple[int, int | None]:
    """(retail, agreed) read off a price-matrix button «پلن • خرده X • نماینده Y»."""
    from tests.e2e.harness import digits

    _, retail, agreed = (part.strip() for part in digits(button).split("•"))
    return int(retail.removeprefix("خرده").strip()), (
        None if agreed.endswith("—") else int(agreed.removeprefix("نماینده").strip())
    )


async def make_customers(shop: Actor, specs: list[tuple[str, str | None]]) -> None:
    """The shop gives each (name, phone) a free trial in the «ویژه» group."""
    for name, phone in specs:
        await take_trial(shop, "ویژه", name, phone)


def list_labels(actor: Actor) -> list[str]:
    """The customer buttons on the shop's list screen, without the status dot."""
    return [b.split(" ", 1)[1] for b in actor.buttons if b[:1] in "🟢🟡🔴⚪"]
