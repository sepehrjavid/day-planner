"""Tier-1 (constraint) invariant library for A3.1's eval scenarios.

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
state after each trial, not from the Scenario object.

placed_events is the raw Google Calendar item shape (see conftest.py's
FakeEventsResource) reflecting everything add_calendar_event actually
inserted during the run — not what the model *said* it would do, per
A3.1's "assert on tool calls and arguments, never on wording." tool_calls
is the ordered list of {"name", "args"} dicts collected from the ADK
event stream for this run.

These only cover tier 1 ("did it break a rule") — tier 2 ("did it choose
sensibly") needs day-load/candidate-slot computation this module
deliberately doesn't build; see A3.1's own scope note on
chosen_slot_ranks_above_median needing A4.1's scorer.
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
