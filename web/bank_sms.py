"""Dedicated credential for the iPhone; never accepts Telegram or query tokens."""

import hmac
import json
import logging

from aiohttp import web
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from services.bank_invoices import BANK_SMS_PATH, require_bank_sms_config
from services.bank_sms import BankSmsError, receive_bank_sms
from services.payments import PaymentError

logger = logging.getLogger(__name__)


def mount_bank_sms(
    app: web.Application, settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> bool:
    if not settings.bank_sms_enabled:
        return False
    require_bank_sms_config(settings)

    async def receive(  # noqa: PLR0911 - distinct protocol failures
        request: web.Request,
    ) -> web.Response:
        expected = f"Bearer {settings.bank_sms_token}".encode()
        if not hmac.compare_digest(request.headers.get("Authorization", "").encode(), expected):
            return web.json_response({"error": "unauthorized"}, status=401)
        # Bound decoded/chunked bodies too, before reading into memory.
        body = bytearray()
        async for chunk in request.content.iter_chunked(1024):
            body.extend(chunk)
            if len(body) > 8192:
                return web.json_response({"error": "invalid_payload"}, status=413)
        try:
            payload = json.loads(body)
            if (
                not isinstance(payload, dict)
                or set(payload) != {"sender", "text"}
                or not isinstance(payload["sender"], str)
                or not isinstance(payload["text"], str)
            ):
                raise BankSmsError("invalid_payload")
        except (ValueError, UnicodeError):
            return web.json_response({"error": "invalid_payload"}, status=400)
        async with session_factory() as session:
            try:
                result = await receive_bank_sms(
                    session, settings, text=payload["text"], sender=payload["sender"]
                )
                # The bank event, approval, wallet suffix and reports commit together.
                # Existing provision recovery/delivery workers consume the paid order.
                await session.commit()
            except BankSmsError:
                await session.rollback()
                return web.json_response({"error": "invalid_sms"}, status=400)
            except (PaymentError, IntegrityError, OperationalError):
                await session.rollback()
                return web.json_response(
                    {"error": "retry"}, status=409, headers={"Retry-After": "5"}
                )
            except Exception:
                await session.rollback()
                # Exception text may contain SQL parameters. Do not log it.
                logger.error(  # noqa: TRY400 - private SQL parameters in exception
                    "Bank SMS processing failed; transaction rolled back"
                )
                return web.json_response(
                    {"error": "retry"}, status=503, headers={"Retry-After": "5"}
                )
        return web.json_response({"outcome": result.outcome}, headers={"Cache-Control": "no-store"})

    app.router.add_post(BANK_SMS_PATH, receive)
    return True
