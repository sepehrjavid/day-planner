"""Interval arithmetic: what's actually free, what a zone's own concrete
occurrences are, and what a new constraint collides with (A4.1).

Every function here takes an explicit `tz` (a zoneinfo.ZoneInfo) to
resolve wall-clock "HH:MM" strings against concrete instants. Using
tz-aware datetimes throughout — never naive ones, never manual UTC-offset
arithmetic — is what makes `datetime + timedelta` land on the correct
wall-clock instant across a DST transition, and what makes ordering
comparisons (`<`, `>`, used everywhere below) correct.

Elapsed *duration* is the one place this isn't automatic: CPython's
datetime subtraction takes a fast path — skipping UTC-offset resolution
entirely — whenever both operands share the identical tzinfo object,
which zoneinfo.ZoneInfo always does for two datetimes built from the
same zone key. That silently produces a 24-hour answer for a 23-hour
DST-spring-forward day. See models.py's Interval.duration_minutes for
the actual fix (subtract via a fixed-offset zone instead) and
test_scheduling.py's DST cases for what breaks without it.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Sequence, TypeVar
from zoneinfo import ZoneInfo

from .models import DAYS_OF_WEEK, Interval, SleepSchedule, Zone

T = TypeVar("T")


def _weekday_code(d: date) -> str:
    return DAYS_OF_WEEK[d.weekday()]


def _parse_hhmm(value: str) -> int:
    """Minutes since midnight. No validation beyond what int() gives you
    for free — callers are expected to have already validated the
    "HH:MM" shape at the schema boundary (see models.py's own docstring)
    before it ever reaches here."""
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def _wall_clock_interval(day: date, start_time: str, end_time: str, tz: ZoneInfo) -> Interval:
    """The concrete Interval for a window that starts on `day` at
    start_time. end_time <= start_time means the window crosses into
    day + 1 (e.g. sleep 23:00 -> 07:00) — except when they're exactly
    equal, which is the zero-length-window convention (see Interval's own
    docstring), not a full-day wraparound."""
    midnight = datetime.combine(day, time.min, tzinfo=tz)
    start_minutes = _parse_hhmm(start_time)
    end_minutes = _parse_hhmm(end_time)

    start_dt = midnight + timedelta(minutes=start_minutes)
    if end_minutes == start_minutes:
        return Interval(start_dt, start_dt)
    if end_minutes > start_minutes:
        end_dt = midnight + timedelta(minutes=end_minutes)
    else:
        end_dt = midnight + timedelta(days=1, minutes=end_minutes)
    return Interval(start_dt, end_dt)


def _resolve_wake_day_times(schedule: SleepSchedule, wake_day: date) -> tuple[str, str]:
    """(sleep_time, wake_time) for the night that wakes up on `wake_day`.

    day_overrides is keyed by the *wake* day, not the bedtime day — "day
    _overrides for days that differ, e.g. sleeping in on Sundays" means
    Sunday *morning*, i.e. the night that started the previous evening.
    Both fields of one override describe that same night as a unit: if
    day_overrides["sun"] only sets wake_time, sleep_time still falls back
    to the schedule default for that same night."""
    override = schedule.day_overrides.get(_weekday_code(wake_day))
    sleep_time = (override.sleep_time if override else None) or schedule.sleep_time
    wake_time = (override.wake_time if override else None) or schedule.wake_time
    return sleep_time, wake_time


def _sleep_span_waking_on(schedule: SleepSchedule, wake_day: date, tz: ZoneInfo) -> Interval:
    sleep_time, wake_time = _resolve_wake_day_times(schedule, wake_day)
    # Whether this night's bedtime falls on wake_day itself (a same-day,
    # non-crossing schedule) or the day before (the ordinary case, sleep_
    # time > wake_time) is decided by this specific night's own resolved
    # times, not by the schedule's un-overridden default — an override
    # could in principle flip which side of midnight a given night falls
    # on, though in practice it never restructures more than an hour or
    # two.
    night_start_day = (
        wake_day if _parse_hhmm(wake_time) >= _parse_hhmm(sleep_time)
        else wake_day - timedelta(days=1)
    )
    return _wall_clock_interval(night_start_day, sleep_time, wake_time, tz)


def zone_occurrences(
    zone: Zone, date_range: tuple[date, date], *, tz: ZoneInfo
) -> list[Interval]:
    """Every concrete occurrence of `zone` within [start_date, end_date) —
    one Interval per day whose weekday matches zone.days_of_week. This is
    what a zone-anchored habit places exactly one session per occurrence
    of, at the zone's own start_time/end_time — see instruction.md's
    placement paragraph for why that's the *only* habit kind allowed to
    skip choosing its own time."""
    start_date, end_date = date_range
    occurrences: list[Interval] = []
    day = start_date
    while day < end_date:
        if _weekday_code(day) in zone.days_of_week:
            occurrences.append(_wall_clock_interval(day, zone.start_time, zone.end_time, tz))
        day += timedelta(days=1)
    return occurrences


def _merge(intervals: list[Interval]) -> list[Interval]:
    """Sorted, overlap-merged. A zero-length interval is absorbed into
    whatever it touches and otherwise contributes nothing — it can never
    itself create a gap."""
    ordered = sorted(intervals, key=lambda iv: iv.start)
    merged: list[Interval] = []
    for iv in ordered:
        if merged and iv.start <= merged[-1].end:
            if iv.end > merged[-1].end:
                merged[-1] = Interval(merged[-1].start, iv.end)
        else:
            merged.append(iv)
    return merged


def free_intervals(
    date_range: tuple[date, date],
    *,
    tz: ZoneInfo,
    zones: Sequence[Zone] = (),
    sleep_schedule: SleepSchedule | None = None,
    busy: Sequence[Interval] = (),
    allowed_zones: Sequence[str] = (),
) -> list[Interval]:
    """Every open interval within [start_date, end_date) once zones, the
    sleep schedule, and existing calendar events are all subtracted out.

    Blocking rules (see instruction.md's placement paragraph, which this
    mirrors exactly):
    - A zone whose days_of_week includes a given day blocks its
      [start_time, end_time) on that day, unless the zone's own label is
      in `allowed_zones`.
    - The sleep period [sleep_time, wake_time) — using that day's
      day_overrides if present, else the default — is blocked on every
      day and is never overridable by allowed_zones or anything else.
    - cool_down_minutes immediately before sleep_time, and
      wake_up_buffer_minutes immediately after wake_time, are blocked
      like ordinary zones: overridable via the fixed labels "cool-down"
      and "wake-up" in `allowed_zones`.
    - Every interval in `busy` (existing calendar events) is blocked and
      is never overridable.

    date_range is a half-open [start_date, end_date) day range; the
    returned intervals are clipped to exactly that span, in wall-clock
    terms for `tz`.
    """
    start_date, end_date = date_range
    if end_date <= start_date:
        return []

    range_start = datetime.combine(start_date, time.min, tzinfo=tz)
    range_end = datetime.combine(end_date, time.min, tzinfo=tz)

    allowed = set(allowed_zones)
    blocked: list[Interval] = []

    for zone in zones:
        if zone.label in allowed:
            continue
        day = start_date
        while day < end_date:
            if _weekday_code(day) in zone.days_of_week:
                blocked.append(_wall_clock_interval(day, zone.start_time, zone.end_time, tz))
            day += timedelta(days=1)

    if sleep_schedule is not None:
        # Iterated by wake day, not bedtime day (see
        # _resolve_wake_day_times) — a night waking on start_date can
        # still bleed into the range (it started the evening before), so
        # start one wake day early; a night waking the instant after
        # end_date is out of range by definition and left for clipping
        # below to drop entirely.
        wake_day = start_date
        while wake_day <= end_date + timedelta(days=1):
            sleep_span = _sleep_span_waking_on(sleep_schedule, wake_day, tz)
            blocked.append(sleep_span)  # never overridable, at any length

            if "cool-down" not in allowed and sleep_schedule.cool_down_minutes > 0:
                blocked.append(
                    Interval(
                        sleep_span.start
                        - timedelta(minutes=sleep_schedule.cool_down_minutes),
                        sleep_span.start,
                    )
                )
            if "wake-up" not in allowed and sleep_schedule.wake_up_buffer_minutes > 0:
                blocked.append(
                    Interval(
                        sleep_span.end,
                        sleep_span.end
                        + timedelta(minutes=sleep_schedule.wake_up_buffer_minutes),
                    )
                )
            wake_day += timedelta(days=1)

    blocked.extend(busy)

    clipped: list[Interval] = []
    for iv in blocked:
        s = max(iv.start, range_start)
        e = min(iv.end, range_end)
        if s < e:
            clipped.append(Interval(s, e))

    merged = _merge(clipped)

    free: list[Interval] = []
    cursor = range_start
    for iv in merged:
        if iv.start > cursor:
            free.append(Interval(cursor, iv.start))
        cursor = max(cursor, iv.end)
    if cursor < range_end:
        free.append(Interval(cursor, range_end))

    return free


def _overlaps(a: Interval, b: Interval) -> bool:
    """Strict overlap — touching endpoints don't count, the same
    half-open convention list_habit_sessions' own Firestore range query
    uses."""
    return a.start < b.end and b.start < a.end


def collisions_with(
    new_constraint: Sequence[Interval], placed_sessions: Sequence[tuple[T, Interval]]
) -> list[T]:
    """Which already-placed sessions a new or changed constraint (a
    zone's occurrences, a sleep-schedule change, ...) now collides with —
    the "conflict you create by learning something new" case in
    instruction.md's second placement paragraph.

    `placed_sessions` pairs each session with its own Interval so the
    caller gets back the *session* (whatever identifies it to them —
    a habit_id/event_id pair, a full HabitSession, anything), not a bare
    Interval it would have to re-match against something. Order is
    preserved from the input.
    """
    return [
        session
        for session, interval in placed_sessions
        if any(_overlaps(interval, c) for c in new_constraint)
    ]
