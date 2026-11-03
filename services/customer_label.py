"""The name a reseller files their own customer under.

A shop sells to people the bot never sees. «رضا، همونی که پنجشنبه اومد» is how
they find a service again, and `neo_a7f3` is not. So a reseller's service
carries a label they wrote, in the shape:

    رضا | 09123456789

Two rules make this module exist rather than an f-string at three call sites.

**The name is Latin, and it seeds the panel username.** `reza` becomes both the
label the shop reads and the `reza_a7f3` they search for in PasarGuard, so the
two halves of one sale are recognisably the same service. The phone is what has
to stay out of the panel name — which is the whole reason the two are asked
**separately**, in the owner's words «که تداخلی در اسم تصادفی که میخوایم در پنل
بسازیم ایجاد نشه». One box taking «رضا 09123456789» would drag eleven digits
into every username.

**The phone is stored in ASCII digits.** Searching «۶۷۸۹» must find a service
filed as «09123456789», and SQL `LIKE` does not know that ۶ and 6 are the same
digit. Normalizing at the one moment the number is written is the only place
that fix stays true; doing it in the query instead would leave every row
written before the fix unfindable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from services.service_names import sanitize_service_name

__all__ = [
    "MAX_LABEL_CHARS",
    "MAX_NAME_CHARS",
    "SEPARATOR",
    "CustomerLabel",
    "build_label",
    "normalize_phone",
    "sanitize_customer_name",
    "search_patterns",
    "split_label",
]

# `PasarGuardAccount.display_name` and `Order.customer_label` are both
# String(64). The name is capped well below it so a long name can never push a
# phone number off the end — a truncated phone is worse than no phone, because
# it looks like one.
MAX_LABEL_CHARS = 64
# `build_panel_username` re-sanitizes to 20 before adding its suffix, so a name
# longer than this is trimmed twice and never reaches the panel intact anyway.
MAX_NAME_CHARS = 24
SEPARATOR = " | "

_ASCII_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_NON_DIGIT = re.compile(r"\D+")


@dataclass(frozen=True)
class CustomerLabel:
    """A shop's note about one of their customers."""

    name: str
    phone: str | None = None

    def __str__(self) -> str:
        return build_label(self.name, self.phone)

    @property
    def has_phone(self) -> bool:
        return bool(self.phone)


def sanitize_customer_name(raw: str) -> str:
    """Latin, because this name becomes the panel username's prefix.

    Exactly the same normalization a customer's own service name gets — one
    rule, so `reza` in the shop's list and `reza_a7f3` in PasarGuard are
    visibly the same service. Raises ``ValueError`` on anything that leaves
    nothing behind, which includes a purely Persian name: the caller asks
    again rather than filing a service under a name the panel cannot hold.

    The separator can never survive this, since `|` is not in `[a-z0-9_]`.
    """
    return sanitize_service_name(raw, max_len=MAX_NAME_CHARS)


def normalize_phone(raw: str) -> str:
    """Persian or Arabic digits and any separators in, bare ASCII digits out.

    «۰۹۱۲ ۳۴۵ ۶۷۸۹», «0912-345-6789» and «+98 912 345 6789» are the same number
    written three ways; filed three ways, none of them is searchable by the
    last four digits. Raises when there is no digit at all, so the caller can
    ask again instead of storing an empty phone that renders as a dangling «|».
    """
    digits = _NON_DIGIT.sub("", (raw or "").translate(_ASCII_DIGITS))
    if not digits:
        raise ValueError("empty_phone")
    return digits[:MAX_LABEL_CHARS]


def build_label(name: str, phone: str | None) -> str:
    """«رضا | 09123456789», or «رضا» when no number was given.

    Truncates the *name* rather than the composed string: the number is the
    part that has to stay exact.
    """
    cleaned = (name or "").strip()
    if not phone:
        return cleaned[:MAX_LABEL_CHARS]
    room = MAX_LABEL_CHARS - len(SEPARATOR) - len(phone)
    if room <= 0:
        # A phone long enough to fill the column on its own. Nothing sane to
        # truncate, so the number wins — it is the searchable half.
        return phone[:MAX_LABEL_CHARS]
    return f"{cleaned[:room].strip()}{SEPARATOR}{phone}"


def split_label(label: str | None) -> CustomerLabel:
    """Read a stored label back into its parts.

    Tolerates labels written before this shape existed — a migrated service
    filed as «reza» simply has no phone, and the rename flow starts from that
    name instead of a blank field.
    """
    text = (label or "").strip()
    if not text:
        return CustomerLabel(name="")
    name, separator, phone = text.partition(SEPARATOR.strip())
    if not separator:
        return CustomerLabel(name=text)
    return CustomerLabel(name=name.strip(), phone=phone.strip() or None)


def search_patterns(query: str) -> list[str]:
    """`LIKE` patterns for one search box, lowercased.

    Two, when the query contains Persian or Arabic digits: the raw text, for a
    name like «رضا ۲» that was stored with those digits, and the ASCII form,
    for a phone that was normalized on the way in. One otherwise. Searching
    «۶۷۸۹» has to find `09123456789`, and searching «رضا ۲» has to keep working.
    """
    cleaned = (query or "").strip().lower()
    if not cleaned:
        return []
    ascii_form = cleaned.translate(_ASCII_DIGITS)
    forms = [cleaned] if ascii_form == cleaned else [cleaned, ascii_form]
    return [f"%{form}%" for form in forms]
