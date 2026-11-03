"""Administrator access to source history and unresolved Mirza records."""

from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.handlers.admin import _deny_if_not_admin, _tg_message
from bot.handlers.common import edit_or_send
from bot.keyboards.mirza_history import MirzaCB, mirza_history_keyboard
from bot.texts import texts
from database.base import end_read_transaction
from database.models import User
from services.mirza_history import HistoryPage, history_page
from services.ux_format import fa_toman, to_persian_digits

router = Router(name="mirza_history")
_TABLE = {
    "user": "حساب میرزا",
    "invoice": "سرویس",
    "Payment_report": "پرداخت",
    "service_other": "عملیات سرویس",
    "marzban_panel": "پنل",
}
_DISPOSITION = {
    "user": "انتقال‌شده",
    "linked": "متصل",
    "archived": "آرشیوشده",
    "quarantined": "نیازمند بررسی",
    "history": "سابقه",
}
_STATUS = {
    "active": "فعال",
    "block": "مسدود",
    "paid": "پرداخت‌شده",
    "unpaid": "تأیید نشده",
    "send_on_hold": "در انتظار",
    "expire": "منقضی",
    "expired": "منقضی",
    "reject": "ردشده",
    "rejected": "ردشده",
    "disabled": "غیرفعال",
    "disabledn": "غیرفعال",
    "removetime": "پایان زمان",
    "removevolume": "پایان حجم",
    "removebyadmin": "حذف مدیر",
    "removedbyadmin": "حذف مدیر",
    "removebyuser": "حذف مشتری",
    "unsuccessful": "ناموفق",
}
_REF = {
    "id_payment": "شناسهٔ پرداخت منبع",
    "id_invoice": "شناسهٔ سرویس منبع",
    "id_order": "شناسهٔ سفارش منبع",
}


def _parse(raw: str) -> tuple[int | None, int, bool] | None:
    parts = raw.removeprefix(MirzaCB.PAGE).split(":")
    if len(parts) != 3 or parts[2] not in {"0", "1"}:
        return None
    scope, offset, review = parts
    if (
        not offset.isascii()
        or not offset.isdecimal()
        or len(offset) > 10
        or int(offset) > 2147483647
    ):
        return None
    if scope == "all":
        return (None, int(offset), True) if review == "1" else None
    if (
        not scope.isascii()
        or not scope.isdecimal()
        or len(scope) > 19
        or not 0 < int(scope) <= 9223372036854775807
    ):
        return None
    return int(scope), int(offset), review == "1"


def _text(page: HistoryPage, *, review_only: bool) -> str:
    title = texts.admin_mirza_review_title if review_only else texts.admin_mirza_history_title
    blocks = [title.format(count=to_persian_digits(page.total))]
    if not page.rows:
        blocks.append(texts.admin_mirza_empty)
    for row in page.rows:
        amount = (
            texts.admin_mirza_toman_amount.format(amount=fa_toman(row.amount_toman))
            if row.amount_toman is not None
            else (
                texts.admin_mirza_unit_unknown
                if row.source_unit is None
                else texts.admin_mirza_amount_unknown
            )
        )
        lines = [
            f"<b>{_TABLE.get(row.source_table, 'سابقه')}</b> <code>{escape(row.namespace)}:{escape(row.source_key)}</code>",
            texts.admin_mirza_record_status.format(
                status=_STATUS.get((row.status or "").casefold(), "ثبت قدیمی"),
                disposition=_DISPOSITION.get(row.disposition, "نیازمند بررسی"),
            ),
            texts.admin_mirza_record_amount.format(amount=amount),
        ]
        if row.username:
            lines.append(f"<code>{escape(row.username[:64])}</code>")
        if row.source_time:
            lines.append(texts.admin_mirza_source_time.format(value=escape(row.source_time[:80])))
        for key, value in row.references:
            lines.append(f"{_REF[key]}: <code>{escape(value[:128])}</code>")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


@router.callback_query(F.data.startswith(MirzaCB.PAGE))
async def show_mirza_history(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    db_user: User | None,
) -> None:
    if await _deny_if_not_admin(callback, settings, db_user):
        return
    parsed = _parse(callback.data or "")
    if parsed is None:
        await callback.answer(texts.admin_panel_invalid, show_alert=True)
        return
    user_id, offset, review = parsed
    await callback.answer()
    await state.clear()
    page = await history_page(session, user_id=user_id, offset=offset, review_only=review)
    await end_read_transaction(session)
    message = _tg_message(callback)
    if message is not None:
        await edit_or_send(
            message,
            _text(page, review_only=review),
            mirza_history_keyboard(page, user_id=user_id, review_only=review),
        )
