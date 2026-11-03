import asyncio
import logging
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from bot.client import create_bot
from bot.commands import setup_bot_commands
from bot.config import Settings, settings
from bot.handlers import register_handlers
from bot.keyboards.style import set_enabled as set_button_colors_enabled
from bot.middlewares import register_middlewares
from bot.runtime_health import RuntimeHealth
from database.base import close_db, get_session_factory, init_db
from scheduler import setup_scheduler, shutdown_scheduler
from services.bank_invoices import require_bank_sms_config
from web.app import mount_web_panel, require_web_panel_url, start_polling_panel
from web.bank_sms import mount_bank_sms

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper()),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
# httpx logs every request URL at INFO, and the subscription URL carries the
# customer's token (`api_client/client.py` `get_subscription_info`).
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def _init_sentry() -> None:
    """Initialize Sentry when SENTRY_DSN is set; no-op otherwise (local/CI safe)."""
    dsn = (settings.sentry_dsn or "").strip()
    if not dsn:
        return
    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        environment=(settings.sentry_environment or "").strip() or None,
        traces_sample_rate=settings.sentry_traces_sample_rate,
    )
    logger.info(
        "Sentry initialized (environment=%s, traces_sample_rate=%s)",
        settings.sentry_environment or "(default)",
        settings.sentry_traces_sample_rate,
    )


_init_sentry()


def require_webhook_secret(app_settings: Settings) -> str:
    """Return the stripped webhook secret, or fail closed in webhook mode.

    ``SimpleRequestHandler(secret_token=None)`` skips Telegram secret checks.
    Webhook mode must never start without a non-empty secret.
    """
    secret = (app_settings.webhook_secret or "").strip()
    if app_settings.is_webhook_mode and not secret:
        raise RuntimeError(
            "WEBHOOK_SECRET is required when RUN_MODE=webhook "
            "(set a non-empty secret and pass it to Telegram + SimpleRequestHandler)"
        )
    return secret


def build_webhook_handler(
    dp: Dispatcher,
    bot: Bot,
    secret: str,
) -> SimpleRequestHandler:
    """Construct the aiohttp webhook handler; always set ``secret_token``.

    An empty/None token would disable ``verify_secret`` — refuse that here.
    """
    token = (secret or "").strip()
    if not token:
        raise RuntimeError(
            "webhook handler requires a non-empty secret_token "
            "(refusing secret_token=None which skips Telegram secret checks)"
        )
    return SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=token,
    )


@asynccontextmanager
async def lifespan(_app: web.Application) -> AsyncGenerator[None, None]:
    logger.info("Starting bot...")
    init_db(settings)
    logger.info("Database initialized")
    yield
    logger.info("Shutting down bot...")
    await shutdown_scheduler()
    await close_db()
    logger.info("Database closed")


def create_dispatcher() -> Dispatcher:
    # SimpleEventIsolation serialises updates that share a storage key (same chat
    # and user). Without it aiogram runs two quick taps of one customer
    # concurrently, so a double-tapped confirm button reads the same pending order
    # twice and can charge or provision twice. It must stay (see CLAUDE.md).
    dp = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    dp["settings"] = settings
    dp["session_factory"] = get_session_factory()

    register_middlewares(dp)
    register_handlers(dp)
    return dp


async def on_startup(bot: Bot) -> None:
    logger.info("Bot started")
    await setup_bot_commands(bot, settings.admin_telegram_ids)
    if settings.is_webhook_mode:
        secret = require_webhook_secret(settings)
        if not settings.webhook_url:
            raise RuntimeError(
                "WEBHOOK_HOST/WEBHOOK_PATH required when RUN_MODE=webhook "
                "(incomplete webhook config)"
            )
        await bot.set_webhook(
            url=settings.webhook_url,
            secret_token=secret,
            drop_pending_updates=True,
        )
        logger.info("Webhook set to %s", settings.webhook_url)
    else:
        await bot.delete_webhook(drop_pending_updates=settings.polling_drop_pending_updates)
        logger.info("Webhook deleted, running in polling mode")

    setup_scheduler(
        bot=bot,
        session_factory=get_session_factory(),
        settings=settings,
    )


async def on_shutdown(bot: Bot) -> None:
    logger.info("Bot stopping")
    await shutdown_scheduler()
    if settings.is_webhook_mode:
        await bot.delete_webhook()
        logger.info("Webhook deleted")


async def main() -> None:
    # A panel switched on without a usable https URL stops the bot here, before
    # the database opens (no-op while WEB_PANEL_ENABLED is off).
    require_web_panel_url(settings)
    require_bank_sms_config(settings)
    # Before any keyboard is built: the colour switch is read once here so a
    # bad render on launch day is one env var away from off.
    set_button_colors_enabled(settings.button_colors_enabled)
    init_db(settings)
    bot = create_bot(settings)
    dp = create_dispatcher()
    health = RuntimeHealth("webhook" if settings.is_webhook_mode else "polling")

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    try:
        health.start(bot)
        if settings.is_webhook_mode:
            secret = require_webhook_secret(settings)
            app = web.Application()
            webhook_handler = build_webhook_handler(dp, bot, secret)
            app.router.add_post(settings.webhook_path, webhook_handler.handle)
            setup_application(app, dp, bot=bot)
            mount_web_panel(app, settings, get_session_factory())  # adds nothing while off
            mount_bank_sms(app, settings, get_session_factory())
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", settings.webhook_port)
            await site.start()
            health.listener_ready(lambda: site in runner.sites and bool(runner.addresses))
            logger.info("Webhook server started on port %s", settings.webhook_port)
            try:
                await asyncio.Event().wait()
            finally:
                await runner.cleanup()
        else:
            # Polling has no HTTP server; with the panel on, one runs beside it.
            panel_runner = await start_polling_panel(settings, get_session_factory())
            try:
                await dp.start_polling(bot)
            finally:
                if panel_runner is not None:
                    await panel_runner.cleanup()
    finally:
        await health.close()
        await shutdown_scheduler()
        await close_db()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
