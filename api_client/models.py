"""Pydantic models for the subset of PasarGuard Panel OpenAPI we use.

Source of truth: docs/vendor/pasarguard-openapi.json
(exported from PasarGuard/panel via scripts/export_openapi.py).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class UserStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    LIMITED = "limited"
    EXPIRED = "expired"
    ON_HOLD = "on_hold"


class DataLimitResetStrategy(str, Enum):
    NO_RESET = "no_reset"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


class Token(BaseModel):
    model_config = ConfigDict(extra="ignore")

    access_token: str
    token_type: str = "bearer"


class AdminDetails(BaseModel):
    """GET /api/admin response (OpenAPI AdminDetails). Parsed loosely."""

    model_config = ConfigDict(extra="ignore")

    id: int | None = None
    username: str | None = None
    status: str | None = None
    is_sudo: bool | None = None
    telegram_id: int | None = None


class UserCreate(BaseModel):
    """POST /api/user body (OpenAPI UserCreate)."""

    model_config = ConfigDict(extra="ignore")

    username: str
    status: UserStatus | None = None
    expire: datetime | int | None = None
    data_limit: int | None = Field(default=None, ge=0)
    data_limit_reset_strategy: DataLimitResetStrategy | None = None
    note: str | None = Field(default=None, max_length=500)
    group_ids: list[int] | None = None
    hwid_limit: int | None = None
    on_hold_expire_duration: int | None = None
    on_hold_timeout: datetime | int | None = None
    auto_delete_in_days: int | None = None
    proxy_settings: dict[str, Any] | None = None
    next_plan: dict[str, Any] | None = None


class UserModify(BaseModel):
    """PUT /api/user/{username} body (OpenAPI UserModify)."""

    model_config = ConfigDict(extra="ignore")

    status: UserStatus | None = None
    expire: datetime | int | None = None
    data_limit: int | None = Field(default=None, ge=0)
    data_limit_reset_strategy: DataLimitResetStrategy | None = None
    note: str | None = Field(default=None, max_length=500)
    group_ids: list[int] | None = None
    hwid_limit: int | None = None
    on_hold_expire_duration: int | None = None
    on_hold_timeout: datetime | int | None = None
    auto_delete_in_days: int | None = None
    proxy_settings: dict[str, Any] | None = None
    next_plan: dict[str, Any] | None = None


class UserStatusToggle(BaseModel):
    """PUT /api/user/{username}/disabled body."""

    model_config = ConfigDict(extra="ignore")

    disabled: bool


class UserResponse(BaseModel):
    """Panel user payload (OpenAPI UserResponse)."""

    model_config = ConfigDict(extra="ignore")

    id: int
    username: str
    status: UserStatus
    used_traffic: int
    created_at: datetime
    expire: datetime | int | None = None
    data_limit: int | None = None
    data_limit_reset_strategy: DataLimitResetStrategy | None = None
    note: str | None = None
    group_ids: list[int] | None = None
    hwid_limit: int | None = None
    lifetime_used_traffic: int | None = None
    edit_at: datetime | None = None
    online_at: datetime | None = None
    subscription_url: str | None = None
    admin: Any | None = None
    proxy_settings: dict[str, Any] | None = None
    next_plan: dict[str, Any] | None = None
    on_hold_expire_duration: int | None = None
    on_hold_timeout: datetime | int | None = None
    auto_delete_in_days: int | None = None


class UserHWIDResponse(BaseModel):
    """One registered device (OpenAPI UserHWIDResponse)."""

    model_config = ConfigDict(extra="ignore")

    id: int | None = None
    hwid: str = ""
    device_os: str | None = None
    os_version: str | None = None
    device_model: str | None = None
    created_at: datetime | None = None
    last_used_at: datetime | None = None


class UserHWIDListResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hwids: list[UserHWIDResponse] = []
    count: int = 0


class UserSubscriptionUpdateSchema(BaseModel):
    """One fetch of the subscription link (which app, when)."""

    model_config = ConfigDict(extra="ignore")

    created_at: datetime | None = None
    user_agent: str = ""
    ip: str | None = None
    hwid: str | None = None


class UserSubscriptionUpdateList(BaseModel):
    model_config = ConfigDict(extra="ignore")

    updates: list[UserSubscriptionUpdateSchema] = []
    count: int = 0


class UserUsageStat(BaseModel):
    model_config = ConfigDict(extra="ignore")

    period_start: datetime
    total_traffic: int


class UserUsageStatsList(BaseModel):
    model_config = ConfigDict(extra="ignore")

    start: datetime
    end: datetime
    stats: dict[str, list[UserUsageStat]]
    period: str | None = None


class SubscriptionUserResponse(BaseModel):
    """GET /{sub_path}/{token}/info payload."""

    model_config = ConfigDict(extra="ignore")

    id: int | None = None
    username: str | None = None
    status: UserStatus | None = None
    used_traffic: int | None = None
    lifetime_used_traffic: int | None = None
    expire: datetime | int | None = None
    data_limit: int | None = None
    data_limit_reset_strategy: DataLimitResetStrategy | None = None
    group_ids: list[int] | None = None
    hwid_limit: int | None = None
    created_at: datetime | None = None
    edit_at: datetime | None = None
    online_at: datetime | None = None
    proxy_settings: dict[str, Any] | None = None
    next_plan: dict[str, Any] | None = None


class GroupResponse(BaseModel):
    """GET /api/groups item (OpenAPI GroupResponse)."""

    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    inbound_tags: list[str] | None = None
    is_disabled: bool = False
    total_users: int = 0


class GroupsResponse(BaseModel):
    """GET /api/groups response (OpenAPI GroupsResponse)."""

    model_config = ConfigDict(extra="ignore")

    groups: list[GroupResponse]
    total: int


class GroupSimple(BaseModel):
    """GET /api/groups/simple item (OpenAPI GroupSimple)."""

    model_config = ConfigDict(extra="ignore")

    id: int
    name: str


class GroupsSimpleResponse(BaseModel):
    """GET /api/groups/simple response (OpenAPI GroupsSimpleResponse)."""

    model_config = ConfigDict(extra="ignore")

    groups: list[GroupSimple]
    total: int


class SystemStats(BaseModel):
    """GET /api/system (OpenAPI SystemStats); the fields the status screen shows."""

    model_config = ConfigDict(extra="ignore")

    version: str = ""
    uptime_seconds: int = 0
    total_user: int = 0
    online_users: int = 0
    active_users: int = 0
    on_hold_users: int = 0
    disabled_users: int = 0
    expired_users: int = 0
    limited_users: int = 0
    incoming_bandwidth: int = 0
    outgoing_bandwidth: int = 0
    mem_total: int | None = None
    mem_used: int | None = None
    disk_total: int | None = None
    disk_used: int | None = None
    cpu_cores: int | None = None
    cpu_usage: float | None = None


class PanelNode(BaseModel):
    """GET /api/nodes item, reduced on purpose.

    The panel's NodeResponse also carries ``api_key`` and ``server_ca``. Neither is
    declared here, so ``extra="ignore"`` drops them at parse time and they can
    never reach a log line, a screen or a report.
    """

    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    status: str
    message: str | None = None
    xray_version: str | None = None
    core_version: str | None = None
    uplink: int = 0
    downlink: int = 0


class PanelNodeSimple(BaseModel):
    """GET /api/nodes/simple item (OpenAPI NodeSimple)."""

    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    status: str


class PanelNodesResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    nodes: list[PanelNode] = []
    total: int = 0


class PanelNodesSimpleResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    nodes: list[PanelNodeSimple] = []
    total: int = 0
