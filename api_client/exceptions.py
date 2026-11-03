"""PasarGuard API client errors."""

from __future__ import annotations


class PasarGuardError(Exception):
    """Base error for PasarGuard client failures."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class PasarGuardAuthError(PasarGuardError):
    """Authentication or authorization failed."""


class PasarGuardNotFoundError(PasarGuardError):
    """Requested resource was not found."""


class PasarGuardAPIError(PasarGuardError):
    """Non-success HTTP response from the panel API."""
