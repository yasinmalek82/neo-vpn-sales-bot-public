# e2e scenarios: driving the whole bot like a customer

`tests/e2e` feeds real Telegram `Update`s to `bot.main.create_dispatcher()` (real routers and
middlewares) on a migrated SQLite file, a **strict fake Telegram** and a **fake PasarGuard** checked
against `docs/vendor/pasarguard-openapi.json`. Scenario markers retain historical checklist IDs; the private operational checklist is not distributed.

## Write a scenario
```python
from bot.texts import texts
from tests.e2e.harness import App, order_by_card, plain, publish_store, smoke

@smoke("۴.۶")                                   # the row id, exactly as the checklist writes it
async def test_text_instead_of_receipt_is_refused(app: App) -> None:
    store = await publish_store(app)             # a realistic published catalog
    ali = app.customer("ali", first_name="Ali")
    await ali.start()
    await order_by_card(ali, store)              # shelf -> name -> summary -> card instructions
    mark = ali.mark()
    await ali.send("پرداخت کردم")
    assert ali.texts_since(mark) == [plain(texts.payment_need_photo)]
```
Example files: `test_smoke_s04_card_purchase.py` (money path), `test_smoke_s12_security.py`.
Run: `BOT_TOKEN=test-token SCHEDULER_ENABLED=false python -m pytest tests/e2e -q`.

## Actors: `app.customer(name, first_name=, username=)`, `app.admin()`
`await a.start(payload=None)` / `send(text)` / `send_photo()` / `tap("label")` (inline, newest message that has
it; a piece of the label works if it is unambiguous; the error lists what is on screen) /
`tap_reply("label")` (bottom keyboard; must be on screen) / `press(callback_data)` (stale or forged button).
Reading: `a.last_text` (as read, tags gone), `last_html`, `buttons`, `rows`, `reply_buttons`, `photos`,
`messages`, `popup` (last callback toast/alert), `mark()` + `messages_since(mark)` / `texts_since(mark)`.
Each message has `.text .html .kind .buttons .file_id .file_bytes`. `plain(html)` strips tags from copy,
`digits(s)` makes «۱۵۰٬۰۰۰» and `150,000` both `150000`. Prefer `texts.<name>` over retyping Persian.
An admin is created as someone who has used the bot before; `app.admin(started=False)` is one who never pressed /start.

## App
`app.panel` (fake PasarGuard), `app.tg` (fake Telegram), `app.settings`, `app.session()` (async with),
`app.latest_order(actor)`, `app.one(select)`, `app.all(select)`, `app.give_wallet(actor, toman)`,
`app.wallet_balance(actor)`, `app.age_order(id, minutes=)` / `app.age(Model, id, days=)` (move a row back in
time), `await app.run_job("expire")` (also `report_flush broadcast reminders digest panel_watch cleanup backup`,
run with the exact arguments production registers), `await app.connect_report_group()` then
`await group.texts("sales")` (topics: sales payments wallet customers errors system backup disruptions).
Helpers: `publish_store(app)` (`store.plans["special_50"].price`, `store.groups[...]`), `browse_to_plan`,
`order_by_card`, `pay_by_card`. They cover ready *volume* plans; tap other shapes yourself.

## Panel: assert and break it
`app.panel.user(name)` -> `.gb .data_limit .days_left() .hwid_limit .group_ids .status .token`; `only_user()`;
`requests_to("POST /api/user")`. Faults: `fail_next("POST /api/user", status=500)`, `timeout_next(path,
applied=True)` (applied, reply lost), `forbid(path)`/`allow(path)`, `go_down()`/`go_up()`, `reject_key()`.

## The harness fails a scenario for you
After the body, `final_checks` raises if (a) the fake Telegram rejected something real Telegram rejects (bare `<`,
callback_data over 64 bytes, empty text/button, edit "not modified", answered a callback twice ...);
(b) the bot sent the panel a request the OpenAPI file refuses (wrong field, bad type); (c) a handler raised and the
error middleware swallowed it. Opt out only when the row is about that: `app.tg.accept("html")`,
`app.panel.allow_violations()`, `app.expect_unhandled_errors()`. A tap the bot never answers raises
`UnansweredCallback` at once (a spinning button).

## A job committing mid-request
`app.commit_at_every_network_call()` makes another connection commit before every Bot API and panel call,
which is what a scheduler job does in production. A handler that reads, awaits the network and writes on the
same snapshot then dies with «database is locked» and `final_checks` fails the scenario. Run every scenario
that way with `E2E_COMMIT_AT_NETWORK=1` after adding a handler that calls Telegram or the panel.
`app.tg.flood_next("EditForumTopic", retry_after=3)` throttles the next call of that method.

## Rules
- Tests only: never edit `bot/ services/ api_client/ database/ scheduler/ alembic/`. Do not edit `harness/`:
  put what is missing in your own `tests/e2e/_sNN_helpers.py` and name it in your report.
- If the bot is wrong, write the *correct* behaviour and mark it
  `@pytest.mark.xfail(strict=True, reason="ARCH-x.y or BUG: one line")`. Do not weaken the assertion.
- A row that needs a real VPN, phone, panel or Telegram is MANUAL: leave it out and say why.
- Never touch `data/bot.db`, `.env`, the network or the real bot process. Suite budget: under 90 s.
