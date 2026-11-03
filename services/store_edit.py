"""Store price edits, shared by the bot's typed prompt and the panel's inline cell.

``set_package_price`` is the bot's whole price edit (validate, write the draft,
publish with the Persian change-log note). ``edit_package_price`` is what the
panel adds around it: only a store owner, only against the store version the
page was drawn from, and the plan named by its slug.

**The version check is not a lock the page holds.** A page is drawn from the
published version N and carries N in its form. The write reads the published
version *inside the transaction that writes*, and refuses when it is not N, so
an edit made meanwhile (by the bot, by another tab, by an undo) is refused and
never merged. A double-submitted form therefore applies once: the first publish
moves N, the second form still says N.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.texts import texts
from database.models.store import (
    StorePackage,
    StorePackagePrice,
    StoreVersion,
    StoreVersionStatus,
)
from services.store_admin import (
    StoreAdminError,
    assert_store_owner,
    commit_and_activate,
    get_draft_package,
    get_or_create_draft,
    update_draft_package,
)
from services.store_pricing import DEFAULT_TIER, duration_from_package_options, get_price
from services.ux_format import format_toman

INT64_MAX = 2**63 - 1

_T = TypeVar("_T")

# Read straight back to the owner in «تغییرات فروشگاه», so Persian.
PACKAGE_EDIT_NOTE = "پلن ویرایش شد"

TEXT_STALE = "فروشگاه در این فاصله عوض شد. چیزی ذخیره نشد؛ صفحه را دوباره باز کن."
TEXT_PLAN_MISSING = "پلن یافت نشد؛ شاید همین الان حذف شده."


class StaleStoreVersion(StoreAdminError):
    """The catalog changed since the page was drawn; nothing was written."""


def parse_toman(raw: str) -> int | None:
    """Accept «150000», «150,000» and «۱۵۰٬۰۰۰»; reject anything else.

    Returning None instead of raising keeps the raw Python ``ValueError`` text
    («invalid literal for int()») out of the Persian UI. A value past SQLite's
    64-bit integer is also None: it would be an ``OverflowError`` from the
    driver at the write. That is the storage's bound, not a cap on prices.
    """
    text = raw.strip().replace(",", "").replace("٬", "").replace("،", "")
    text = text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    if not text.isdecimal():  # isdigit() lets «²» through and int() then raises
        return None
    value = int(text)
    return value if 0 <= value <= INT64_MAX else None


# A new price at least this many times the current one, or at most one this-many-th of
# it, is asked about once before it is saved. A warning, never a refusal: the owner may
# mean it, and a typo of a zero is exactly the slip it catches («۱۵۰٬۰۰۰» typed as
# «۱۵۰۰٬۰۰۰»).
PRICE_JUMP_FACTOR = 10


def price_jump(old: int | None, new: int) -> bool:
    """Whether ``new`` is a wildly different price from ``old`` (10x up or a tenth down).

    No current price (a plan with no cell yet) is nothing to compare with.
    """
    if old is None or old <= 0:
        return False
    return new >= old * PRICE_JUMP_FACTOR or new * PRICE_JUMP_FACTOR <= old


def price_text(value: int) -> str:
    """«۱۵۰٬۰۰۰ تومان», the way the bot's plan screens print a price."""
    return f"{format_toman(int(value), persian_digits=True)} تومان"


class PriceJumpNeedsConfirm(StoreAdminError):
    """The new price is a wildly different one; nothing was written. Ask, then call again.

    ``str(exc)`` is the bot's confirm sentence with the old and the new price. It is
    an ask, not a refusal: both the panel (a confirm page) and the bot (a confirm
    step) answer it by calling ``set_package_price`` again with ``confirmed=True``.
    """

    def __init__(self, *, title: str, old: int, new: int) -> None:
        super().__init__(
            texts.admin_store_price_jump_confirm.format(
                title=title, old=price_text(old), new=price_text(new)
            )
        )
        self.old = old
        self.new = new


async def current_retail_price(session: AsyncSession, pkg: StorePackage) -> int | None:
    """The plan's retail cell on its own version: the number the dashboard shows."""
    rows = list(
        await session.scalars(
            select(StorePackagePrice).where(
                StorePackagePrice.store_version_id == pkg.store_version_id,
                StorePackagePrice.package_slug == pkg.slug,
            )
        )
    )
    return get_price(
        rows,
        slug=str(pkg.slug),
        duration_days=duration_from_package_options(dict(pkg.options or {})),
        tier=DEFAULT_TIER,
    )


async def set_package_price(
    session: AsyncSession, package_id: int, raw: str, *, confirmed: bool = False
) -> StorePackage:
    """The retail price of a ready plan: validate, write the draft, publish, commit.

    A free service is the trial feature, not a zero-priced catalog plan, so
    zero is refused here with the other malformed input. A price that is 10 times
    the current one or a tenth of it raises ``PriceJumpNeedsConfirm`` before anything
    is written, unless the caller passes ``confirmed=True`` (the second request).
    """
    amount = parse_toman(raw)
    if amount is None or amount == 0:
        raise StoreAdminError(texts.admin_store_invalid_input)
    if not confirmed:
        pkg = await get_draft_package(session, package_id)
        old = await current_retail_price(session, pkg)
        if old is not None and price_jump(old, amount):
            raise PriceJumpNeedsConfirm(title=str(pkg.title_fa), old=old, new=amount)
    pkg = await update_draft_package(session, package_id, fixed_price_toman=amount)
    await commit_and_activate(session, notes=PACKAGE_EDIT_NOTE)
    return pkg


async def published_store_version(session: AsyncSession, *, lock: bool = False) -> int | None:
    """The published version number, read in the caller's transaction.

    ``lock`` is for the write path: ``FOR UPDATE`` makes a concurrent writer
    wait on a server database; SQLite ignores it and refuses the later writer
    instead (see ``edit_package_price``). A page read never locks.
    """
    stmt = (
        select(StoreVersion.version)
        .where(StoreVersion.status == StoreVersionStatus.published)
        .limit(1)
    )
    return await session.scalar(stmt.with_for_update() if lock else stmt)


async def assert_store_version(session: AsyncSession, expected: int | None) -> None:
    current = await published_store_version(session, lock=True)
    if current is None or expected is None or int(current) != int(expected):
        raise StaleStoreVersion(TEXT_STALE)


async def guarded_store_write(
    session: AsyncSession,
    settings: Settings,
    telegram_id: int | None,
    *,
    expected_version: int | None,
    action: Callable[[AsyncSession], Awaitable[_T]],
) -> _T:
    """Every panel write to the catalog: owner only, current version only.

    ``action`` runs in the transaction that read the published version, and ends
    in ``commit_and_activate``. The one place that maps SQLite's «database is
    locked» to the stale refusal, so a setting added later cannot forget it.
    """
    assert_store_owner(settings, telegram_id)
    try:
        await assert_store_version(session, expected_version)
        return await action(session)
    except StoreAdminError:
        await session.rollback()  # a draft flushed before the refusal must not stay
        raise
    except OperationalError as exc:
        # SQLite refuses a writer whose read snapshot another commit has passed
        # («database is locked», see database/base.py). That is this check
        # losing a race to a concurrent edit; nothing was written.
        await session.rollback()
        if "locked" in str(exc).lower():
            raise StaleStoreVersion(TEXT_STALE) from exc
        raise


async def edit_package_price(
    session: AsyncSession,
    settings: Settings,
    telegram_id: int | None,
    *,
    expected_version: int | None,
    package_slug: str,
    raw_price: str,
    confirmed: bool = False,
) -> StorePackage:
    """The panel's price edit: owner only, current version only, plan by slug.

    A wildly different price raises ``PriceJumpNeedsConfirm`` (nothing written, the
    draft rolled back); the page asks and sends the form again with ``confirmed``.
    """

    async def action(s: AsyncSession) -> StorePackage:
        draft = await get_or_create_draft(s)
        pkg = next((p for p in draft.packages if str(p.slug) == package_slug), None)
        if pkg is None:
            raise StoreAdminError(TEXT_PLAN_MISSING)
        return await set_package_price(s, int(pkg.id), raw_price, confirmed=confirmed)

    return await guarded_store_write(
        session, settings, telegram_id, expected_version=expected_version, action=action
    )
