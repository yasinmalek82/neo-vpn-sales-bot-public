"""Administrator source-history review is read-only, paged and access-controlled."""

from copy import deepcopy

from sqlalchemy import func, select

from database.models import MirzaBatch, MirzaRecord, User, WalletLedgerEntry
from services.mirza_import import apply_migration, plan_migration
from tests.e2e._s07_helpers import open_customer_card
from tests.e2e.harness import App
from tests.integration.test_mirza_import import policy, source


async def imported(app: App, *, many=False):
    raw = deepcopy(source().tables)
    raw["user"][0]["username"] = "legacy_ali"
    if many:
        for index in range(6):
            raw["invoice"].append(
                {
                    "id_invoice": f"archive-{index}",
                    "id_user": "1001",
                    "username": f"old_service_{index}",
                    "Status": "disabled",
                    "price_product": "70000",
                }
            )
    src = source()
    plan = plan_migration(type(src)(src.sha256, raw), policy())
    async with app.session() as session:
        await apply_migration(session, plan, confirm_sha256=plan.sha256)
        await session.commit()
        return await session.scalar(select(User.id).where(User.telegram_id == 1001))


async def test_admin_reads_customer_history_and_review_queue_without_money_mutation(app: App):
    user_id = await imported(app)
    admin = app.admin()
    await open_customer_card(admin, "@legacy_ali")
    await admin.tap("سوابق میرزا")
    assert "90,000 تومان" in admin.last_text
    assert "نیازمند بررسی" in admin.last_text
    assert "legacy_exact" in admin.last_text
    await admin.tap("فقط نیازمند بررسی")
    assert "پرداخت" in admin.last_text and "تأیید نشده" in admin.last_text
    await admin.tap("خانه ادمین")
    await admin.tap("فروش و سفارش")
    await admin.tap("بررسی میرزا")
    assert "تأیید نشده" in admin.last_text
    async with app.session() as session:
        assert (
            await session.scalar(select(User.wallet_balance_toman).where(User.id == user_id))
            == 125000
        )
        assert await session.scalar(select(func.count()).select_from(WalletLedgerEntry)) == 1
        assert await session.scalar(select(func.count()).select_from(MirzaRecord)) == 5
    assert app.panel.requests == []


async def test_history_pagination_and_unknown_currency_do_not_invent_amounts(app: App):
    await imported(app, many=True)
    async with app.session() as session:
        batch = await session.get(MirzaBatch, "mirza-cutover")
        batch.source_unit = None
        await session.commit()
    admin = app.admin()
    await open_customer_card(admin, "@legacy_ali")
    await admin.tap("سوابق میرزا")
    assert "واحد مبلغ نامشخص" in admin.last_text
    assert "تومان" not in admin.last_text
    first = admin.last_text
    await admin.tap("بعدی")
    assert admin.last_text != first
    await admin.tap("قبلی")
    assert admin.last_text == first


async def test_customer_cannot_forge_admin_legacy_history_callback(app: App):
    user_id = await imported(app)
    customer = app.customer("outsider")
    await customer.start()
    mark = customer.mark()
    await customer.press(f"adm:mh:{user_id}:0:0")
    assert customer.messages_since(mark) == []
    assert "legacy_exact" not in (customer.popup or "")
