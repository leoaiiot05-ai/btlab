"""When the automatic updates are due (SPEC.md §17.4). Pure: it takes the time, the schedule and the job runs.

A due time is the latest scheduled moment that has passed. An update is due when no run started after it, so a PC that
was off runs one missed update at start, not one per missed day (intentional).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from btlab.calendar import session, weekday
from btlab.markets import MARKETS
from btlab.records import JobRun, to_day

TAIPEI = ZoneInfo("Asia/Taipei")
# Python counts Monday as 0. The US update follows each New York session: Tuesday to Saturday in Taipei.
WEEKDAYS = {"US": (1, 2, 3, 4, 5), "TW": (0, 1, 2, 3, 4)}
RETRY_AFTER = timedelta(minutes=30)
RETRIES = 3
# FinMind's Sunday maintenance ends at 07:00 Asia/Taipei (§5.2).
MAINTENANCE_RETRY = time(7, 5)
SWEEP_DELAY = timedelta(minutes=15)


@dataclass(frozen=True)
class Due:
    """An update to start now, and the due time it answers: its job run's scheduled_for."""

    market: str
    scheduled_for: datetime


def due_time(market: str, at: time, now: datetime) -> datetime:
    """The market's latest scheduled moment at or before now, in UTC."""
    local = now.astimezone(TAIPEI)
    moments = (datetime.combine(local.date() - timedelta(days=back), at, TAIPEI) for back in range(8))
    return next(m for m in moments if m.weekday() in WEEKDAYS[market] and m <= local).astimezone(timezone.utc)


def next_time(market: str, at: time, now: datetime) -> datetime:
    """The market's first scheduled moment after now, in UTC: the state document's next update."""
    local = now.astimezone(TAIPEI)
    moments = (datetime.combine(local.date() + timedelta(days=ahead), at, TAIPEI) for ahead in range(8))
    return next(m for m in moments if m.weekday() in WEEKDAYS[market] and m > local).astimezone(timezone.utc)


def due_updates(now: datetime, times: Mapping[str, time], runs: Mapping[str, Sequence[JobRun]]) -> list[Due]:
    """The updates due now, earliest due time first. times holds each market's update time in Asia/Taipei, runs each
    market's update runs, newest first."""
    found = [due for market, at in times.items() if (due := _due(market, at, now, runs.get(market, ()))) is not None]
    return sorted(found, key=lambda due: due.scheduled_for)


def exhausted(runs: Sequence[JobRun], scheduled_for: datetime) -> bool:
    """Whether the update of a due time has failed, and failed again in each of its 3 retries."""
    return sum(run.scheduled_for == scheduled_for and run.outcome == "FAILED" for run in runs) > RETRIES


def _due(market: str, at: time, now: datetime, runs: Sequence[JobRun]) -> Due | None:
    latest = due_time(market, at, now)
    since = [run for run in runs if run.started_at >= latest]
    if not since:
        return Due(market, latest)
    last = since[0]
    # A run still going, or a manual one, answers the due time: only scheduled runs are retried.
    if last.finished_at is None or last.scheduled_for is None:
        return None
    if last.outcome == "FAILED" and not exhausted(since, last.scheduled_for) and now >= last.finished_at + RETRY_AFTER:
        return Due(market, last.scheduled_for)
    if last.outcome == "MAINTENANCE" and now >= _maintenance_retry(last.finished_at):
        return Due(market, last.scheduled_for)
    return None


def _maintenance_retry(moment: datetime) -> datetime:
    """The first 07:05 Asia/Taipei after moment."""
    local = moment.astimezone(TAIPEI)
    retry = datetime.combine(local.date(), MAINTENANCE_RETRY, TAIPEI)
    return (retry if retry > local else retry + timedelta(days=1)).astimezone(timezone.utc)


def due_sweep(market: str, now: datetime, last_swept: int | None) -> int | None:
    """The session day whose trade-mode sweep is due, once: 15 minutes after the session's end (§15.6, §17.4). A
    weekday holiday gets a sweep that finds nothing."""
    day = to_day(now.astimezone(ZoneInfo(MARKETS[market].time_zone)).date())
    if weekday(day) >= 5 or (last_swept is not None and last_swept >= day):
        return None
    return day if now.timestamp() >= session(market, day)[1] + SWEEP_DELAY.total_seconds() else None
