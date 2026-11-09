"""Register all routers."""

from __future__ import annotations

from aiogram import Dispatcher

from bot.handlers import (
    account,
    account_link,
    admin,
    admin_customers,
    admin_wallet,
    common,
    customer_label,
    disruption,
    lifecycle_admin,
    marketing_admin,
    menu,
    mirza_history,
    onboarding,
    payment,
    purchase,
    referral,
    reply_menu,
    store_admin,
    wallet,
)


def register_handlers(dp: Dispatcher) -> None:
    # First: /cancel must beat every state-scoped message handler.
    dp.include_router(common.router)
    # Second: a bottom-keyboard tap is navigation, so it must beat any
    # state-scoped message handler waiting for typed input — same reason
    # /cancel goes first.
    dp.include_router(reply_menu.router)
    dp.include_router(onboarding.router)
    dp.include_router(menu.router)
    dp.include_router(disruption.router)
    # Before purchase/account/referral: it owns the two label states those
    # three flows hand off to, and nothing else answers them.
    dp.include_router(customer_label.router)
    dp.include_router(purchase.router)
    dp.include_router(payment.router)
    dp.include_router(account.router)
    dp.include_router(wallet.router)
    dp.include_router(referral.router)
    dp.include_router(admin.router)
    dp.include_router(admin_customers.router)
    dp.include_router(admin_wallet.router)
    dp.include_router(account_link.router)
    dp.include_router(mirza_history.router)
    dp.include_router(store_admin.router)
    dp.include_router(lifecycle_admin.router)
    dp.include_router(marketing_admin.router)
    # Last: answers callbacks nothing above claimed, so stale buttons
    # report themselves instead of spinning until Telegram times out.
    dp.include_router(common.fallback_router)
