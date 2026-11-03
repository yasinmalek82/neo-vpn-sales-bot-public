"""Mocked unit tests for PasarGuardClient (API key only, no live panel)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from api_client import PasarGuardClient, UserCreate, UserModify, UserStatus
from api_client.client import build_subscription_url
from api_client.exceptions import PasarGuardAuthError, PasarGuardNotFoundError
from api_client.models import UserResponse


def _user_payload(**overrides):
    data = {
        "id": 42,
        "username": "neo_user_1",
        "status": "active",
        "used_traffic": 0,
        "created_at": datetime.now(UTC).isoformat(),
        "subscription_url": "https://panel.example.com/sub/abc123",
        "data_limit": 10 * 1024**3,
        "expire": int(datetime.now(UTC).timestamp()) + 86400,
    }
    data.update(overrides)
    return data


@pytest.mark.asyncio
async def test_create_user_sends_x_api_key() -> None:
    seen = {"token_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/admin/token"):
            seen["token_calls"] += 1
            return httpx.Response(
                200, json={"access_token": "should_not_use", "token_type": "bearer"}
            )
        if request.method == "POST" and request.url.path.endswith("/api/user"):
            assert request.headers.get("X-Api-Key") == "pg_key_11111111-2222-4333-8444-555555555555"
            assert not (request.headers.get("Authorization") or "").startswith("Bearer")
            return httpx.Response(201, json=_user_payload())
        return httpx.Response(500, json={"detail": "unexpected"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://panel.example.com") as http:
        client = PasarGuardClient(
            base_url="https://panel.example.com",
            api_key="pg_key_11111111-2222-4333-8444-555555555555",
            client=http,
        )
        assert client.uses_api_key is True
        assert client.auth_mode == "api_key"
        user = await client.create_user(
            UserCreate(
                username="neo_user_1",
                status=UserStatus.ACTIVE,
                data_limit=10 * 1024**3,
                group_ids=[1, 2],
            )
        )
        assert user.username == "neo_user_1"
        assert user.subscription_url is not None
        assert seen["token_calls"] == 0


@pytest.mark.asyncio
async def test_get_user_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/api/user/" in request.url.path:
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://panel.example.com") as http:
        client = PasarGuardClient(
            base_url="https://panel.example.com",
            api_key="pg_key_11111111-2222-4333-8444-555555555555",
            client=http,
        )
        with pytest.raises(PasarGuardNotFoundError):
            await client.get_user("missing")


@pytest.mark.asyncio
async def test_api_key_401_raises_without_relogin() -> None:
    calls = {"admin": 0, "token": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/admin/token"):
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": "tok", "token_type": "bearer"})
        if request.url.path.endswith("/api/admin"):
            calls["admin"] += 1
            return httpx.Response(401, json={"detail": "bad key"})
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://panel.example.com") as http:
        client = PasarGuardClient(
            base_url="https://panel.example.com",
            api_key="pg_key_deadbeef-dead-4eef-8eef-deadbeefdead",
            client=http,
        )
        with pytest.raises(PasarGuardAuthError):
            await client.get_current_admin()
        assert calls["token"] == 0
        assert calls["admin"] == 1


@pytest.mark.asyncio
async def test_modify_user_with_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-Api-Key") == "pg_key_modddddd-dddd-4ddd-8ddd-dddddddddddd"
        if request.method == "PUT" and request.url.path.endswith("/api/user/neo_user_1"):
            return httpx.Response(200, json=_user_payload(data_limit=20 * 1024**3))
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://panel.example.com") as http:
        client = PasarGuardClient(
            base_url="https://panel.example.com",
            api_key="pg_key_modddddd-dddd-4ddd-8ddd-dddddddddddd",
            client=http,
        )
        user = await client.modify_user("neo_user_1", UserModify(data_limit=20 * 1024**3))
        assert user.data_limit == 20 * 1024**3


@pytest.mark.asyncio
async def test_disable_and_subscription_info() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/disabled"):
            assert request.headers.get("X-Api-Key")
            return httpx.Response(200, json=_user_payload(status="disabled"))
        if request.url.path.endswith("/sub/abc123/info"):
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "username": "neo_user_1",
                    "status": "active",
                    "used_traffic": 123,
                },
            )
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://panel.example.com") as http:
        client = PasarGuardClient(
            base_url="https://panel.example.com",
            api_key="pg_key_disablll-llll-4lll-8lll-llllllllllll",
            sub_path="sub",
            client=http,
        )
        disabled = await client.disable_user("neo_user_1", disabled=True)
        assert disabled.status == UserStatus.DISABLED
        info = await client.get_subscription_info("abc123")
        assert info.username == "neo_user_1"


@pytest.mark.asyncio
async def test_get_current_admin_with_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-Api-Key") == "pg_key_aaaaaaa1-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        if request.url.path.endswith("/api/admin"):
            return httpx.Response(200, json={"id": 1, "username": "sudo", "status": "active"})
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://panel.example.com") as http:
        client = PasarGuardClient(
            base_url="https://panel.example.com",
            api_key="pg_key_aaaaaaa1-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            client=http,
        )
        admin = await client.get_current_admin()
        assert admin.username == "sudo"
        assert admin.id == 1


def test_empty_api_key_raises() -> None:
    with pytest.raises(ValueError, match="api_key is required"):
        PasarGuardClient(base_url="https://panel.example.com", api_key="")
    with pytest.raises(ValueError, match="api_key is required"):
        PasarGuardClient(base_url="https://panel.example.com", api_key="   ")


def _client(base_url: str = "https://panel.example.com") -> PasarGuardClient:
    return PasarGuardClient(
        base_url=base_url,
        api_key="pg_key_11111111-2222-4333-8444-555555555555",
    )


def _user_resp(subscription_url: str | None) -> UserResponse:
    return UserResponse(
        id=1,
        username="neo_1",
        status=UserStatus.ACTIVE,
        used_traffic=0,
        created_at=datetime.now(UTC),
        subscription_url=subscription_url,
    )


def test_subscription_url_absolute_unchanged() -> None:
    client = _client("https://panel.example.com")
    url = client.subscription_url_from_response(_user_resp("https://cdn.example.com/sub/TOKEN"))
    assert url == "https://cdn.example.com/sub/TOKEN"


def test_subscription_url_relative_leading_slash() -> None:
    client = _client("https://panel.example.com")
    url = client.subscription_url_from_response(_user_resp("/sub/TOKEN"))
    assert url == "https://panel.example.com/sub/TOKEN"


def test_subscription_url_relative_no_leading_slash() -> None:
    client = _client("https://panel.example.com")
    url = client.subscription_url_from_response(_user_resp("sub/TOKEN"))
    assert url == "https://panel.example.com/sub/TOKEN"


def test_subscription_url_base_with_trailing_slash() -> None:
    client = _client("https://panel.example.com/")
    url = client.subscription_url_from_response(_user_resp("/sub/TOKEN"))
    assert url == "https://panel.example.com/sub/TOKEN"


def test_subscription_url_empty_or_none() -> None:
    client = _client()
    assert client.subscription_url_from_response(_user_resp(None)) is None
    assert client.subscription_url_from_response(_user_resp("")) is None
    assert client.subscription_url_from_response(_user_resp("   ")) is None
    assert build_subscription_url("https://panel.example.com", None) is None
    assert build_subscription_url("https://panel.example.com", "") is None


def test_subscription_url_sub_path_leading_slash_does_not_break() -> None:
    """Relative panel path already includes /sub/...; joining stays correct."""
    client = PasarGuardClient(
        base_url="https://panel.example.com",
        api_key="pg_key_11111111-2222-4333-8444-555555555555",
        sub_path="/sub",
    )
    url = client.subscription_url_from_response(_user_resp("/sub/TOKEN"))
    assert url == "https://panel.example.com/sub/TOKEN"


@pytest.mark.asyncio
async def test_list_groups() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-Api-Key") == "pg_key_gggggggg-gggg-4ggg-8ggg-gggggggggggg"
        assert request.url.path.endswith("/api/groups")
        return httpx.Response(
            200,
            json={
                "groups": [
                    {
                        "id": 7,
                        "name": "volume-grp",
                        "inbound_tags": ["tag-a"],
                        "is_disabled": False,
                        "total_users": 3,
                    },
                    {
                        "id": 1,
                        "name": "unlimited-grp",
                        "inbound_tags": [],
                        "is_disabled": True,
                        "total_users": 0,
                    },
                ],
                "total": 2,
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://panel.example.com") as http:
        client = PasarGuardClient(
            base_url="https://panel.example.com",
            api_key="pg_key_gggggggg-gggg-4ggg-8ggg-gggggggggggg",
            client=http,
        )
        resp = await client.list_groups(limit=50)
        assert resp.total == 2
        assert resp.groups[0].id == 7
        assert resp.groups[0].name == "volume-grp"
        assert resp.groups[1].is_disabled is True


@pytest.mark.asyncio
async def test_list_groups_simple() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/api/groups/simple")
        return httpx.Response(
            200,
            json={"groups": [{"id": 5, "name": "simple"}], "total": 1},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://panel.example.com") as http:
        client = PasarGuardClient(
            base_url="https://panel.example.com",
            api_key="pg_key_ssssssss-ssss-4sss-8sss-ssssssssssss",
            client=http,
        )
        resp = await client.list_groups_simple(all_groups=True)
        assert resp.total == 1
        assert resp.groups[0].id == 5


@pytest.mark.asyncio
async def test_not_found_includes_sanitized_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Group not found"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://panel.example.com") as http:
        client = PasarGuardClient(
            base_url="https://panel.example.com",
            api_key="pg_key_nnnnnnnn-nnnn-4nnn-8nnn-nnnnnnnnnnnn",
            client=http,
        )
        with pytest.raises(PasarGuardNotFoundError) as excinfo:
            await client.get_user("missing")
        assert "Group not found" in str(excinfo.value)


@pytest.mark.asyncio
async def test_not_found_strips_secret_looking_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "bad api_key pg_key_secret"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://panel.example.com") as http:
        client = PasarGuardClient(
            base_url="https://panel.example.com",
            api_key="pg_key_zzzzzzzz-zzzz-4zzz-8zzz-zzzzzzzzzzzz",
            client=http,
        )
        with pytest.raises(PasarGuardNotFoundError) as excinfo:
            await client.get_user("missing")
        assert "pg_key_" not in str(excinfo.value)
        assert "api_key" not in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_revoke_user_subscription() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/api/user/neo_user_1/revoke_sub")
        assert request.headers.get("X-Api-Key")
        return httpx.Response(
            200,
            json=_user_payload(subscription_url="https://panel.example.com/sub/newtoken"),
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://panel.example.com") as http:
        client = PasarGuardClient(
            base_url="https://panel.example.com",
            api_key="pg_key_11111111-2222-4333-8444-555555555555",
            client=http,
        )
        user = await client.revoke_user_subscription("neo_user_1")
        assert user.username == "neo_user_1"
        assert user.subscription_url and "newtoken" in user.subscription_url
