"""A changing signed URL is accepted only after server-side identity validation."""

from datetime import UTC, datetime

import httpx
import pytest

from api_client import PasarGuardClient, UserResponse, UserStatus
from api_client.exceptions import PasarGuardAPIError


@pytest.mark.parametrize(
    "case,expected",
    [
        ("valid", True),
        ("wrong_id", False),
        ("wrong_name", False),
        ("revoked", False),
        ("host_changed", False),
        ("unsafe_token", False),
        ("same_url", True),
    ],
)
async def test_reviewed_subscription_identity(case, expected):
    from services.subscription_identity import reviewed_subscription_matches

    calls = []

    def respond(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url.host == "panel.invalid"
        assert request.url.path == "/sub/old-token/info"
        assert "X-Api-Key" not in request.headers
        if case == "revoked":
            return httpx.Response(404, json={})
        return httpx.Response(
            200,
            json={
                "id": 42 if case == "wrong_id" else 41,
                "username": "foreign" if case == "wrong_name" else "exact_name",
            },
        )

    async with httpx.AsyncClient(
        base_url="https://panel.invalid", transport=httpx.MockTransport(respond)
    ) as transport:
        client = PasarGuardClient(
            base_url="https://panel.invalid", api_key="pg_key_test", client=transport
        )
        old = "https://subs.invalid/sub/old-token"
        current = "https://subs.invalid/sub/fresh-token"
        if case == "host_changed":
            current = "https://other.invalid/sub/fresh-token"
        elif case == "unsafe_token":
            old = "https://subs.invalid/sub/old%2Ftoken"
        elif case == "same_url":
            current = old
        live = UserResponse(
            id=41,
            username="exact_name",
            status=UserStatus.ACTIVE,
            used_traffic=0,
            created_at=datetime.now(UTC),
            subscription_url=current,
        )
        assert await reviewed_subscription_matches(client, live, old) is expected
    assert len(calls) == int(case not in {"host_changed", "unsafe_token", "same_url"})


async def test_subscription_metadata_outage_is_not_called_a_valid_snapshot():
    from services.subscription_identity import reviewed_subscription_matches

    async with httpx.AsyncClient(
        base_url="https://panel.invalid",
        transport=httpx.MockTransport(lambda _request: httpx.Response(503, json={})),
    ) as transport:
        client = PasarGuardClient(
            base_url="https://panel.invalid", api_key="pg_key_test", client=transport
        )
        live = UserResponse(
            id=41,
            username="exact_name",
            status=UserStatus.ACTIVE,
            used_traffic=0,
            created_at=datetime.now(UTC),
            subscription_url="https://subs.invalid/sub/fresh-token",
        )
        with pytest.raises(PasarGuardAPIError):
            await reviewed_subscription_matches(client, live, "https://subs.invalid/sub/old-token")


@pytest.mark.parametrize("segment", [".", ".."])
async def test_dot_segments_cannot_change_the_metadata_endpoint(segment):
    from services.subscription_identity import reviewed_subscription_matches

    calls = []

    def unexpected(request):
        calls.append(str(request.url))
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(
        base_url="https://panel.invalid", transport=httpx.MockTransport(unexpected)
    ) as transport:
        client = PasarGuardClient(
            base_url="https://panel.invalid", api_key="pg_key_test", client=transport
        )
        live = UserResponse(
            id=41,
            username="exact_name",
            status=UserStatus.ACTIVE,
            used_traffic=0,
            created_at=datetime.now(UTC),
            subscription_url="https://subs.invalid/sub/fresh-token",
        )
        assert not await reviewed_subscription_matches(
            client, live, "https://subs.invalid/sub/" + segment
        )
    assert calls == []
