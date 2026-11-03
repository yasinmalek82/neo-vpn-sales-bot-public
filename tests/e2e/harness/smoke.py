"""``@smoke("۴.۷")``: name the checklist row a scenario proves.

Use the id exactly as ``docs/LIVE_SMOKE_FA.md`` writes it (Persian digits,
suffix letters included: ``۴.۳الف``). One scenario may carry several ids; one row
may have several scenarios. ``scripts/smoke_coverage.py`` reads these by AST,
so keep the ids as plain string literals.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

import pytest

F = TypeVar("F", bound=Callable[..., Any])


def smoke(*rows: str) -> Callable[[F], F]:
    if not rows or not all(isinstance(r, str) and r.strip() for r in rows):
        raise TypeError('smoke() takes checklist row ids as strings, e.g. @smoke("۴.۷")')
    return pytest.mark.smoke(rows=tuple(rows))  # type: ignore[return-value]
