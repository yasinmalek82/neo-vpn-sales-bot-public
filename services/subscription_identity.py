"""Verify a reviewed link without mistaking freshly signed tokens for revocation."""

import re
from urllib.parse import urlsplit

from api_client import PasarGuardClient, UserResponse
from api_client.exceptions import PasarGuardError


async def reviewed_subscription_matches(
    client: PasarGuardClient, live: UserResponse, reviewed_url: str | None
) -> bool:
    current = client.subscription_url_from_response(live)
    if not reviewed_url or not current:
        return False
    if reviewed_url == current:
        return True
    try:
        old, new = urlsplit(reviewed_url), urlsplit(current)
        old_prefix, _, token = old.path.rstrip("/").rpartition("/")
        new_prefix, _, _ = new.path.rstrip("/").rpartition("/")
        if (
            old.scheme not in {"https", "http"}
            or not old.hostname
            or old.username is not None
            or old.password is not None
            or new.username is not None
            or new.password is not None
            or (old.scheme, old.netloc.lower(), old_prefix)
            != (new.scheme, new.netloc.lower(), new_prefix)
            or old.query
            or new.query
            or old.fragment
            or new.fragment
            or token in {".", ".."}
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,2048}", token)
        ):
            return False
    except ValueError:
        return False
    # Never fetch the supplied URL or trust an unsigned token payload. Ask the
    # configured panel's existing metadata endpoint to verify its signature and
    # binding. The reviewed link remains the one stored after confirmation.
    try:
        info = await client.get_subscription_info(token)
    except PasarGuardError as exc:
        if exc.status_code in {400, 401, 403, 404, 422}:
            return False
        raise
    return info.id == live.id and info.username == live.username
