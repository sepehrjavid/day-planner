"""Invariant library for A3.1's eval scenarios — tier 1 (constraint,
"did it break a rule") and tier 2 (decision, "did it choose sensibly").

Each invariant is a pure function: (world, placed_events, tool_calls) ->
InvariantResult.

world is the *final* zones/sleep_schedule/habits state after the
scenario ran — deliberately not the scenario's static `given` block.
Some scenarios (e.g. a zone-anchored habit the model creates mid-
conversation via create_habit/update_habit) only have a real habit_id
and allowed_zones once the run is over; checking against `given.habits`
alone would make every such scenario fail these checks for a reason that
has nothing to do with whether the agent behaved correctly. See
evals/runner.py, which builds World from its ScenarioFixture's live
state after each trial, not from the Scenario object. calendar_events and
today, unlike zones/sleep_schedule/habits, are static for the whole
scenario (existing events and "today" don't change mid-run) — carried on
World anyway so the tier-2 invariants below don't need a second
parameter threaded through every call site.

placed_events is the raw Google Calendar item shape (see conftest.py's
FakeEventsResource) reflecting everything add_calendar_event actually
inserted during the run — not what the model *said* it would do, per
A3.1's "assert on tool calls and arguments, never on wording." tool_calls
is the ordered list of {"name", "args"} dicts collected from the ADK
event stream for this run.

Tier-2 invariants need a day-load notion the real scheduling engine
(A4.1) doesn't exist yet to supply — see chosen_slot_ranks_above_median's
own docstring for the deliberately coarse, day-level (not full slot-
legality) approximation used here, matching A3.1's own documented
fallback for it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from datetime import time as dtime
from datetime import timedelta

_HABIT_ID_PROPERTY = "day_planner_habit_id"
_OFFSET_RE = re.compile(r"(Z|[+-]\d{2}:\d{2})$")


@dataclass
class World:
    """Final fixture state after a scenario trial ran — see the module
    docstring for why this isn't just Scenario.given."""

    zones: list[dict] = field(default_factory=list)
    sleep_schedule: dict | None = None
    habits: list[dict] = field(default_factory=list)
    calendar_events: list[dict] = field(default_factory=list)
    today: str | None = None

    def habit_by_id(self, habit_id: str) -> dict | None:
        for h in self.habits:
            if h["habit_id"] == habit_id:
                return h
        return None


@dataclass
class InvariantResult:
    passed: bool
    detail: str = ""


def _strip_offset(value: str) -> str:
    return _OFFSET_RE.sub("", value)


def _parse_wall_clock(value: str) -> datetime:
    """Local wall-clock components only — zones/sleep are defined in the
    same local frame add_calendar_event places events in, so comparing
    offset-naive components avoids a spurious UTC conversion that would
    make an in-zone placement look fine or a compliant one look wrong."""
    return datetime.fromisoformat(_strip_offset(value))


def _day_key(d: date) -> str:
    return d.strftime("%a").lower()[:3]


def _time_of(hhmm: str) -> dtime:
    return dtime.fromisoformat(hhmm)


def _shift(t: dtime, minutes: int) -> dtime:
    return (datetime.combine(date(2000, 1, 1), t) + timedelta(minutes=minutes)).time()


def _in_same_day_interval(start: datetime, end: datetime, window_start: dtime, window_end: dtime) -> bool:
    """True if [start, end) overlaps [window_start, window_end) on the
    same calendar day as start. Zones don't span midnight in this
    codebase (see zone_tools.py), so same-day comparison is sufficient."""
    ws = datetime.combine(start.date(), window_start)
    we = datetime.combine(start.date(), window_end)
    return start < we and end > ws


def _in_wrapping_window(t: dtime, window_start: dtime, window_end: dtime) -> bool:
    """True if t falls in [window_start, window_end), where the window
    may wrap past midnight (the normal case for sleep: e.g. 22:30 to
    07:15). Falls back to a same-day interval when it doesn't wrap."""
    if window_start <= window_end:
        return window_start <= t < window_end
    return t >= window_start or t < window_end


def _habit_id_of(event: dict) -> str | None:
    return (event.get("extendedProperties") or {}).get("private", {}).get(_HABIT_ID_PROPERTY)


def _habit_tagged_events(placed_events: list[dict]) -> list[tuple[dict, str]]:
    result = []
    for e in placed_events:
        habit_id = _habit_id_of(e)
        if habit_id:
            result.append((e, habit_id))
    return result


# ---------------------------------------------------------------------------
# Tier 1 invariants
# ---------------------------------------------------------------------------


def no_session_overlaps_any_zone(
    world: World, placed_events: list[dict], tool_calls: list[dict]
) -> InvariantResult:
    violations = []
    for event, habit_id in _habit_tagged_events(placed_events):
        habit = world.habit_by_id(habit_id)
        allowed = set(habit.get("allowed_zones") or []) if habit else set()
        start = _parse_wall_clock(event["start"]["dateTime"])
        end = _parse_wall_clock(event["end"]["dateTime"])
        for zone in world.zones:
            if zone["label"] in allowed:
                continue
            if _day_key(start.date()) not in zone["days_of_week"]:
                continue
            if _in_same_day_interval(
                start, end, _time_of(zone["start_time"]), _time_of(zone["end_time"])
            ):
                violations.append(
                    f"{event.get('summary')!r} at {event['start']['dateTime']} "
                    f"overlaps zone {zone['label']!r}"
                )
    return InvariantResult(not violations, "; ".join(violations))


def _effective_sleep_times(schedule: dict, day_key: str) -> tuple[dtime, dtime]:
    """day_overrides (e.g. sleeping in on Sundays) replace sleep_time/
    wake_time for that day only — cool_down_minutes/wake_up_buffer_minutes
    stay global, matching set_sleep_schedule's own shape (day_overrides
    only ever carries sleep_time/wake_time, never the buffer minutes)."""
    override = (schedule.get("day_overrides") or {}).get(day_key)
    if override:
        return _time_of(override["sleep_time"]), _time_of(override["wake_time"])
    return _time_of(schedule["sleep_time"]), _time_of(schedule["wake_time"])


def no_session_overlaps_sleep_or_cooldown(
    world: World, placed_events: list[dict], tool_calls: list[dict]
) -> InvariantResult:
    schedule = world.sleep_schedule
    if not schedule:
        return InvariantResult(True, "no sleep schedule in this scenario")

    violations = []
    for event, habit_id in _habit_tagged_events(placed_events):
        habit = world.habit_by_id(habit_id)
        allowed = set(habit.get("allowed_zones") or []) if habit else set()
        start = _parse_wall_clock(event["start"]["dateTime"])
        t = start.time()
        sleep_t, wake_t = _effective_sleep_times(schedule, _day_key(start.date()))
        cooldown_start = _shift(sleep_t, -schedule["cool_down_minutes"])
        wakebuffer_end = _shift(wake_t, schedule["wake_up_buffer_minutes"])

        # The sleep period itself is never overridable by anything.
        if _in_wrapping_window(t, sleep_t, wake_t):
            violations.append(
                f"{event.get('summary')!r} at {event['start']['dateTime']} overlaps sleep"
            )
            continue
        if "cool-down" not in allowed and _in_wrapping_window(t, cooldown_start, sleep_t):
            violations.append(
                f"{event.get('summary')!r} at {event['start']['dateTime']} overlaps cool-down"
            )
            continue
        if "wake-up" not in allowed and _in_wrapping_window(t, wake_t, wakebuffer_end):
            violations.append(
                f"{event.get('summary')!r} at {event['start']['dateTime']} overlaps wake-up buffer"
            )
    return InvariantResult(not violations, "; ".join(violations))


def every_habit_session_passes_habit_id(
    world: World, placed_events: list[dict], tool_calls: list[dict]
) -> InvariantResult:
    known_ids = {h["habit_id"] for h in world.habits}
    violations = []
    for call in tool_calls:
        if call["name"] != "add_calendar_event":
            continue
        habit_id = call["args"].get("habit_id")
        if not habit_id:
            violations.append(f"add_calendar_event({call['args'].get('summary')!r}) missing habit_id")
        elif habit_id not in known_ids:
            violations.append(f"add_calendar_event passed unknown habit_id {habit_id!r}")
    return InvariantResult(not violations, "; ".join(violations))


def no_habit_id_on_plain_appointment(
    world: World, placed_events: list[dict], tool_calls: list[dict]
) -> InvariantResult:
    violations = [
        f"add_calendar_event({call['args'].get('summary')!r}) unexpectedly tagged habit_id="
        f"{call['args'].get('habit_id')!r}"
        for call in tool_calls
        if call["name"] == "add_calendar_event" and call["args"].get("habit_id")
    ]
    return InvariantResult(not violations, "; ".join(violations))


_WEEKLY_TARGET_RE = re.compile(r"(\d+)\s*min(?:ute)?s?\s*/\s*week")


def placed_minutes_meets_target(
    world: World, placed_events: list[dict], tool_calls: list[dict]
) -> InvariantResult:
    shortfalls = []
    totals: dict[str, int] = {}
    for event, habit_id in _habit_tagged_events(placed_events):
        start = _parse_wall_clock(event["start"]["dateTime"])
        end = _parse_wall_clock(event["end"]["dateTime"])
        totals[habit_id] = totals.get(habit_id, 0) + int((end - start).total_seconds() // 60)

    for habit in world.habits:
        match = _WEEKLY_TARGET_RE.search(habit["goal"])
        if not match:
            continue  # zone-anchored or otherwise target-less habit — not this invariant's concern
        target = int(match.group(1))
        placed = totals.get(habit["habit_id"], 0)
        if placed < target:
            shortfalls.append(
                f"{habit['label']!r}: placed {placed} min, target {target} min/week"
            )
    return InvariantResult(not shortfalls, "; ".join(shortfalls))


def zone_anchored_sessions_match_zone_times(
    world: World, placed_events: list[dict], tool_calls: list[dict]
) -> InvariantResult:
    zones_by_label = {z["label"]: z for z in world.zones}
    violations = []
    for habit in world.habits:
        if _WEEKLY_TARGET_RE.search(habit["goal"]):
            continue  # has its own frequency/duration target — not zone-anchored
        anchor_zones = [
            zones_by_label[label] for label in habit.get("allowed_zones", []) if label in zones_by_label
        ]
        if not anchor_zones:
            continue
        placed_for_habit = [e for e, hid in _habit_tagged_events(placed_events) if hid == habit["habit_id"]]
        for event in placed_for_habit:
            start = _parse_wall_clock(event["start"]["dateTime"])
            end = _parse_wall_clock(event["end"]["dateTime"])
            matches_some_zone = any(
                _day_key(start.date()) in zone["days_of_week"]
                and start.time() == _time_of(zone["start_time"])
                and end.time() == _time_of(zone["end_time"])
                for zone in anchor_zones
            )
            if not matches_some_zone:
                violations.append(
                    f"{event.get('summary')!r} at {event['start']['dateTime']}-"
                    f"{event['end']['dateTime']} doesn't match any anchor zone's own time"
                )
    return InvariantResult(not violations, "; ".join(violations))


TIER1_INVARIANTS = {
    "no_session_overlaps_any_zone": no_session_overlaps_any_zone,
    "no_session_overlaps_sleep_or_cooldown": no_session_overlaps_sleep_or_cooldown,
    "every_habit_session_passes_habit_id": every_habit_session_passes_habit_id,
    "no_habit_id_on_plain_appointment": no_habit_id_on_plain_appointment,
    "placed_minutes_meets_target": placed_minutes_meets_target,
    "zone_anchored_sessions_match_zone_times": zone_anchored_sessions_match_zone_times,
}


# ---------------------------------------------------------------------------
# Tier 2 (decision) invariants — "did it choose sensibly," not just legally.
# Gate at >=90%, warn-only, per A3.1's own tier table — never block a
# release the way a tier-1 failure does.
# ---------------------------------------------------------------------------


def _existing_event_minutes_on(day: date, calendar_events: list[dict]) -> int:
    total = 0
    for e in calendar_events:
        start = e.get("start", {}).get("dateTime")
        end = e.get("end", {}).get("dateTime")
        if not start or not end:
            continue  # an all-day event (date, not dateTime) doesn't count as timed load
        s = _parse_wall_clock(start)
        if s.date() != day:
            continue
        total += int((_parse_wall_clock(end) - s).total_seconds() // 60)
    return total


def _week_days(today: str) -> list[date]:
    """The 7 calendar days starting today — every scenario in this suite
    phrases its ask as "this week"/"the next 7 days" from today, so this
    is the period tier-2 invariants reason over. A real scheduling engine
    (A4.1) would derive this from the actual request; this is the
    documented, coarser stand-in A3.1's own scope note anticipates."""
    start = datetime.strptime(today, "%Y-%m-%d").date()
    return [start + timedelta(days=i) for i in range(7)]


def heavier_load_on_lighter_days(
    world: World, placed_events: list[dict], tool_calls: list[dict]
) -> InvariantResult:
    """For the same habit, a day with less pre-existing calendar load
    must not get a *shorter* session than a heavier day also got — see
    instruction.md's "give a packed day a shorter session... and a light
    day a longer one." Only compares pairs of days that both actually
    received a placement for the same habit; says nothing about days
    that got no session at all (that's what placed_minutes_meets_target
    and the constraint invariants are for)."""
    by_habit: dict[str, list[tuple[date, int]]] = {}
    for event, habit_id in _habit_tagged_events(placed_events):
        start = _parse_wall_clock(event["start"]["dateTime"])
        end = _parse_wall_clock(event["end"]["dateTime"])
        duration = int((end - start).total_seconds() // 60)
        by_habit.setdefault(habit_id, []).append((start.date(), duration))

    violations = []
    for habit_id, sessions in by_habit.items():
        loads = {d: _existing_event_minutes_on(d, world.calendar_events) for d, _ in sessions}
        for (day_a, dur_a), (day_b, dur_b) in ((a, b) for a in sessions for b in sessions):
            if day_a == day_b:
                continue
            if loads[day_a] < loads[day_b] and dur_a < dur_b:
                violations.append(
                    f"{habit_id}: {day_a} (lighter, {loads[day_a]}min load) got a "
                    f"{dur_a}min session, shorter than {day_b} (heavier, "
                    f"{loads[day_b]}min load)'s {dur_b}min session"
                )
    return InvariantResult(not violations, "; ".join(sorted(set(violations))))


def weekend_preferred_when_weekend_is_free(
    world: World, placed_events: list[dict], tool_calls: list[dict]
) -> InvariantResult:
    """When the week's weekend has no pre-existing commitments at all,
    instruction.md's own stated preference is to load a larger share of
    the target onto it before falling back to weekdays. "Larger share"
    is checked against the flat 2/7 baseline two weekend days out of
    seven would get with no preference at all — a low bar, deliberately:
    this is a tier-2 "did it lean the right direction" check, not a
    precise target."""
    if not world.today:
        return InvariantResult(True, "no `today` on this scenario — not applicable")

    week = _week_days(world.today)
    weekend_days = {d for d in week if d.weekday() in (5, 6)}
    if not weekend_days:
        return InvariantResult(True, "this week has no weekend day in range")

    weekend_has_existing_load = any(
        _existing_event_minutes_on(d, world.calendar_events) > 0 for d in weekend_days
    )
    if weekend_has_existing_load:
        return InvariantResult(True, "weekend isn't free this week — precondition doesn't apply")

    total = weekend_total = 0
    for event, _habit_id in _habit_tagged_events(placed_events):
        start = _parse_wall_clock(event["start"]["dateTime"])
        end = _parse_wall_clock(event["end"]["dateTime"])
        duration = int((end - start).total_seconds() // 60)
        total += duration
        if start.date() in weekend_days:
            weekend_total += duration

    if total == 0:
        return InvariantResult(True, "nothing placed this week")

    fair_share = len(weekend_days) / 7
    actual_share = weekend_total / total
    passed = actual_share >= fair_share
    return InvariantResult(
        passed,
        "" if passed else f"weekend got {actual_share:.0%} of placed minutes, expected >= {fair_share:.0%}",
    )


def chosen_slot_ranks_above_median(
    world: World, placed_events: list[dict], tool_calls: list[dict]
) -> InvariantResult:
    """A3.1's own documented fallback for this invariant, verbatim: until
    A4.1's real scorer exists, assert the weaker property that the
    chosen slot is not in the bottom quartile of legal candidate days by
    day-load. "Candidate days" here means every day in the week (see
    _week_days) — day-level, not the full free-interval/slot-legality
    computation A4.1 will eventually own; a coarse stand-in, not a
    preview of that engine."""
    if not world.today:
        return InvariantResult(True, "no `today` on this scenario — not applicable")

    week = _week_days(world.today)  # always 7 days — see _week_days
    loads = sorted(_existing_event_minutes_on(d, world.calendar_events) for d in week)
    # Bottom quartile by desirability = the most heavily loaded 25% of
    # candidate days — the threshold is the load value at the 75th
    # percentile; a day loaded more than that is in the worst quarter.
    threshold_index = max(0, int(len(loads) * 0.75) - 1)
    threshold = loads[threshold_index]

    violations = []
    for event, habit_id in _habit_tagged_events(placed_events):
        start = _parse_wall_clock(event["start"]["dateTime"])
        day_load = _existing_event_minutes_on(start.date(), world.calendar_events)
        if day_load > threshold:
            violations.append(
                f"{event.get('summary')!r} on {start.date()} (load={day_load}min) is in "
                f"the bottom quartile of this week's days by load (threshold={threshold}min)"
            )
    return InvariantResult(not violations, "; ".join(violations))


TIER2_INVARIANTS = {
    "heavier_load_on_lighter_days": heavier_load_on_lighter_days,
    "weekend_preferred_when_weekend_is_free": weekend_preferred_when_weekend_is_free,
    "chosen_slot_ranks_above_median": chosen_slot_ranks_above_median,
}

ALL_INVARIANTS = {**TIER1_INVARIANTS, **TIER2_INVARIANTS}
