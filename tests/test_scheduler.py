"""The schedule of the automatic updates: due times, catch-up and retries (SPEC.md §17.4, AC-18)."""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from btlab.records import JobRun
from btlab.scheduler import Due, due_time, due_updates, exhausted

TAIPEI = ZoneInfo("Asia/Taipei")
TIMES = {"US": time(8, 30), "TW": time(18, 0)}


def taipei(day: int, hour: int, minute: int = 0, month: int = 10) -> datetime:
    """A moment of 2026 in Asia/Taipei, as UTC. 26 October 2026 is a Monday."""
    return datetime(2026, month, day, hour, minute, tzinfo=TAIPEI).astimezone(timezone.utc)


def run(started: datetime, outcome: str | None = "UPDATED", scheduled_for: datetime | None = None,
        minutes: int = 5, kind: str = "UPDATE_US") -> JobRun:
    finished = None if outcome is None else started + timedelta(minutes=minutes)
    return JobRun(1, "DATA", kind, scheduled_for, started, finished, outcome, None)


@pytest.mark.parametrize(
    "market, now, due",
    [
        ("US", taipei(28, 9), taipei(28, 8, 30)),  # Wednesday morning: today's
        ("US", taipei(28, 8), taipei(27, 8, 30)),  # before today's time: Tuesday's
        ("US", taipei(26, 9), taipei(24, 8, 30)),  # Monday: Saturday's, as Sunday and Monday have none
        ("US", taipei(25, 23), taipei(24, 8, 30)),  # Sunday
        ("TW", taipei(31, 12), taipei(30, 18)),  # Saturday: Friday's
        ("TW", taipei(26, 18), taipei(26, 18)),  # Monday at the due time
        ("TW", taipei(26, 17, 59), taipei(23, 18)),  # just before: the Friday before
    ],
)
def test_due_times_fall_on_the_markets_weekdays_in_taipei(market, now, due):
    assert due_time(market, TIMES[market], now) == due


def test_the_first_start_runs_both_updates_earliest_due_first():
    assert due_updates(taipei(28, 9), TIMES, {}) == [Due("TW", taipei(27, 18)), Due("US", taipei(28, 8, 30))]


def test_a_pc_that_was_off_catches_up_once():
    # The last US run was on the 20th; a week of due times passed while the PC was off.
    runs = {"US": [run(taipei(20, 8, 30), scheduled_for=taipei(20, 8, 30))]}
    assert due_updates(taipei(28, 9), {"US": TIMES["US"]}, runs) == [Due("US", taipei(28, 8, 30))]


@pytest.mark.parametrize(
    "latest",
    [
        run(taipei(28, 8, 31), scheduled_for=taipei(28, 8, 30)),  # done
        run(taipei(28, 8, 31), outcome=None, scheduled_for=taipei(28, 8, 30)),  # still going
        run(taipei(28, 8, 40), outcome="FAILED"),  # a manual run answered the due time: no retry
    ],
    ids=["done", "running", "manual"],
)
def test_nothing_is_due_after_a_run_answered_the_due_time(latest):
    assert due_updates(taipei(28, 12), {"US": TIMES["US"]}, {"US": [latest]}) == []


def test_a_failed_scheduled_update_retries_after_30_minutes_at_most_3_times():
    due = taipei(28, 8, 30)
    failures = [run(due + timedelta(minutes=40 * k), "FAILED", due) for k in range(4)][::-1]
    for count in range(1, 5):
        runs = failures[-count:]  # newest first
        retry_at = runs[0].finished_at + timedelta(minutes=30)
        expected = [] if count == 4 else [Due("US", due)]
        assert due_updates(retry_at - timedelta(minutes=1), {"US": TIMES["US"]}, {"US": runs}) == []
        assert due_updates(retry_at, {"US": TIMES["US"]}, {"US": runs}) == expected
        assert exhausted(runs, due) == (count == 4)


def test_maintenance_retries_at_7_05_taipei():
    # The PC starts on Sunday at 03:00, during FinMind's maintenance; Friday's TW update catches up then.
    due = taipei(30, 18)
    runs = {"TW": [run(taipei(1, 3, month=11), "MAINTENANCE", due, minutes=1, kind="UPDATE_TW")]}
    assert due_updates(taipei(1, 7, 4, month=11), {"TW": TIMES["TW"]}, runs) == []
    assert due_updates(taipei(1, 7, 5, month=11), {"TW": TIMES["TW"]}, runs) == [Due("TW", due)]
