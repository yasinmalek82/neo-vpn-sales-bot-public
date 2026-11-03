"""``decide``: when an ended service may be deleted, as a table.

The owner's words: «a mistake here is expensive for me». A missed deletion is
harmless, a wrong one cannot be undone, so every case that is not clearly «ended
long enough, warned in time, nothing pending» is KEEP.

Boundary, chosen on the side of not deleting: ``whole days elapsed > N``. With
N = 10, ten days exactly (and ten days 23 hours) is KEEP; eleven days is DELETE.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.lifecycle_settings import LifecycleConfig
from services.service_cleanup import (
    DELETE,
    KEEP,
    WARN,
    AccountFacts,
    Decision,
    decide,
    delete_warning_key,
    hard_stop_reached,
)
from services.volume_warnings import period_of

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)


def config(**kw: object) -> LifecycleConfig:
    base = {
        "warn_days": (3, 1),
        "warn_volume_percents": (20, 5),
        "delete_mode": "on",
        "delete_after_expiry_days": 10,
        "delete_after_limited_days": 10,
        "delete_trial_after_days": 1,
        "delete_warn_days_before": 0,  # most rows isolate the clock; the warning has its own
        "last_rehearsal_at": NOW,
    }
    base.update(kw)
    return LifecycleConfig(**base)  # type: ignore[arg-type]


def facts(**kw: object) -> AccountFacts:
    base: dict[str, object] = {"account_id": 7, "created_at": NOW - 90 * DAY}
    base.update(kw)
    return AccountFacts(**base)  # type: ignore[arg-type]


def expired(ago: timedelta, *, status: str = "expired") -> SimpleNamespace:
    return SimpleNamespace(status=status, expire=NOW - ago, data_limit=10 * 1024**3, used_traffic=0)


def limited_user(**kw: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "status": "limited",
        "expire": None,
        "data_limit": 10 * 1024**3,
        "used_traffic": 10 * 1024**3,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def verdict(d: Decision) -> str:
    return d.verdict


# --- the expiry clock, at the boundary -----------------------------------------


@pytest.mark.parametrize(
    ("ago", "expected"),
    [
        (9 * DAY, KEEP),
        (10 * DAY, KEEP),  # ten days exactly: KEEP
        (10 * DAY + 23 * timedelta(hours=1) + timedelta(minutes=59), KEEP),  # 10d 23h 59m
        (11 * DAY, DELETE),  # eleven whole days: DELETE
        (11 * DAY + timedelta(seconds=1), DELETE),
        (200 * DAY, DELETE),
    ],
)
def test_an_expired_service_is_deleted_only_after_more_than_n_whole_days(
    ago: timedelta, expected: str
) -> None:
    got = decide(facts(), expired(ago), config(), NOW)
    assert got.verdict == expected, got
    if expected == DELETE:
        assert got.kind == "expired" and got.days == int(ago // DAY)


def test_the_number_is_the_owners_not_a_constant() -> None:
    assert (
        decide(facts(), expired(4 * DAY), config(delete_after_expiry_days=3), NOW).verdict == DELETE
    )
    assert (
        decide(facts(), expired(4 * DAY), config(delete_after_expiry_days=4), NOW).verdict == KEEP
    )


# --- the volume clock ------------------------------------------------------------


@pytest.mark.parametrize(
    ("since_ago", "expected", "reason"),
    [
        (None, KEEP, "limited_first_seen"),  # never deleted in the run that first sees it
        (timedelta(0), KEEP, "not_due"),  # seen limited today
        (10 * DAY, KEEP, "not_due"),
        (11 * DAY, DELETE, "limited"),
    ],
)
def test_a_limited_service_is_deleted_only_after_the_bot_has_seen_it_limited_long_enough(
    since_ago: timedelta | None, expected: str, reason: str
) -> None:
    since = None if since_ago is None else NOW - since_ago
    got = decide(facts(limited_since=since), limited_user(), config(), NOW)
    assert got.verdict == expected
    assert got.reason == reason or expected == DELETE
    if expected == DELETE:
        assert got.kind == "limited" and got.days == 11


def test_a_limited_service_with_no_expiry_is_still_deleted_on_the_volume_clock() -> None:
    got = decide(facts(limited_since=NOW - 30 * DAY), limited_user(expire=None), config(), NOW)
    assert got.verdict == DELETE


def test_a_service_that_got_volume_back_has_no_clock_so_it_starts_over() -> None:
    """The executor clears limited_since the first time it sees anything but limited."""
    back = SimpleNamespace(status="active", expire=None, data_limit=20 * 1024**3, used_traffic=0)
    assert decide(facts(limited_since=NOW - 30 * DAY), back, config(), NOW).verdict == KEEP
    again = decide(facts(limited_since=None), limited_user(), config(), NOW)
    assert again.verdict == KEEP and again.reason == "limited_first_seen"


# --- everything that is always KEEP ------------------------------------------------


def test_mode_off_keeps_everything_and_rehearsal_decides_like_on() -> None:
    old = expired(100 * DAY)
    assert decide(facts(), old, config(delete_mode="off"), NOW).verdict == KEEP
    assert decide(facts(), old, config(delete_mode="rehearsal"), NOW).verdict == DELETE
    assert decide(facts(), old, config(delete_mode="on"), NOW).verdict == DELETE
    assert decide(facts(), old, config(delete_mode="nonsense"), NOW).verdict == KEEP


def test_a_panel_user_that_was_not_read_is_kept() -> None:
    got = decide(facts(), None, config(), NOW)
    assert got.verdict == KEEP and got.reason == "panel_not_read"


@pytest.mark.parametrize("status", ["active", "on_hold", "disabled", "weird", "", None])
def test_only_expired_and_limited_are_ever_candidates(status: str | None) -> None:
    user = SimpleNamespace(status=status, expire=NOW - 100 * DAY, data_limit=1, used_traffic=1)
    assert decide(facts(limited_since=NOW - 100 * DAY), user, config(), NOW).verdict == KEEP


def test_an_active_user_with_a_past_expiry_is_kept_the_panel_has_not_flipped_it() -> None:
    assert decide(facts(), expired(100 * DAY, status="active"), config(), NOW).verdict == KEEP


@pytest.mark.parametrize("expire", [None, 0])
def test_an_expired_status_with_no_expiry_is_kept(expire: int | None) -> None:
    user = SimpleNamespace(status="expired", expire=expire, data_limit=1, used_traffic=0)
    got = decide(facts(), user, config(), NOW)
    assert got.verdict == KEEP and got.reason == "no_expiry"


def test_an_expired_status_with_an_expiry_in_the_future_is_kept_the_panel_disagrees() -> None:
    user = SimpleNamespace(status="expired", expire=NOW + DAY, data_limit=1, used_traffic=0)
    assert decide(facts(), user, config(), NOW).verdict == KEEP


def test_a_panel_expiry_as_a_unix_int_is_read() -> None:
    user = SimpleNamespace(
        status="expired", expire=int((NOW - 12 * DAY).timestamp()), data_limit=1, used_traffic=0
    )
    assert decide(facts(), user, config(), NOW).verdict == DELETE


@pytest.mark.parametrize(
    "field", ["delete_after_expiry_days", "delete_after_limited_days", "delete_trial_after_days"]
)
def test_a_number_of_zero_turns_that_rule_off(field: str) -> None:
    cases = {
        "delete_after_expiry_days": (facts(), expired(100 * DAY)),
        "delete_after_limited_days": (facts(limited_since=NOW - 100 * DAY), limited_user()),
        "delete_trial_after_days": (facts(is_trial=True), expired(100 * DAY)),
    }
    f, user = cases[field]
    assert decide(f, user, config(), NOW).verdict == DELETE
    off = decide(f, user, config(**{field: 0}), NOW)
    assert off.verdict == KEEP and off.reason == "rule_off"


def test_an_open_order_keeps_the_service() -> None:
    got = decide(facts(has_open_order=True), expired(100 * DAY), config(), NOW)
    assert got.verdict == KEEP and got.reason == "open_order"


def test_a_service_created_or_changed_in_the_last_day_is_kept() -> None:
    old = expired(100 * DAY)
    assert (
        decide(facts(created_at=NOW - 12 * timedelta(hours=1)), old, config(), NOW).verdict == KEEP
    )
    assert (
        decide(facts(last_change_at=NOW - timedelta(hours=23)), old, config(), NOW).verdict == KEEP
    )
    assert decide(facts(last_change_at=NOW - DAY), old, config(), NOW).verdict == DELETE


def test_the_owners_exemption_is_always_kept() -> None:
    got = decide(facts(delete_exempt=True), expired(500 * DAY), config(), NOW)
    assert got.verdict == KEEP and got.reason == "exempt"
    assert (
        decide(facts(delete_exempt=True, is_trial=True), expired(500 * DAY), config(), NOW).verdict
        == KEEP
    )


def test_a_banned_owner_is_never_deleted_nor_warned() -> None:
    got = decide(
        facts(owner_banned=True), expired(500 * DAY), config(delete_warn_days_before=3), NOW
    )
    assert got.verdict == KEEP


# --- trial vs paid -------------------------------------------------------------------


def test_a_trial_follows_its_own_number_and_a_paid_service_does_not() -> None:
    five = expired(5 * DAY)
    assert decide(facts(is_trial=False), five, config(), NOW).verdict == KEEP
    assert decide(facts(is_trial=True), five, config(), NOW).verdict == DELETE


@pytest.mark.parametrize(("ago", "expected"), [(DAY, KEEP), (2 * DAY, DELETE)])
def test_a_trial_boundary_is_the_same_whole_days_rule(ago: timedelta, expected: str) -> None:
    got = decide(facts(is_trial=True), expired(ago), config(), NOW)
    assert got.verdict == expected
    if expected == DELETE:
        assert got.kind == "trial"


def test_a_limited_trial_is_deleted_on_the_trial_number() -> None:
    f = facts(is_trial=True, limited_since=NOW - 2 * DAY)
    assert decide(f, limited_user(), config(), NOW).verdict == DELETE
    assert (
        decide(replace(f, limited_since=NOW - DAY), limited_user(), config(), NOW).verdict == KEEP
    )


def test_a_trial_needs_no_warning_even_when_the_warning_number_is_set() -> None:
    got = decide(facts(is_trial=True), expired(5 * DAY), config(delete_warn_days_before=3), NOW)
    assert got.verdict == DELETE


# --- warn first, then delete -----------------------------------------------------------------


def warned_facts(user: SimpleNamespace, sent_ago: timedelta | None, **kw: object) -> AccountFacts:
    key = delete_warning_key(7, period_of(int(user.data_limit or 0), user.expire))
    warned = {} if sent_ago is None else {key: NOW - sent_ago}
    return facts(warned=warned, **kw)


@pytest.mark.parametrize(
    ("ago", "expected", "reason"),
    [
        (5 * DAY, KEEP, "not_due"),
        (8 * DAY - timedelta(minutes=1), KEEP, "not_due"),
        (8 * DAY, WARN, "warn_window"),  # deadline (11 d) minus 3 days
        (10 * DAY, WARN, "warn_window"),
        (11 * DAY, WARN, "warn_first"),  # past the deadline, never warned: warn, do not delete
        (400 * DAY, WARN, "warn_first"),
    ],
)
def test_a_paid_service_is_warned_before_it_can_be_deleted(
    ago: timedelta, expected: str, reason: str
) -> None:
    user = expired(ago)
    got = decide(warned_facts(user, None), user, config(delete_warn_days_before=3), NOW)
    assert (got.verdict, got.reason) == (expected, reason)
    assert got.after_warning is (reason == "warn_first")


def test_the_warning_dates_the_deletion() -> None:
    # Warned now, deleted no sooner than 3 days later, and not before the 11-day deadline.
    user = expired(9 * DAY)
    got = decide(warned_facts(user, None), user, config(delete_warn_days_before=3), NOW)
    assert got.verdict == WARN and got.delete_on == NOW + 3 * DAY
    later = decide(
        warned_facts(user, None),
        user,
        config(delete_warn_days_before=3, delete_after_expiry_days=20),
        NOW,
    )
    assert later.verdict == KEEP  # the window opens at day 18
    window = expired(18 * DAY)
    got = decide(
        warned_facts(window, None),
        window,
        config(delete_warn_days_before=3, delete_after_expiry_days=20),
        NOW,
    )
    assert got.verdict == WARN and got.delete_on == NOW + 3 * DAY
    old = expired(40 * DAY)
    got = decide(warned_facts(old, None), old, config(delete_warn_days_before=3), NOW)
    assert got.delete_on == NOW + 3 * DAY  # warned now, deleted no sooner than 3 days later


@pytest.mark.parametrize(
    ("sent_ago", "expected"),
    [
        (timedelta(minutes=1), KEEP),
        (3 * DAY - timedelta(seconds=1), KEEP),  # warned just under 3 days ago
        (3 * DAY, DELETE),  # warned exactly delete_warn_days_before days ago
        (10 * DAY, DELETE),
    ],
)
def test_a_warned_service_is_deleted_only_once_the_warning_is_old_enough(
    sent_ago: timedelta, expected: str
) -> None:
    user = expired(40 * DAY)
    got = decide(warned_facts(user, sent_ago), user, config(delete_warn_days_before=3), NOW)
    assert got.verdict == expected, got


def test_a_warning_inside_the_window_is_not_repeated() -> None:
    user = expired(9 * DAY)
    got = decide(
        warned_facts(user, timedelta(hours=1)), user, config(delete_warn_days_before=3), NOW
    )
    assert got.verdict == KEEP


def test_a_warning_for_another_period_does_not_count_a_renewal_re_arms_it() -> None:
    """Warned for the old expiry; renewed; lapsed again: the new period needs its own warning."""
    user = expired(40 * DAY)
    old_period = SimpleNamespace(
        status="expired", expire=NOW - 200 * DAY, data_limit=user.data_limit
    )
    key = delete_warning_key(7, period_of(int(old_period.data_limit), old_period.expire))
    got = decide(facts(warned={key: NOW - 150 * DAY}), user, config(delete_warn_days_before=3), NOW)
    assert got.verdict == WARN and got.reason == "warn_first"


def test_a_warning_number_of_zero_turns_the_warning_requirement_off() -> None:
    user = expired(40 * DAY)
    got = decide(warned_facts(user, None), user, config(delete_warn_days_before=0), NOW)
    assert got.verdict == DELETE


def test_a_limited_paid_service_is_warned_too() -> None:
    f = facts(limited_since=NOW - 40 * DAY)
    got = decide(f, limited_user(), config(delete_warn_days_before=3), NOW)
    assert got.verdict == WARN and got.kind == "limited" and got.after_warning


# --- the hard stop -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("deletes", "total", "stopped"),
    [
        (0, 0, False),
        (1, 4, False),  # a four-service shop can delete one (25% is not enough on its own)
        (5, 10, False),  # 50%, but five is not more than five
        (6, 23, True),  # more than five and more than a quarter
        (6, 24, False),  # exactly a quarter
        (6, 20, True),
        (20, 100, False),
        (21, 100, True),  # more than twenty, whatever the share
        (21, 1000, True),
        (20, 70, True),  # 28% of 70 and more than five
    ],
)
def test_the_hard_stop(deletes: int, total: int, stopped: bool) -> None:
    assert hard_stop_reached(deletes, total) is stopped


# --- the adversarial review of 66ff2aa --------------------------------------------------------------


def test_b1_a_limited_clock_older_than_the_last_change_starts_again() -> None:
    """A renewal (or any change by the bot) after limited_since restarts the clock."""
    f = facts(limited_since=NOW - 20 * DAY, last_change_at=NOW - 5 * DAY)
    got = decide(f, limited_user(), config(), NOW)
    assert got.verdict == KEEP and got.reason == "limited_first_seen"
    # A change BEFORE the clock started does not restart it.
    f = facts(limited_since=NOW - 20 * DAY, last_change_at=NOW - 30 * DAY)
    assert decide(f, limited_user(), config(), NOW).verdict == DELETE


def test_b4_a_warning_from_an_earlier_ended_spell_is_not_proof_for_this_one() -> None:
    """Same limit and expiry (a panel-side usage reset), so the same key: only a warning
    sent AFTER the current spell began counts."""
    user = limited_user()
    key = delete_warning_key(7, period_of(int(user.data_limit), user.expire))
    stale = facts(limited_since=NOW - 40 * DAY, warned={key: NOW - 100 * DAY})
    got = decide(stale, user, config(delete_warn_days_before=3), NOW)
    assert got.verdict == WARN and got.reason == "warn_first"
    fresh = facts(limited_since=NOW - 40 * DAY, warned={key: NOW - 10 * DAY})
    assert decide(fresh, user, config(delete_warn_days_before=3), NOW).verdict == DELETE
    expired_user = expired(40 * DAY)
    key = delete_warning_key(7, period_of(int(expired_user.data_limit), expired_user.expire))
    old = facts(warned={key: NOW - 100 * DAY})  # sent 60 days before the spell began
    assert decide(old, expired_user, config(delete_warn_days_before=3), NOW).verdict == WARN


def test_r14_the_same_username_answering_as_another_panel_user_is_kept() -> None:
    user = expired(100 * DAY)
    user.id = 6
    got = decide(facts(panel_user_id=5), user, config(), NOW)
    assert got.verdict == KEEP and got.reason == "panel_id_mismatch"
    user.id = 5
    assert decide(facts(panel_user_id=5), user, config(), NOW).verdict == DELETE
    # No id known on either side (an old row): decided on the rest.
    assert decide(facts(), expired(100 * DAY), config(), NOW).verdict == DELETE
