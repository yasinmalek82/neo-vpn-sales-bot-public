"""What a button's colour means, in one place.

Bot API 9.4 (February 2026) added a `style` field to keyboard buttons, with
three colours. Three colours across a hundred buttons is only useful if the
same colour always means the same thing — otherwise it is decoration, and
decoration that changes meaning per screen is worse than none.

The rule here follows `BRAND.md` §7, which says accent is for **one** thing per
screen: «اگه همه‌جا نعنایی باشه، هیچ‌جا تأکید نیست». So:

- ``PRIMARY`` (blue) — the one action the screen exists for. At most one per
  screen: buy, confirm an order, pay.
- ``SUCCESS`` (green) — approving or activating something that was pending.
  The admin's «تأیید», a plan going live.
- ``DANGER`` (red) — destructive and hard to reverse: reject, delete, ban,
  zero a wallet, cut a service off.
- **no style** — everything else. Navigation, back, browsing, settings. Most
  buttons are this, and that is what makes the coloured ones readable.

Styling is behind a switch (`BUTTON_COLORS_ENABLED`) because it is new: if a
client renders it badly on launch day it can be turned off in one line without
a deploy of new code.

## Layout

The same rule decides **placement**, because on a phone a full-width button is
an accent too. A screen of eight full-width rows emphasises nothing, and the
customer reads top to bottom looking for the one they came for.

1. **The primary action is alone on its first row.** Sharing a row makes it one
   of two equal options.
2. **Everything else pairs up**, two to a row, in the order they get used.
3. **Navigation goes last** — «بازگشت» at the bottom, on its own row, so a
   thumb reaching for it cannot hit anything expensive.
4. **A destructive button never shares a row with navigation.** Colour marks
   it; distance is what stops the misclick.

`tests/unit/test_button_style.py` enforces (1) — at most one `PRIMARY` per
keyboard — because that is the rule a new screen breaks by accident.
"""

from __future__ import annotations

from aiogram.enums import ButtonStyle

__all__ = ["DANGER", "PRIMARY", "SUCCESS", "styled"]

PRIMARY = ButtonStyle.PRIMARY
SUCCESS = ButtonStyle.SUCCESS
DANGER = ButtonStyle.DANGER

# Flipped by `bot.config.Settings.button_colors_enabled` at startup. A module
# flag rather than a settings lookup inside every keyboard builder: those are
# pure functions called dozens of times per screen and threading settings into
# all of them would be a large change for a colour.
_enabled = True


def set_enabled(value: bool) -> None:
    """Turn colours on or off for the whole bot."""
    global _enabled
    _enabled = bool(value)


def styled(style: str | None) -> str | None:
    """The style to pass to a button, or ``None`` when colours are off.

    Every keyboard calls this rather than passing a style directly, so the
    switch has exactly one place to work.
    """
    return style if _enabled else None
