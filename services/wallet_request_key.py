"""The request key of a web wallet adjustment.

The confirm page carries a token the server minted when it rendered the page.
The token is signed over the admin, the customer and the signed amount it was
minted for, so it cannot be reused for another customer, another amount or by
another admin. Its ``stamp`` is the part of the ledger's idempotency key
(``admin_adjust:{admin}:{customer}:{stamp}``, ``services/customer_lookup.py``)
that tells one request from the next: the same token sent twice is the same
ledger key, and the ledger writes it once.

Stateless on purpose: a table of minted keys would be a migration and a second
place that can disagree with the ledger.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

__all__ = ["mint_request_token", "stamp_of_request_token"]

_PREFIX = "web:"
_NONCE_BYTES = 8
_SIG_CHARS = 32


def _signature(secret: str, *, admin: int, user: int, amount: int, nonce: str) -> str:
    key = hashlib.sha256(b"wallet-request-key:" + secret.encode()).digest()
    message = f"{int(admin)}:{int(user)}:{int(amount)}:{nonce}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:_SIG_CHARS]


def mint_request_token(secret: str, *, admin: int, user: int, amount: int) -> str:
    """A fresh token for one confirm page: ``{nonce}.{signature}``."""
    nonce = secrets.token_hex(_NONCE_BYTES)
    return f"{nonce}.{_signature(secret, admin=admin, user=user, amount=amount, nonce=nonce)}"


def stamp_of_request_token(
    secret: str, token: str | None, *, admin: int, user: int, amount: int
) -> str | None:
    """The ledger ``stamp`` of a token minted for exactly this admin, customer and amount.

    ``None`` for a missing, malformed or foreign token, and for one minted for
    anything else.
    """
    nonce, dot, signature = (token or "").partition(".")
    if not dot or len(nonce) != _NONCE_BYTES * 2 or len(signature) != _SIG_CHARS:
        return None
    if not all(c in "0123456789abcdef" for c in nonce):
        return None
    expected = _signature(secret, admin=admin, user=user, amount=amount, nonce=nonce)
    if not hmac.compare_digest(expected, signature):
        return None
    return f"{_PREFIX}{nonce}"
