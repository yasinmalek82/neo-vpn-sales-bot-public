"""The iPhone endpoint has its own credentials and mounts only when enabled."""

from contextlib import asynccontextmanager

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tests.integration.test_bank_sms import config, reserve, seed, sms


@asynccontextmanager
async def client_for(factory, settings):
    from web.bank_sms import mount_bank_sms

    app = web.Application()
    mount_bank_sms(app, settings, factory)
    async with TestClient(TestServer(app)) as client:
        yield client


@pytest.mark.asyncio
async def test_disabled_endpoint_is_not_mounted(two_sessions):
    factory, _ = two_sessions
    async with client_for(factory, config(BANK_SMS_ENABLED=False)) as client:
        assert (await client.post("/bank-sms/blu", json={})).status == 404


@pytest.mark.asyncio
async def test_missing_wrong_or_query_token_is_refused_before_body(two_sessions):
    factory, _ = two_sessions
    async with client_for(factory, config()) as client:
        for headers in (
            {},
            {"Authorization": "Bearer wrong"},
            {"X-Telegram-Init-Data": "anything"},
        ):
            response = await client.post(
                "/bank-sms/blu?token=" + "x" * 40, headers=headers, data="invalid"
            )
            assert response.status == 401


@pytest.mark.asyncio
async def test_authenticated_request_records_manual_outcome_and_replay(two_sessions):
    factory, _ = two_sessions
    async with client_for(factory, config()) as client:
        headers = {"Authorization": "Bearer " + "x" * 40}
        payload = {"sender": "BluBank", "text": sms(128_000)}
        first = await client.post("/bank-sms/blu", headers=headers, json=payload)
        assert first.status == 200
        assert await first.json() == {"outcome": "manual_review"}
        assert first.headers["Cache-Control"] == "no-store"
        second = await client.post("/bank-sms/blu", headers=headers, json=payload)
        assert await second.json() == {"outcome": "duplicate"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"sender": "other", "text": sms(128_000)},
        {"sender": "BluBank", "text": "x" * 2049},
        {"sender": "BluBank", "text": 128000},
        {"sender": "BluBank", "text": sms(128_000), "order_id": 1},
    ],
)
async def test_bad_payload_is_rejected(two_sessions, payload):
    factory, _ = two_sessions
    async with client_for(factory, config()) as client:
        response = await client.post(
            "/bank-sms/blu", headers={"Authorization": "Bearer " + "x" * 40}, json=payload
        )
        assert response.status == 400


def test_enabled_configuration_fails_closed_without_scheduler_or_https():
    from services.bank_invoices import require_bank_sms_config

    for bad in (
        {"SCHEDULER_ENABLED": False},
        {"BANK_SMS_TOKEN": "short"},
        {"BANK_SMS_URL": "http://host/bank-sms/blu"},
        {"BANK_SMS_URL": "https://host/wrong"},
        {"BANK_SMS_SENDER": ""},
    ):
        with pytest.raises(RuntimeError, match="BANK_SMS"):
            require_bank_sms_config(config(**bad))


@pytest.mark.asyncio
async def test_invoice_screen_shows_exact_amount_and_wallet_return(two_sessions):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from bot.handlers.payment import begin_payment_wait

    class Message:
        body = ""

        async def answer(self, text, *, reply_markup):
            self.body = text
            self.keyboard = reply_markup

    factory, _ = two_sessions
    _, _, order_id = await seed(factory)
    invoice = await reserve(factory, order_id)
    message = Message()
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=1, user_id=1))
    async with factory() as session:
        await begin_payment_wait(
            message, state, config(), session=session, order_id=order_id, amount_toman=150_000
        )
    body = message.body.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٬", "0123456789,"))
    assert f"<b>{invoice.amount_toman * 10:,}</b> ریال" in body
    assert message.keyboard.inline_keyboard[0][-1].copy_text.text == str(invoice.amount_toman * 10)
    callbacks = {button.callback_data for row in message.keyboard.inline_keyboard for button in row}
    assert f"paycancel:{order_id}" in callbacks
    assert f"payreceipt:{order_id}" in callbacks
    assert f"{invoice.suffix_toman * 10:,} ریال" in body
    assert "کیف پول" in body
    assert (await state.get_data())["pending_amount"] == invoice.amount_toman
    await storage.close()
