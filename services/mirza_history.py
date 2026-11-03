"""Read-only projections of immutable source history for administrator review."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models.mirza_import import MirzaBatch, MirzaRecord
from database.models.user import User
from services.mirza_source import DumpError, MirzaDump

PAGE_SIZE = 4
_AMOUNT_FIELDS = {
    "user": "Balance",
    "invoice": "price_product",
    "Payment_report": "price",
    "service_other": "price",
}
_STATUS_FIELDS = {
    "user": "User_Status",
    "invoice": "Status",
    "Payment_report": "payment_Status",
    "service_other": "status",
}
_TIME_FIELDS = {
    "user": "register",
    "invoice": "time_sell",
    "Payment_report": "time",
    "service_other": "time",
}


@dataclass(frozen=True)
class HistoryRow:
    namespace: str
    source_table: str
    source_key: str
    user_id: int | None
    telegram_id: int | None
    disposition: str
    source_unit: str | None
    amount_toman: int | None
    status: str | None
    username: str | None
    source_time: str | None
    references: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class HistoryPage:
    rows: tuple[HistoryRow, ...]
    total: int
    offset: int


def _view(record: MirzaRecord, unit: str | None, telegram_id: int | None) -> HistoryRow:
    data = record.payload
    amount = None
    raw = data.get(_AMOUNT_FIELDS.get(record.source_table, ""))
    if isinstance(raw, str) and unit in {"toman", "rial"}:
        with suppress(DumpError):
            amount = MirzaDump.money(raw, source_unit=unit)
    return HistoryRow(
        namespace=record.namespace,
        source_table=record.source_table,
        source_key=record.source_key,
        user_id=record.target_user_id,
        telegram_id=telegram_id,
        disposition=record.disposition,
        source_unit=unit,
        amount_toman=amount,
        status=data.get(_STATUS_FIELDS.get(record.source_table, "")),
        username=data.get("username"),
        source_time=data.get(_TIME_FIELDS.get(record.source_table, "")),
        references=tuple(
            (key, data[key])
            for key in ("id_payment", "id_invoice", "id_order")
            if isinstance(data.get(key), str) and data[key] not in {"", "0", "none", "None"}
        ),
    )


async def history_page(
    session: AsyncSession, *, user_id: int | None, review_only: bool = False, offset: int = 0
) -> HistoryPage:
    filters = []
    if user_id is not None:
        filters.append(MirzaRecord.target_user_id == user_id)
    if review_only:
        filters.append(MirzaRecord.disposition == "quarantined")
    total = int(
        await session.scalar(select(func.count()).select_from(MirzaRecord).where(*filters)) or 0
    )
    offset = min(max(0, offset), max(0, ((total - 1) // PAGE_SIZE) * PAGE_SIZE))
    rows = (
        await session.execute(
            select(MirzaRecord, MirzaBatch.source_unit, User.telegram_id)
            .join(MirzaBatch, MirzaBatch.namespace == MirzaRecord.namespace)
            .outerjoin(User, User.id == MirzaRecord.target_user_id)
            .where(*filters)
            .order_by(MirzaRecord.namespace, MirzaRecord.source_table, MirzaRecord.source_key)
            .offset(offset)
            .limit(PAGE_SIZE)
        )
    ).all()
    return HistoryPage(
        tuple(_view(record, unit, telegram_id) for record, unit, telegram_id in rows), total, offset
    )
