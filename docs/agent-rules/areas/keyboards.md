---
paths:
  - "bot/keyboards/**"
  - "tests/unit/test_button_style.py"
  - "tests/unit/test_admin_navigation.py"
---

# Buttons: colour and layout

`bot/keyboards/style.py` is the only place either is decided, and it carries
the reasoning.

Bot API 9.4 (Feb 2026) added `style` to keyboard buttons; aiogram 3.31 exposes
it. `icon_custom_emoji_id` exists too but needs the bot owner to hold Telegram
Premium or a Fragment username. This deployment has neither, so icons are out
and colours are in.

- **Three meanings, never mixed.** `PRIMARY` = the one action the screen
  exists for, `SUCCESS` = approving something pending, `DANGER` = destructive.
  Every other button has **no** style, and that is what makes the coloured
  ones readable. BRAND.md §7: «اگه همه‌جا نعنایی باشه، هیچ‌جا تأکید نیست».
- **Layout follows the same logic**, because a full-width button is an accent
  on a phone: the primary is alone on the first row, the rest pair up two to a
  row, navigation is last and alone, and a destructive button never shares a
  row with it.
- **The bottom (reply) menu carries the accent too**: «🛒 خرید اشتراک» is
  `KeyboardButton(style=styled(PRIMARY))`, the only coloured button there.
  E2e rows ۱۱ب.۱ and ۱۱ب.۲ pin it and its off switch.
- **Always go through `styled()`**, never pass a style constant directly.
  `BUTTON_COLORS_ENABLED=false` has to be a complete off switch on launch day,
  and a direct constant silently survives it.
- **Every admin message carries a keyboard.**
  `tests/unit/test_admin_navigation.py` fails the build otherwise.
- Test: `tests/unit/test_button_style.py` finds the keyboard builders by
  reflection, so a screen added later is covered without anyone remembering.
  It enforces one `PRIMARY` per keyboard and a clean off switch.

## Bottom menu routing

- **Rule: inside an FSM state only the exact label of a bottom button
  navigates; the keyword guess runs only with no state**
  (`reply_menu.py::navigation_root`).
  **Why:** the reply router runs before every FSM handler, and «سرویست فعال
  شد», typed by the owner as a message to a customer, opened «سرویس‌های من».
  **Test:** `tests/unit/test_reply_menu.py`, e2e rows ۷.۱۲ and ۲.۶.

## Back buttons

- **Rule: `back_label` is the parent's bare name**; `nav.py::back_button`
  adds the «⬅️».
  **Why:** two callers passed «⬅️ پنل» and «⬅️ مشتری‌ها», and the buttons
  read «⬅️ ⬅️ پنل».
  **Test:** `tests/unit/test_admin_navigation.py::test_no_keyboard_doubles_the_back_arrow`.
