"""ASCII service-name sanitization and panel username generation."""

from __future__ import annotations

import re
import secrets
import string

_ASCII_RE = re.compile(r"[^a-zA-Z0-9_]+")
_LEADING_TRAILING = re.compile(r"^_+|_+$")
_SUFFIX_ALPHABET = string.ascii_lowercase + string.digits


def sanitize_service_name(raw: str, *, max_len: int = 24) -> str:
    """Normalize customer ASCII name for display / username prefix.

    Raises ValueError with code ``invalid_service_name`` when empty after sanitize.
    """
    cleaned = _ASCII_RE.sub("_", (raw or "").strip())
    cleaned = _LEADING_TRAILING.sub("", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned)
    if not cleaned:
        raise ValueError("invalid_service_name")
    if not cleaned[0].isalpha():
        cleaned = f"s_{cleaned}"
    return cleaned[:max_len].rstrip("_").lower()


def build_panel_username(service_name: str, *, suffix_len: int = 4) -> str:
    """``{sanitized}_{random}`` — not ``neo_{order_id}``."""
    base = sanitize_service_name(service_name, max_len=20)
    suffix = "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(suffix_len))
    # PasarGuard usernames commonly capped ~32–64; keep under 32 for safety.
    return f"{base}_{suffix}"[:32]


def build_anonymous_panel_username(*, prefix: str = "neo", suffix_len: int = 8) -> str:
    """A panel username with nothing of the buyer in it.

    What a reseller gets. Their customer's name is Persian and belongs in the
    shop's own label, not in a panel identifier — and the old fallback for an
    unusable name was ``neo_{order_id}``, which is guessable and tells anyone
    reading the panel how many orders the bot has taken.

    Eight characters of ``[a-z0-9]`` is ~41 bits. Collisions are handled by the
    caller storing the name on the order before the panel is called, so a retry
    reuses it rather than minting a second one.
    """
    suffix = "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(suffix_len))
    return f"{prefix}_{suffix}"[:32]
