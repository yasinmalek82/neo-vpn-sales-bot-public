"""PasarGuard Panel API client package."""

from api_client.client import PasarGuardClient
from api_client.exceptions import (
    PasarGuardAPIError,
    PasarGuardAuthError,
    PasarGuardError,
    PasarGuardNotFoundError,
)
from api_client.factory import create_pasarguard_client, get_pasarguard_client
from api_client.models import (
    AdminDetails,
    DataLimitResetStrategy,
    GroupResponse,
    GroupSimple,
    GroupsResponse,
    GroupsSimpleResponse,
    SubscriptionUserResponse,
    Token,
    UserCreate,
    UserModify,
    UserResponse,
    UserStatus,
    UserUsageStatsList,
)

__all__ = [
    "PasarGuardClient",
    "create_pasarguard_client",
    "get_pasarguard_client",
    "PasarGuardAPIError",
    "PasarGuardAuthError",
    "PasarGuardError",
    "PasarGuardNotFoundError",
    "AdminDetails",
    "DataLimitResetStrategy",
    "SubscriptionUserResponse",
    "Token",
    "UserCreate",
    "UserModify",
    "UserResponse",
    "UserStatus",
    "UserUsageStatsList",
    "GroupResponse",
    "GroupSimple",
    "GroupsResponse",
    "GroupsSimpleResponse",
]
