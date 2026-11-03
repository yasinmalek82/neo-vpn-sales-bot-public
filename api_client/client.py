"""Async PasarGuard Panel HTTP client (OpenAPI-backed).

Verified against docs/vendor/pasarguard-openapi.json from PasarGuard/panel.
No invented endpoints.
Auth is API key only: header X-Api-Key — verified from panel source
app/routers/authentication.py (not listed in OpenAPI securitySchemes).
Password / POST /api/admin/token auth has been removed from this bot.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urljoin

import httpx

from api_client.exceptions import (
    PasarGuardAPIError,
    PasarGuardAuthError,
    PasarGuardError,
    PasarGuardNotFoundError,
)
from api_client.models import (
    AdminDetails,
    GroupsResponse,
    GroupsSimpleResponse,
    PanelNode,
    PanelNodeSimple,
    PanelNodesResponse,
    PanelNodesSimpleResponse,
    SubscriptionUserResponse,
    SystemStats,
    UserCreate,
    UserHWIDListResponse,
    UserModify,
    UserResponse,
    UserStatusToggle,
    UserSubscriptionUpdateList,
    UserUsageStatsList,
)

logger = logging.getLogger(__name__)


class PasarGuardClient:
    """Thin async wrapper around the PasarGuard Panel REST API (API key auth only)."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float = 30.0,
        sub_path: str = "sub",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        key = (api_key or "").strip()
        if not key:
            raise ValueError("PasarGuard api_key is required (non-empty)")
        # Preferred panel format: pg_key_ + UUID (panel source). Soft-validate.
        if not key.startswith("pg_key_"):
            logger.warning("PasarGuard api_key does not start with 'pg_key_'; continuing anyway")
        self._base_url = base_url.rstrip("/") + "/"
        self._api_key = key
        self._timeout = timeout
        self._sub_path = sub_path.strip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
        )

    @property
    def auth_mode(self) -> str:
        return "api_key"

    @property
    def uses_api_key(self) -> bool:
        return True

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> PasarGuardClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    def _auth_headers(self) -> dict[str, str]:
        return {"X-Api-Key": self._api_key}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        response = await self._client.request(
            method,
            path,
            json=json_body,
            params=params,
            headers=self._auth_headers(),
        )
        if response.status_code in {401, 403}:
            raise PasarGuardAuthError(
                "PasarGuard unauthorized (api_key)",
                status_code=response.status_code,
            )
        return response

    @staticmethod
    def _sanitize_response_detail(response: httpx.Response) -> str | None:
        """Extract a short, non-secret detail string from an error JSON body."""
        try:
            data = response.json()
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        detail = data.get("detail")
        if detail is None:
            return None
        if isinstance(detail, list):
            parts: list[str] = []
            for item in detail[:5]:
                if isinstance(item, dict) and "msg" in item:
                    parts.append(str(item["msg"]))
                else:
                    parts.append(str(item))
            text = "; ".join(parts)
        else:
            text = str(detail)
        lowered = text.lower()
        if any(
            s in lowered
            for s in ("password", "secret", "token", "api_key", "api-key", "pg_key_", "bearer")
        ):
            return None
        if len(text) > 200:
            return text[:200] + "…"
        return text or None

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        status = response.status_code
        # Avoid logging response bodies that may contain sensitive fields;
        # optionally surface sanitized ``detail`` (e.g. "Group not found").
        detail = PasarGuardClient._sanitize_response_detail(response)
        if status in {401, 403}:
            raise PasarGuardAuthError("PasarGuard unauthorized", status_code=status)
        if status == 404:
            msg = "PasarGuard resource not found"
            if detail:
                msg = f"{msg}: {detail}"
            raise PasarGuardNotFoundError(msg, status_code=status)
        msg = f"PasarGuard API error (HTTP {status})"
        if detail:
            msg = f"{msg}: {detail}"
        raise PasarGuardAPIError(msg, status_code=status)

    async def get_current_admin(self) -> AdminDetails:
        """GET /api/admin — current admin (connectivity probe for API key auth)."""
        response = await self._request("GET", "/api/admin")
        self._raise_for_status(response)
        return AdminDetails.model_validate(response.json())

    async def create_user(self, payload: UserCreate) -> UserResponse:
        """POST /api/user"""
        response = await self._request(
            "POST",
            "/api/user",
            json_body=payload.model_dump(mode="json", exclude_none=True),
        )
        self._raise_for_status(response)
        return UserResponse.model_validate(response.json())

    async def get_user(self, username: str) -> UserResponse:
        """GET /api/user/{username}"""
        response = await self._request("GET", f"/api/user/{username}")
        self._raise_for_status(response)
        return UserResponse.model_validate(response.json())

    async def get_user_by_id(self, user_id: int) -> UserResponse:
        """GET /api/user/by-id/{user_id}"""
        response = await self._request("GET", f"/api/user/by-id/{user_id}")
        self._raise_for_status(response)
        return UserResponse.model_validate(response.json())

    async def modify_user(self, username: str, payload: UserModify) -> UserResponse:
        """PUT /api/user/{username} — also used for expire/data_limit updates."""
        response = await self._request(
            "PUT",
            f"/api/user/{username}",
            json_body=payload.model_dump(mode="json", exclude_none=True),
        )
        self._raise_for_status(response)
        return UserResponse.model_validate(response.json())

    async def delete_user(self, username: str) -> None:
        """DELETE /api/user/{username}"""
        response = await self._request("DELETE", f"/api/user/{username}")
        self._raise_for_status(response)

    async def disable_user(self, username: str, *, disabled: bool = True) -> UserResponse:
        """PUT /api/user/{username}/disabled"""
        body = UserStatusToggle(disabled=disabled)
        response = await self._request(
            "PUT",
            f"/api/user/{username}/disabled",
            json_body=body.model_dump(mode="json"),
        )
        self._raise_for_status(response)
        return UserResponse.model_validate(response.json())

    async def revoke_user_subscription(self, username: str) -> UserResponse:
        """POST /api/user/{username}/revoke_sub — regenerate subscription link/proxies."""
        response = await self._request("POST", f"/api/user/{username}/revoke_sub")
        self._raise_for_status(response)
        return UserResponse.model_validate(response.json())

    async def reset_user_data_usage(self, username: str) -> UserResponse:
        """POST /api/user/{username}/reset — used traffic back to zero."""
        response = await self._request("POST", f"/api/user/{username}/reset")
        self._raise_for_status(response)
        return UserResponse.model_validate(response.json())

    async def get_user_hwids(self, panel_user_id: int) -> UserHWIDListResponse:
        """GET /api/user/{user_id}/hwids — the path takes the panel user id, not the name."""
        response = await self._request("GET", f"/api/user/{int(panel_user_id)}/hwids")
        self._raise_for_status(response)
        return UserHWIDListResponse.model_validate(response.json())

    async def reset_user_hwids(self, panel_user_id: int) -> None:
        """POST /api/user/{user_id}/hwids/reset — forget every registered device."""
        response = await self._request("POST", f"/api/user/{int(panel_user_id)}/hwids/reset")
        self._raise_for_status(response)

    async def list_sub_updates(
        self, username: str, *, limit: int = 3, offset: int = 0
    ) -> UserSubscriptionUpdateList:
        """GET /api/user/{username}/sub_update — newest first: which app fetched the link."""
        response = await self._request(
            "GET",
            f"/api/user/{username}/sub_update",
            params={"offset": offset, "limit": limit},
        )
        self._raise_for_status(response)
        return UserSubscriptionUpdateList.model_validate(response.json())

    async def get_user_usage(self, username: str) -> UserUsageStatsList:
        """GET /api/user/{username}/usage"""
        response = await self._request("GET", f"/api/user/{username}/usage")
        self._raise_for_status(response)
        return UserUsageStatsList.model_validate(response.json())

    async def extend_subscription(
        self,
        username: str,
        *,
        expire: Any,
        data_limit: int | None = None,
    ) -> UserResponse:
        """Extend via PUT /api/user/{username} (UserModify), not a separate endpoint."""
        return await self.modify_user(
            username,
            UserModify(expire=expire, data_limit=data_limit),
        )

    async def increase_data_limit(self, username: str, data_limit: int) -> UserResponse:
        """Increase data limit via PUT /api/user/{username}."""
        if data_limit < 0:
            raise PasarGuardError("data_limit must be >= 0")
        return await self.modify_user(username, UserModify(data_limit=data_limit))

    async def list_groups(
        self,
        *,
        offset: int | None = None,
        limit: int | None = None,
        ids: list[int] | None = None,
    ) -> GroupsResponse:
        """GET /api/groups — full group list (OpenAPI GroupsResponse)."""
        params: dict[str, Any] = {}
        if offset is not None:
            params["offset"] = offset
        if limit is not None:
            params["limit"] = limit
        if ids is not None:
            params["ids"] = ids
        response = await self._request(
            "GET",
            "/api/groups",
            params=params or None,
        )
        self._raise_for_status(response)
        return GroupsResponse.model_validate(response.json())

    async def list_groups_simple(
        self,
        *,
        offset: int | None = None,
        limit: int | None = None,
        ids: list[int] | None = None,
        search: str | None = None,
        sort: str | None = None,
        all_groups: bool = False,
    ) -> GroupsSimpleResponse:
        """GET /api/groups/simple — lightweight id/name list for pickers."""
        params: dict[str, Any] = {}
        if offset is not None:
            params["offset"] = offset
        if limit is not None:
            params["limit"] = limit
        if ids is not None:
            params["ids"] = ids
        if search is not None:
            params["search"] = search
        if sort is not None:
            params["sort"] = sort
        if all_groups:
            params["all"] = True
        response = await self._request(
            "GET",
            "/api/groups/simple",
            params=params or None,
        )
        self._raise_for_status(response)
        return GroupsSimpleResponse.model_validate(response.json())

    async def get_system_stats(self) -> SystemStats:
        """GET /api/system — version, uptime, CPU/RAM/disk, users by status, bandwidth."""
        response = await self._request("GET", "/api/system")
        self._raise_for_status(response)
        return SystemStats.model_validate(response.json())

    async def list_nodes(self, *, limit: int = 100) -> list[PanelNode]:
        """GET /api/nodes — nodes with status and message (no api_key / server_ca kept)."""
        response = await self._request("GET", "/api/nodes", params={"limit": limit})
        self._raise_for_status(response)
        return PanelNodesResponse.model_validate(response.json()).nodes

    async def list_nodes_simple(self) -> list[PanelNodeSimple]:
        """GET /api/nodes/simple?all=true — id, name and status only (the watcher's read)."""
        response = await self._request("GET", "/api/nodes/simple", params={"all": True})
        self._raise_for_status(response)
        return PanelNodesSimpleResponse.model_validate(response.json()).nodes

    async def reconnect_node(self, node_id: int) -> None:
        """POST /api/node/{node_id}/reconnect — ask the panel to reconnect one node."""
        response = await self._request("POST", f"/api/node/{int(node_id)}/reconnect")
        self._raise_for_status(response)

    async def get_subscription_info(self, subscription_token: str) -> SubscriptionUserResponse:
        """GET /{sub_path}/{token}/info (public subscription path)."""
        path = f"/{self._sub_path}/{subscription_token}/info"
        # Subscription info typically does not use admin auth.
        response = await self._client.get(path)
        self._raise_for_status(response)
        return SubscriptionUserResponse.model_validate(response.json())

    def subscription_url_from_response(self, user: UserResponse) -> str | None:
        """Return an absolute subscription URL from panel UserResponse.

        Prefers ``user.subscription_url`` when non-blank. Absolute http(s) URLs
        are returned as-is; relative paths are joined with ``self._base_url``.
        Does not invent a token from the username.
        """
        return build_subscription_url(self._base_url, user.subscription_url)

    def absolute_url(self, path: str) -> str:
        return urljoin(self._base_url, path.lstrip("/"))


def build_subscription_url(
    base_url: str,
    panel_subscription_url: str | None,
) -> str | None:
    """Normalize panel subscription_url to an absolute URL, or None if missing.

    ``base_url`` should be the panel origin (trailing slash optional). Relative
    panel paths (``/sub/TOKEN`` or ``sub/TOKEN``) are joined via urljoin.
    """
    if panel_subscription_url is None:
        return None
    stripped = panel_subscription_url.strip()
    if not stripped:
        return None
    if stripped.startswith(("http://", "https://")):
        return stripped
    # Ensure base ends with / so urljoin treats the next segment correctly
    # when the panel path has no leading slash.
    normalized_base = base_url if base_url.endswith("/") else base_url.rstrip("/") + "/"
    return urljoin(normalized_base, stripped)
