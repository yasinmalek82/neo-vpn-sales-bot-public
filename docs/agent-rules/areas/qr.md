---
paths:
  - "services/qr_brand.py"
  - "services/subscription_qr.py"
  - "bot/delivery.py"
  - "database/models/brand_asset.py"
  - "tests/unit/test_qr_brand.py"
---

# Service QR on the brand template

`services/qr_brand.py` puts every service QR (delivery card, «سرویس‌های من»,
the QR button) on `assets/brand/qr_template_default.png`, or on a background
the owner uploaded from «🎨 پس‌زمینهٔ QR» (stored in `brand_assets`, not on
disk, so it survives a rebuild and a restore).

- **The code goes in the template's measured white box (`BOX`)**, redrawn to
  cover the template's placeholder, with **nearest-neighbour** scaling: only
  pure black and white, or a phone camera struggles at an angle.
- **The bottom pill shows the bot's handle drawn live.** The test bot and the
  live bot differ, and a forwarded QR must still say where it came from.
- **With no handle the whole bottom line is removed.** The template's «[@…]»
  placeholder must never reach a customer. On an upload only the code is
  drawn.
- **`bot.delivery.service_qr_png` falls back to the plain code on any
  failure.** A broken upload must never cost a customer their link.
- **The render (~150 ms) runs in `asyncio.to_thread`**, or every other tap
  would wait.
- No QR decoder is installed. `tests/unit/test_qr_brand.py` proves the code by
  reading every module back from the pixels against the library's matrix.

## Automatic card recovery

Activation writes a delivery intent in the business transaction. Automatic
callers pass `order_id` to `deliver_provisioned_service_card`; the leased queue
worker renders the current card and verifies local ownership. Manual resend
omits `order_id` intentionally. Queue payload holds account/order references,
not subscription credentials. Release the read transaction before Telegram.
Regression: `tests/e2e/test_delivery_recovery.py` includes both transport failure
and interruption immediately after activation commit.
