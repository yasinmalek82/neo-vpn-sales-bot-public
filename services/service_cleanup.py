"""When may an ended service be deleted? A pure decision, and nothing else.

``decide`` answers KEEP, WARN or DELETE from what the panel says NOW and from
facts the bot already has. It touches no database and no network, so every
branch is a row in ``tests/unit/test_service_cleanup_decide.py``. The job that
acts on it (``services/lifecycle_run.py``) re-reads the panel and decides again
right before each delete.

The owner's words: «a mistake here is expensive for me». A missed deletion is
harmless; a wrong one cannot be undone. So DELETE is the narrowest answer:

* ``delete_mode`` is ``rehearsal`` or ``on``; the panel user was read in THIS
  run; the owner has not exempted the service (``delete_exempt``);
* the panel status is ``expired`` (with an expiry in the past) or ``limited``
  (the bot has seen it limited for the whole period). Everything else is KEEP:
  ``active``, ``on_hold``, ``disabled``, an unknown status, an ``active`` user
  whose expiry is in the past (the panel has not flipped it yet);
* no order of the same user is pending, in review, approved or provisioning,
  and the service was not created or changed in the last day;
* more than N **whole days** have passed since it ended: ten days exactly (or
  ten days and 23 hours) is KEEP, eleven is DELETE. N = 0 turns the rule off;
* a paid service was WARNED at least ``delete_warn_days_before`` days ago for
  the current period (limit + expiry): a renewal re-arms the warning. A service
  past its deadline that was never warned is WARN, not DELETE. A trial gets no
  warning and follows ``delete_trial_after_days``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from database.models.lifecycle import DELETE_MODE_ON, DELETE_MODE_REHEARSAL
from services.accounts import normalize_expire
from services.lifecycle_settings import LifecycleConfig
from services.volume_warnings import period_of

KEEP = "keep"
WARN = "warn"
DELETE = "delete"

KIND_EXPIRED = "expired"
KIND_LIMITED = "limited"
KIND_TRIAL = "trial"

# Hard stop: more than this many deletions in one run, or more than a quarter of the
# live services when that is more than HARD_STOP_SMALL.
HARD_STOP_COUNT = 20
HARD_STOP_SMALL = 5

# A service created or changed within this long is never deleted.
RECENT_CHANGE = timedelta(days=1)


@dataclass(frozen=True)
class AccountFacts:
    """What the bot knows about one live service. Plain data."""

    account_id: int
    panel_username: str = ""
    # The panel id this account was created with; a different id answering to the same name
    # is another user (never delete by name alone).
    panel_user_id: int | None = None
    created_at: datetime | None = None
    limited_since: datetime | None = None
    delete_requested_at: datetime | None = None
    delete_exempt: bool = False
    is_trial: bool = False
    # Any order of the same user awaiting payment, in review, approved or provisioning,
    # or a failed provisioning of THIS account.
    has_open_order: bool = False
    last_change_at: datetime | None = None
    # `delwarn:{account}:{period}` -> when the warning was sent (sent rows only).
    warned: Mapping[str, datetime] = field(default_factory=dict)
    # Warning keys whose send was refused for good (the customer blocked the bot).
    undelivered: frozenset[str] = frozenset()
    # Who and how to name it, for the notices and the lists.
    user_id: int = 0
    owner_telegram_id: int = 0
    owner_banned: bool = False
    owner_is_reseller: bool = False
    owner_label: str = ""
    # What the CUSTOMER calls it (notices) and what the ADMIN lists call it (lists).
    label: str = ""
    title: str = ""


@dataclass(frozen=True)
class Decision:
    verdict: str
    reason: str
    kind: str | None = None
    days: int | None = None
    delete_on: datetime | None = None
    # WARN only: the service is already past its deadline and is deleted after the warning.
    after_warning: bool = False


def delete_warning_key(account_id: int, period: str) -> str:
    return f"delwarn:{account_id}:{period}"


def hard_stop_reached(deletes: int, total_services: int) -> bool:
    """Delete NOTHING when a run would delete this many."""
    if deletes > HARD_STOP_COUNT:
        return True
    return deletes > HARD_STOP_SMALL and deletes * 4 > total_services


def status_of(panel_user: Any) -> str:
    status = getattr(panel_user, "status", None)
    value = getattr(status, "value", status)
    return str(value or "").lower()


def _expire_of(panel_user: Any) -> datetime | None:
    expire = getattr(panel_user, "expire", None)
    if expire in (None, 0):
        return None
    return normalize_expire(expire)


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _keep(reason: str) -> Decision:
    return Decision(KEEP, reason)


def decide(  # noqa: PLR0911 - one early return per KEEP reason is the point
    facts: AccountFacts, panel_user: Any | None, config: LifecycleConfig, now: datetime
) -> Decision:
    """KEEP, WARN or DELETE for one service. See the module docstring."""
    if config.delete_mode not in (DELETE_MODE_REHEARSAL, DELETE_MODE_ON):
        return _keep("mode_off")
    if facts.delete_exempt:
        return _keep("exempt")
    if panel_user is None:
        return _keep("panel_not_read")
    answered_id = getattr(panel_user, "id", None)
    if (
        facts.panel_user_id is not None
        and answered_id is not None
        and answered_id != facts.panel_user_id
    ):
        return _keep("panel_id_mismatch")
    if facts.owner_banned:
        return _keep("owner_banned")
    status = status_of(panel_user)
    if status not in ("expired", "limited"):
        return _keep(f"status_{status or 'unknown'}")
    if facts.has_open_order:
        return _keep("open_order")
    changed = [m for m in (_aware(facts.created_at), _aware(facts.last_change_at)) if m]
    if changed and now - max(changed) < RECENT_CHANGE:
        return _keep("recent_change")

    if facts.is_trial:
        number, kind = config.delete_trial_after_days, KIND_TRIAL
    elif status == "expired":
        number, kind = config.delete_after_expiry_days, KIND_EXPIRED
    else:
        number, kind = config.delete_after_limited_days, KIND_LIMITED
    if number <= 0:
        return _keep("rule_off")

    if status == "expired":
        ended_at = _expire_of(panel_user)
        if ended_at is None:
            return _keep("no_expiry")
        if ended_at >= now:
            return _keep("not_yet_expired")
    else:
        ended_at = _aware(facts.limited_since)
        if ended_at is None:
            return _keep("limited_first_seen")
        if ended_at > now:
            return _keep("clock_ahead")
        # The bot (or an admin) changed the service after the clock started: a renewal, a
        # switch, added volume. That clock belongs to a spell that is over; start again.
        changed_at = _aware(facts.last_change_at)
        if changed_at is not None and changed_at > ended_at:
            return _keep("limited_first_seen")

    whole_days = int((now - ended_at) // timedelta(days=1))
    deadline = ended_at + timedelta(days=number + 1)  # the first moment whole_days > number
    past_deadline = whole_days > number
    warn_before = 0 if facts.is_trial else int(config.delete_warn_days_before)

    if warn_before <= 0:
        if past_deadline:
            return Decision(DELETE, kind, kind, whole_days)
        return Decision(KEEP, "not_due", kind, whole_days, delete_on=deadline)

    period = period_of(
        int(getattr(panel_user, "data_limit", 0) or 0), getattr(panel_user, "expire", None)
    )
    warned_at = _aware(facts.warned.get(delete_warning_key(facts.account_id, period)))
    if warned_at is not None and warned_at < ended_at:
        # Sent in an earlier ended spell (the key is the same after a panel-side usage
        # reset): it is no proof for this one.
        warned_at = None
    gap = timedelta(days=warn_before)
    if not past_deadline:
        if warned_at is None and now >= deadline - gap:
            return Decision(
                WARN, "warn_window", kind, whole_days, delete_on=max(deadline, now + gap)
            )
        return Decision(
            KEEP, "not_due" if warned_at is None else "already_warned", kind, whole_days
        )
    if warned_at is None:
        return Decision(
            WARN, "warn_first", kind, whole_days, delete_on=now + gap, after_warning=True
        )
    if now - warned_at >= gap:
        return Decision(DELETE, kind, kind, whole_days)
    return Decision(KEEP, "warned_recently", kind, whole_days, delete_on=warned_at + gap)
