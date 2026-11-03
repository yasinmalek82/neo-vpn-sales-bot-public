"""The administrator reviews a real existing name before changing local ownership."""

from dataclasses import asdict

from sqlalchemy import func, select

from bot.keyboards.reply_menu import ACCOUNT
from database.models import Order, PasarGuardAccount, WalletLedgerEntry
from tests.e2e._s07_helpers import db_user, open_customer_card
from tests.e2e._s16_helpers import panel_user
from tests.e2e.harness import App


async def test_admin_can_preview_confirm_and_replay_existing_account_link(app: App):
    buyer = app.customer("ali")
    await buyer.start()
    existing = panel_user(app, "friend_exact", gb=10, used_gb=2)
    before = asdict(existing)
    admin = app.admin()
    await open_customer_card(admin, "ali")
    await admin.tap("اتصال سرویس موجود")
    await admin.send("friend_exact")
    assert "friend_exact" in admin.last_text
    assert await app.all(select(PasarGuardAccount)) == []
    preview = admin.last_message
    await admin.tap("تأیید اتصال")
    account = await app.one(select(PasarGuardAccount))
    assert account.user_id == (await db_user(app, buyer)).id
    assert account.panel_username == "friend_exact" and account.delete_exempt is True
    confirm = next(
        b.callback_data
        for row in preview.markup.inline_keyboard
        for b in row
        if "تأیید اتصال" in b.text
    )
    await admin.press(confirm, message=preview)
    assert len(await app.all(select(PasarGuardAccount))) == 1
    await buyer.tap_reply(ACCOUNT)
    await buyer.tap("friend_exact")
    assert "friend_exact" in buyer.last_text
    async with app.session() as session:
        for model in (Order, WalletLedgerEntry):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
    assert asdict(existing) == before
    assert all(request.method == "GET" for request in app.panel.requests)


async def test_admin_cannot_capture_another_customers_existing_account(app: App):
    buyer, other = app.customer("ali"), app.customer("reza")
    await buyer.start()
    await other.start()
    existing = panel_user(app, "other_exact", gb=10)
    owner = await db_user(app, other)
    async with app.session() as session:
        session.add(
            PasarGuardAccount(
                user_id=owner.id, panel_user_id=existing.id, panel_username=existing.username
            )
        )
        await session.commit()
    admin = app.admin()
    await open_customer_card(admin, "ali")
    await admin.tap("اتصال سرویس موجود")
    await admin.send("other_exact")
    assert "مشتری دیگری" in admin.last_text
    assert (await app.one(select(PasarGuardAccount))).user_id == owner.id
    assert all(request.method == "GET" for request in app.panel.requests)
