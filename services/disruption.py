"""Customer disruption reports: which ISP, which city, which services.

Direct inbounds work on some ISPs and not others, and one city's ISP behaves
differently from another's. The owner fixes the ISPs with the most reports
first, so the ISP is a fixed key (counted) and the city is cleaned text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import DisruptionReport, Order, OrderStatus, User
from database.models.report import TOPIC_DISRUPTIONS
from services.report_events import disruption_reported
from services.report_outbox import enqueue_report
from services.store_config import get_published_store_config

# Order is the button order. Keys are what gets stored and counted.
ISPS: tuple[tuple[str, str], ...] = (
    ("mci", "همراه اول"),
    ("irancell", "ایرانسل"),
    ("rightel", "رایتل"),
    ("tci", "مخابرات"),
    ("shatel", "شاتل"),
    ("mobinnet", "مبین‌نت"),
    ("hiweb", "های‌وب"),
    ("asiatech", "آسیاتک"),
    ("parsonline", "پارس‌آنلاین"),
    ("other", "سایر"),
)
_ISP_LABELS = dict(ISPS)

CITIES: tuple[str, ...] = ("تهران", "مشهد", "اصفهان", "کرج", "شیراز", "تبریز", "اهواز", "قم")

# One report per person per half hour: repeated taps must not outvote an ISP.
COOLDOWN = timedelta(minutes=30)

_CITY_MAX = 40
# Letters (Persian or Latin), spaces, ZWNJ and a hyphen — a city name, not a
# link or a phone number typed into the wrong box.
_CITY_OK = re.compile(r"^[؀-ۿa-zA-Z‌\- ]+$")


class DisruptionError(Exception):
    """``unknown_isp`` | ``bad_city`` | ``too_soon``."""


@dataclass(frozen=True)
class IspCount:
    isp: str
    label: str
    count: int
    top_city: str | None


def isp_label(key: str) -> str | None:
    return _ISP_LABELS.get(key)


def normalize_city(raw: str) -> str | None:
    """«  تهران » → «تهران»; Arabic ي/ك → Persian; ``None`` for anything else."""
    text = (raw or "").replace("ي", "ی").replace("ك", "ک")
    text = re.sub(r"\s+", " ", text).strip()
    if not 2 <= len(text) <= _CITY_MAX or not _CITY_OK.match(text):
        return None
    if not re.search(r"[؀-ۿa-zA-Z]", text):
        return None
    return text


async def _service_groups(session: AsyncSession, user_id: int) -> str | None:
    """Store group titles of the reporter's live services, «direct or tunnel?»."""
    cfg = await get_published_store_config(session)
    if cfg is None:
        return None
    cat_title = {str(c.slug): str(c.title_fa) for c in cfg.categories}
    cat_by_id = {int(c.id): str(c.title_fa) for c in cfg.categories}
    pkg_cat = {str(p.slug): cat_by_id.get(int(p.category_id)) for p in cfg.packages}
    rows = await session.execute(
        select(Order.store_package_slug, Order.trial_category_slug).where(
            Order.user_id == user_id, Order.status == OrderStatus.ACTIVE
        )
    )
    titles: list[str] = []
    for pkg_slug, trial_slug in rows:
        title = pkg_cat.get(str(pkg_slug)) if pkg_slug else cat_title.get(str(trial_slug))
        if title and title not in titles:
            titles.append(title)
    return "، ".join(titles)[:160] or None


async def record_disruption(
    session: AsyncSession,
    *,
    user: User,
    isp: str,
    city: str,
    now: datetime | None = None,
) -> DisruptionReport:
    """Store one report and queue it for the report group.

    Raises ``DisruptionError`` for an unknown ISP key, a city that is not a
    city name, or a second report inside ``COOLDOWN``.
    """
    label = isp_label(isp)
    if label is None:
        raise DisruptionError("unknown_isp")
    clean_city = normalize_city(city)
    if clean_city is None:
        raise DisruptionError("bad_city")
    moment = now or datetime.now(UTC)
    recent = await session.scalar(
        select(DisruptionReport.id)
        .where(
            DisruptionReport.user_id == user.id,
            DisruptionReport.created_at >= moment - COOLDOWN,
        )
        .limit(1)
    )
    if recent is not None:
        raise DisruptionError("too_soon")

    row = DisruptionReport(
        user_id=user.id,
        isp=isp,
        city=clean_city,
        service_groups=await _service_groups(session, int(user.id)),
        created_at=moment,
    )
    session.add(row)
    await session.flush()
    report = disruption_reported(
        report_id=int(row.id),
        telegram_id=int(user.telegram_id),
        name=user.first_name,
        username=user.username,
        is_reseller=bool(user.is_reseller),
        isp=label,
        city=clean_city,
        service_groups=row.service_groups,
        when=moment,
        link_user_id=int(user.id),
    )
    await enqueue_report(
        session,
        event_key=report.event_key,
        topic_key=TOPIC_DISRUPTIONS,
        body=report.body,
        link_user_id=report.link_user_id,
    )
    return row


async def disruption_counts(
    session: AsyncSession, *, start: datetime, end: datetime
) -> list[IspCount]:
    """ISPs by report count in ``[start, end)``, most first, each with its top city."""
    per_city = (
        await session.execute(
            select(DisruptionReport.isp, DisruptionReport.city, func.count())
            .where(DisruptionReport.created_at >= start, DisruptionReport.created_at < end)
            .group_by(DisruptionReport.isp, DisruptionReport.city)
        )
    ).all()
    totals: dict[str, int] = {}
    top: dict[str, tuple[int, str]] = {}
    for isp, city, count in per_city:
        totals[isp] = totals.get(isp, 0) + int(count)
        best = top.get(isp)
        if best is None or int(count) > best[0] or (int(count) == best[0] and city < best[1]):
            top[isp] = (int(count), str(city))
    ranked = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    return [
        IspCount(
            isp=isp,
            label=isp_label(isp) or isp,
            count=count,
            top_city=top[isp][1] if isp in top else None,
        )
        for isp, count in ranked
    ]
