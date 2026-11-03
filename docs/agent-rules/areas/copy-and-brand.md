---
paths:
  - "bot/client.py"
  - "tests/unit/test_report_delivery_numbers.py"
  - "bot/texts/**"
  - "services/brand.py"
  - "services/service_names.py"
  - "bot/handlers/admin.py"
  - "BRAND.md"
  - "tests/unit/test_brand_voice.py"
  - "tests/unit/test_brand_signature.py"
  - "tests/unit/test_customer_label.py"
  - "tests/unit/test_customer_copy_consistency.py"
  - "tests/unit/test_wallet_emoji.py"
---

# Customer copy and the brand

`BRAND.md` governs every string a customer reads. Admin copy is exempt; only
the owner reads it.

## Voice

- **«تو» everywhere.** Mixing «تو» and «شما» is worse than either. Colloquial
  spelling, and a number instead of an adjective.
- **At most one emoji per line** (BRAND.md §3). There is no cap on the whole
  message: the owner lifted «three per message» on 2026-10-03, because a
  multi-line card reads better when one emoji names each line. Pick the emoji
  that names what the line is about (💳 card payment, 📅 duration, 🏦 wallet,
  💰 an amount), never ✅ as decoration, and one emoji per concept across every
  screen so a button and the text under it agree.
- **A ✅/❌ legend heading two lists is fine** under «one per line»: each mark
  sits on its own line.
- **No raw `str(exc)` in user copy.** It leaks `invalid literal for int()` into
  the UI.

## The glossary: one emoji per concept

- **Rule: a line shaped «<emoji> برچسب: مقدار» carries the emoji its label has
  in `GLOSSARY` (`tests/unit/test_customer_copy_consistency.py`), on every
  screen.** 📦 حجم · 📊 مصرف حجم · 📅 مدت/انقضا/تمدید · ⏳ زمان باقی‌مانده ·
  🚦 وضعیت · 🔗 لینک · 📡 سرویس · 👤 مشتری/نام کارت · 🗂 نوع · 📂 گروه ·
  👥 کاربر همزمان · 💳 کارت (شماره، مبلغ کارت) · 🏛 بانک · 🧾 سفارش/خلاصه ·
  💰 a plain amount · 🏦 the wallet · 🏷 discount amount · 🎫 a code · 🎟 trial
  · 🎁 invite and reward · ⚠️ a warning · ❌ a refusal.
  Buttons and texts agree: the glossary follows the menu (🎟 trial, 🔗 link).
- **Why:** the same wallet had a purse in the bottom menu, a briefcase in
  the admin card and a bag of money on one button. The owner chose 🏦 on
  2026-10-03 (no wallet emoji exists) and the card's bank name moved to 🏛.
  `tests/unit/test_wallet_emoji.py` fails if the old purse or briefcase returns
  or a «… کیف پول» label carries any other emoji. The old bottom-menu label
  stays an alias in `reply_menu.py` so a chat showing the old keyboard still
  opens the wallet.
- A new label line goes into `GLOSSARY` with its emoji in the same change.

## Digits, punctuation, terms

- **Owner decision, 2026-11-01: every report produced by the BOT uses English
  ASCII numerals.** This includes the delivered message text/captions for money,
  counts, IDs and dates, not merely the assistant's progress reports. The owner
  explicitly reaffirmed this preference; do not ask it again. The shared bot
  client applies the delivery policy to sends/edits and nested media captions.
  Existing templates/formatters may still produce Persian digits internally;
  the delivered report is normalized at the common API boundary. Keep money in
  shared integer-toman formatters and preserve exact opaque URLs, HTML attributes,
  callback identities and stored data. Keyboards retain their existing labels.
  **Tests:** `tests/unit/test_report_delivery_numbers.py` and real-router e2e
  tests use the same bot factory as production. This direct owner decision
  supersedes older Persian-digit requirements for delivered report bodies.
- **Punctuation:** «،» «؛» «؟» «:» take no space before and one after, «…» not
  three dots, no double or trailing space, ZWNJ in «می‌شه» and «سرویس‌ها», a blank
  line between a heading and its list. `«»` quote a button name.
- **Terms, one each:** سرویس (the thing; «خرید اشتراک» only as the button's
  name) · کیف پول · گیگ · تمدید (a service) / شارژ (the wallet) · رسید (never
  فیش) · تأیید · جدید (never تازه) · برنامه (never اپلیکیشن) · لینک سابسکریپشن ·
  میاد · اینجا. The retired spellings are pinned in the same test.
- **Out of the pass, by decision:** the connection guides (`account_howto_*`,
  placeholder until the owner adds pictures) and the outage and restore
  announcements (broadcast bodies).

## `admin_*` is exempt, `notify_*` is not

The prefix is the whole basis of the exemption, so a string the owner
*triggers* but the customer *reads* must be named `notify_*`.

- Why: four wallet and service notices sat under `admin_` names and drifted to
  «شما» unseen.
- Test: `tests/unit/test_brand_voice.py` parses `bot/handlers/admin.py` and
  fails if any `texts.admin_*` reaches a `send_message` aimed at a customer.

## A rule the bot states, it must keep

`texts.username_rules` lists what `sanitize_service_name` actually refuses:
Persian, and nothing else with no Latin in it.

- `ali_`, `tele@` and `_mahdi` are *accepted and cleaned*, so listing them as
  wrong (a competitor's prompt does) would be the bot lying about itself. It
  says it strips them and then shows the result via
  `purchase_service_name_cleaned`.
- Test: `tests/unit/test_customer_label.py` runs every example in that string
  through the sanitizer.

## Never hardcode a brand number in a string

- Prices and group counts come from `services/brand.py` (live published
  catalog). Support handle, support hours and channel come from env.
- If the number is missing, **drop the whole claim**. A bare «@» or «از تومان
  شروع می‌شه» is the unprovable claim `BRAND.md` rejects.

## The signature

BRAND.md §4's closing block (`─────` / `نئو نتورک · @bot`) exists so a
forwarded message keeps its source.

- `services/brand.py::with_signature` adds it once, idempotently.
- **Applied at compose time** in the broadcast and outage flows, not at
  delivery. The prompt promises «همان‌طور که می‌نویسی برای مشتری ارسال می‌شود»,
  so the stored body, the preview and the delivered message must be one string.
- **Not on the referral pitch.** That text ends with the referrer's
  `?start=<code>` link; a bare `@handle` under it hands the reader an
  unattributed way in and the referrer loses the credit the message exists to
  earn. `tests/unit/test_brand_signature.py` pins that exception.
- **Rule: the pitch's «📤 فرستادن برای دوستان» button shares the same
  `?start=<code>` link the message shows** (`build_invite_share_url`), and its
  text makes no claim the pitch does not.
  **Why:** the pitch was the one customer message with no button; a share
  link built from the bare bot handle would lose the referrer's credit.
  **Test:** `tests/unit/test_invite_pitch.py`, e2e row ۲.۴.
- **The brand names itself on `welcome_new` and `main_menu` and nowhere else**,
  the two screens a customer anchors on. A name is not a claim, so it is exempt
  from «no unprovable numbers», but it earns nothing by repetition.

## A name or a typed string is escaped before it goes into HTML

- **Rule: a Telegram first name, a typed reason or query, or any other string a
  person supplied goes through `html.escape` where it is interpolated into
  copy** (the bot's parse mode is HTML, and a bare `<` makes Telegram refuse
  the whole message). Reports go through `report_events._esc`. Escape at the
  `format` call, never when storing: the DB keeps what was typed.
- **Why:** «Ali <3» broke `/start` and the receipt caption the admins get (so a
  paid order sat unseen), and a reject reason like «<5 دقیقه» never reached the
  customer although the order was rejected.
- **Test:** `tests/e2e/test_smoke_s16_html_in_names.py`; the strict fake
  Telegram refuses a bare `<`, so a new unescaped site fails any scenario that
  reaches it with a hostile name.
- **Not covered:** the admin's own DM and broadcast bodies are sent as typed,
  so a bare `<` in one fails the send. That is the owner's text, and escaping
  it would stop `<b>` working there: an open owner decision.
