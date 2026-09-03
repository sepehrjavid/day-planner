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
event stream for this run. reply_text (A3.2) is the model's concatenated
visible text for the run — most invariants ignore it, but a few (e.g.
connect_url_handed_to_user) check for a specific, deterministic string a
tool actually returned, which is checking a fact the tool asserted, not
phrasing — consistent with A3.1's wording ban, not an exception to it.

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
    # A4.3 (repeat-bump cut): pre-seeded, previously-placed sessions —
    # static "given" state, like calendar_events, not what the model
    # placed this trial. Lets an invariant re-derive which slots are
    # genuinely repeatedly-bumped the same way compute_habit_review does,
    # instead of hardcoding a scenario-specific slot.
    habit_sessions: list[dict] = field(default_factory=list)

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
    world: World, placed_events: list[dict], tool_calls: list[dict], reply_text: str = ""
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
    world: World, placed_events: list[dict], tool_calls: list[dict], reply_text: str = ""
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
    world: World, placed_events: list[dict], tool_calls: list[dict], reply_text: str = ""
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
    world: World, placed_events: list[dict], tool_calls: list[dict], reply_text: str = ""
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
    world: World, placed_events: list[dict], tool_calls: list[dict], reply_text: str = ""
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
    world: World, placed_events: list[dict], tool_calls: list[dict], reply_text: str = ""
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


def no_session_overlaps_existing_events(
    world: World, placed_events: list[dict], tool_calls: list[dict], reply_text: str = ""
) -> InvariantResult:
    """A3.5's "conflicting event" perturbation: a pre-existing event
    placed at what would otherwise be the obvious slot must actually be
    avoided. "Look at what's already committed each day" (instruction.md's
    placement paragraph) is baseline competence rather than a
    zone/sleep-specific guardrail, but nothing in the tier-1 library
    checked it before this — the other invariants only compare against
    zones/sleep, never against ordinary existing calendar events."""
    violations = []
    for event, habit_id in _habit_tagged_events(placed_events):
        start = _parse_wall_clock(event["start"]["dateTime"])
        end = _parse_wall_clock(event["end"]["dateTime"])
        for existing in world.calendar_events:
            e_start = existing.get("start", {}).get("dateTime")
            e_end = existing.get("end", {}).get("dateTime")
            if not e_start or not e_end:
                continue  # an all-day event (date, not dateTime) isn't a timed conflict
            es = _parse_wall_clock(e_start)
            ee = _parse_wall_clock(e_end)
            if start < ee and end > es:
                violations.append(
                    f"{event.get('summary')!r} at {event['start']['dateTime']} overlaps "
                    f"existing event {existing.get('summary')!r}"
                )
    return InvariantResult(not violations, "; ".join(violations))


_AFTER_8PM = _time_of("20:00")


def no_physical_session_after_8pm(
    world: World, placed_events: list[dict], tool_calls: list[dict], reply_text: str = ""
) -> InvariantResult:
    """A3.5's "no physical activity after 8pm" perturbation — a profile
    preference (instruction.md's own example of a blackout window) must
    rule out evening candidate times outright, the same as a zone would.
    Fixed at 20:00 rather than scenario-configurable — this checks one
    specific, literal instruction.md example, not a general cutoff
    mechanism."""
    violations = [
        f"{event.get('summary')!r} placed at {event['start']['dateTime']}, at/after 8pm"
        for event, _habit_id in _habit_tagged_events(placed_events)
        if _parse_wall_clock(event["start"]["dateTime"]).time() >= _AFTER_8PM
    ]
    return InvariantResult(not violations, "; ".join(violations))


def no_events_actually_placed(
    world: World, placed_events: list[dict], tool_calls: list[dict], reply_text: str = ""
) -> InvariantResult:
    """A3.2's own three failure-mode scenarios (zone fetch failing,
    needs_auth, a read-only calendar) all cash out to the same correct
    behaviour: nothing actually gets written to the calendar. Checking
    tool-call counts alone isn't enough for the not_writable case —
    add_calendar_event still gets *called*, it just comes back with
    status "not_writable"; what must not happen is a successful insert,
    which is exactly what placed_events reflects (see conftest.py's
    FakeCalendarService.placed_events)."""
    if placed_events:
        summaries = ", ".join(repr(e.get("summary")) for e in placed_events)
        return InvariantResult(False, f"expected nothing placed, got: {summaries}")
    return InvariantResult(True)


# Fixed strings the fixture's fakes actually produce (see conftest.py's
# ScenarioFixture.list_calendars and FakeCalendarListResource) — shared
# so the invariant and the fixture that must match it can't silently
# drift apart. Not scenario-configurable: these are two specific
# failure-mode fixtures, not a general "assert this string" mechanism.
NEEDS_AUTH_CONNECT_URL = "https://connect.example/start"
READ_ONLY_CALENDAR_SUMMARY = "Personal"


def connect_url_handed_to_user(
    world: World, placed_events: list[dict], tool_calls: list[dict], reply_text: str = ""
) -> InvariantResult:
    """A3.2's calendar_needs_auth scenario: "hands over the connect_url
    and stops" has two halves, and no_events_actually_placed only covers
    the second one. This checks the first — the specific URL string
    backend_client.NeedsAuth carries must actually reach the user, not
    just that nothing got written. Checking for this exact string (not
    phrasing) stays within A3.1's "never assert on wording" rule."""
    if NEEDS_AUTH_CONNECT_URL in reply_text:
        return InvariantResult(True)
    return InvariantResult(
        False, f"connect_url {NEEDS_AUTH_CONNECT_URL!r} not found in reply: {reply_text!r}"
    )


def reply_reports_readonly_calendar(
    world: World, placed_events: list[dict], tool_calls: list[dict], reply_text: str = ""
) -> InvariantResult:
    """A3.2's calendar_not_writable scenario: "reports a read-only
    calendar" means the user actually learns which calendar and why, not
    just that nothing got placed. Checks for the calendar's own summary
    string (what add_calendar_event's not_writable message names), the
    same entity-matching idea A3.6 will later formalise more generally —
    a fixed, deterministic string a tool returned, not phrasing."""
    if READ_ONLY_CALENDAR_SUMMARY in reply_text:
        return InvariantResult(True)
    return InvariantResult(
        False,
        f"calendar summary {READ_ONLY_CALENDAR_SUMMARY!r} not found in reply: {reply_text!r}",
    )


def no_silent_edit_of_pre_existing_habit_session(
    world: World, placed_events: list[dict], tool_calls: list[dict], reply_text: str = ""
) -> InvariantResult:
    """instruction.md's "conflict you create by learning something new"
    paragraph (A4.3, find_zone_collisions): discovering that a new or
    changed zone collides with a session already on the calendar is never
    license to move or delete it on the spot — "ask the user how to
    resolve it ... rather than leaving the collision in place unmentioned
    or silently moving anything without asking first." Checks that no
    update_calendar_event or delete_calendar_event call in this trial
    targets one of the scenario's own pre-existing, habit-tagged events
    (given.calendar_events) — a single turn that only describes a new
    constraint is never itself an explicit instruction to touch that
    session."""
    pre_existing_ids = {
        event["id"]
        for event in world.calendar_events
        if "id" in event and _habit_id_of(event) is not None
    }
    if not pre_existing_ids:
        return InvariantResult(True, "no pre-existing habit-tagged events in this scenario")
    for call in tool_calls:
        if call["name"] not in ("update_calendar_event", "delete_calendar_event"):
            continue
        touched = call["args"].get("event_id")
        if touched in pre_existing_ids:
            return InvariantResult(
                False,
                f"{call['name']} silently touched pre-existing event {touched!r} "
                f"with no explicit user instruction to",
            )
    return InvariantResult(True)


TIER1_INVARIANTS = {
    "no_session_overlaps_any_zone": no_session_overlaps_any_zone,
    "no_session_overlaps_sleep_or_cooldown": no_session_overlaps_sleep_or_cooldown,
    "every_habit_session_passes_habit_id": every_habit_session_passes_habit_id,
    "no_habit_id_on_plain_appointment": no_habit_id_on_plain_appointment,
    "placed_minutes_meets_target": placed_minutes_meets_target,
    "zone_anchored_sessions_match_zone_times": zone_anchored_sessions_match_zone_times,
    "no_events_actually_placed": no_events_actually_placed,
    "connect_url_handed_to_user": connect_url_handed_to_user,
    "reply_reports_readonly_calendar": reply_reports_readonly_calendar,
    "no_session_overlaps_existing_events": no_session_overlaps_existing_events,
    "no_physical_session_after_8pm": no_physical_session_after_8pm,
    "no_silent_edit_of_pre_existing_habit_session": no_silent_edit_of_pre_existing_habit_session,
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
    world: World, placed_events: list[dict], tool_calls: list[dict], reply_text: str = ""
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
    world: World, placed_events: list[dict], tool_calls: list[dict], reply_text: str = ""
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
    world: World, placed_events: list[dict], tool_calls: list[dict], reply_text: str = ""
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


# ---------------------------------------------------------------------------
# A3.6 — explanation consistency and process invariants (tier 2).
#
# Constraint invariants (tier 1) check *what* the agent did to the
# calendar; these check *how it got there* — whether its stated reasons
# are actually true of the fixture, and whether the reads its decision
# should rest on actually happened before the write. Both are
# deliberately structural, not semantic: entity-string matching against
# known fixture values and tool-call-trace ordering, never an LLM judging
# whether an explanation "sounds right" — that stays out of scope per
# A3.6's own note, and belongs in tier 3 if it's ever built at all.
# ---------------------------------------------------------------------------

# Common words that can immediately precede "zone"/"habit" in ordinary
# English without naming one — capitalized only at a sentence boundary,
# not because they're a fixture entity. A citation regex this simple
# can't tell the two apart any other way; this is a known, accepted
# false-negative source (a real zone label that happens to collide with
# one of these would be missed) rather than one worth a heavier parser.
_GENERIC_ENTITY_PREFIXES = {
    "This", "That", "The", "A", "An", "Any", "No", "Every", "Each",
    "Your", "Its", "New", "Time", "Some", "One", "Another",
}


def _entity_citations(reply_text: str, real_labels: set[str], suffix: str) -> list[str]:
    """Find "<label> {suffix}" citations in reply_text, preferring the
    longest real label that matches immediately before the suffix word.
    Labels aren't always one word ("Deep Work"), so a naive single-word
    capture right before "zone"/"habit" would grab only "Work" out of
    "your Deep Work zone" — a real, existing zone — and wrongly flag it
    as fabricated. Real multi-word citations are matched and excluded
    first; only a citation no real label accounts for falls through to
    the single-capitalized-word heuristic that actually flags something."""
    known_spans: list[tuple[int, int]] = []
    if real_labels:
        alternation = "|".join(re.escape(label) for label in sorted(real_labels, key=len, reverse=True))
        known_re = re.compile(rf"\b(?:{alternation})\s+{suffix}\b")
        known_spans = [m.span() for m in known_re.finditer(reply_text)]

    def _covered(pos: int) -> bool:
        return any(start <= pos < end for start, end in known_spans)

    generic_re = re.compile(rf"\b([A-Z][a-zA-Z]*)\s+{suffix}\b")
    citations = []
    for match in generic_re.finditer(reply_text):
        if _covered(match.start()):
            continue
        word = match.group(1)
        if word in _GENERIC_ENTITY_PREFIXES:
            continue
        citations.append(word)
    return citations


def explanation_cites_real_entities(
    world: World, placed_events: list[dict], tool_calls: list[dict], reply_text: str = ""
) -> InvariantResult:
    """instruction.md requires the agent to explain why it placed each
    session — this checks that the explanation's named entities are real,
    not that the explanation is well-formed or persuasive. Scans
    reply_text for "<label> zone" / "<label> habit" citations and flags
    any that isn't an actual zone or habit label in this fixture (nor
    "cool-down"/"wake-up", the two sleep-derived windows that behave like
    zones — see instruction.md's placement paragraph). A cheap,
    deterministic catch for blatant confabulation — "I avoided your
    Evening zone" when no such zone exists — not a semantic check of
    whether the cited entity actually covers the slot in question."""
    real_zone_labels = {z["label"] for z in world.zones} | {"cool-down", "wake-up"}
    real_habit_labels = {h["label"] for h in world.habits}

    violations = []
    for word in _entity_citations(reply_text, real_zone_labels, "zone"):
        violations.append(f"reply cites {word!r} zone, which does not exist in this fixture")
    for word in _entity_citations(reply_text, real_habit_labels, "habit"):
        violations.append(f"reply cites {word!r} habit, which does not exist in this fixture")
    return InvariantResult(not violations, "; ".join(violations))


def _habit_tagged_add_indices(tool_calls: list[dict]) -> list[int]:
    return [
        i
        for i, c in enumerate(tool_calls)
        if c["name"] == "add_calendar_event" and c["args"].get("habit_id")
    ]


def calendar_checked_before_habit_placement(
    world: World, placed_events: list[dict], tool_calls: list[dict], reply_text: str = ""
) -> InvariantResult:
    """instruction.md's placement paragraph: "call get_calendar_events for
    that period first, look at what's already committed each day" —
    before any of it is placed. Checks a get_calendar_events call appears
    earlier in the trace than the first habit-tagged add_calendar_event,
    with a [date_from, date_to) range that fully covers every date a
    habit session actually landed on — not merely that the tool was
    called at all.

    A get_available_slots call covering the same range counts equally
    (A4.3): scheduling_tool.py's _compute_candidates forwards that call's
    own date_from/date_to straight into its own get_calendar_events call
    (verified by reading the source, not assumed) — the calendar check
    still happens, just inside the tool rather than as a separately
    visible call. Accepting only a direct get_calendar_events call here
    would fail a trial that did exactly what instruction.md's new
    get_available_slots paragraph tells it to prefer."""
    add_indices = _habit_tagged_add_indices(tool_calls)
    if not add_indices:
        return InvariantResult(True, "no habit-tagged placement in this trial — not applicable")
    first_add_index = min(add_indices)

    placed_dates = []
    for i in add_indices:
        start_time = tool_calls[i]["args"].get("start_time")
        if start_time:
            placed_dates.append(_parse_wall_clock(start_time).date())
    if not placed_dates:
        return InvariantResult(True, "no start_time on any habit-tagged add_calendar_event call")
    period_start, period_end = min(placed_dates), max(placed_dates)

    for call in tool_calls[:first_add_index]:
        if call["name"] not in ("get_calendar_events", "get_available_slots"):
            continue
        try:
            date_from = date.fromisoformat(call["args"]["date_from"])
            date_to = date.fromisoformat(call["args"]["date_to"])
        except (KeyError, ValueError):
            continue
        if date_from <= period_start and date_to > period_end:
            return InvariantResult(True)
    return InvariantResult(
        False,
        f"no get_calendar_events/get_available_slots call before the first habit "
        f"placement (index {first_add_index}) covers the placed period "
        f"{period_start}..{period_end}",
    )


def list_habits_precedes_placement(
    world: World, placed_events: list[dict], tool_calls: list[dict], reply_text: str = ""
) -> InvariantResult:
    """instruction.md: "call list_habits explicitly whenever you're
    deciding what to schedule, rather than assuming get_profile already
    covers them" — habits aren't preloaded, so relying on the preloaded
    profile instead of calling this tool is exactly the mistake this rule
    exists to catch."""
    add_indices = _habit_tagged_add_indices(tool_calls)
    if not add_indices:
        return InvariantResult(True, "no habit-tagged placement in this trial — not applicable")
    first_add_index = min(add_indices)
    if any(c["name"] == "list_habits" for c in tool_calls[:first_add_index]):
        return InvariantResult(True)
    return InvariantResult(
        False,
        f"list_habits not called before the first habit placement (index {first_add_index})",
    )


def review_habit_week_precedes_replan(
    world: World, placed_events: list[dict], tool_calls: list[dict], reply_text: str = ""
) -> InvariantResult:
    """instruction.md: call review_habit_week for the period immediately
    preceding the one about to be planned, for any habit that already has
    prior sessions — "every time, not only when you already suspect it
    went badly." "Prior sessions" here means calendar_events (the
    fixture's static given state) tagged with a habit_id and dated before
    `today`. Checks the call precedes the first new placement for that
    habit and covers a period ending at or before today — "genuinely
    preceding," not merely called at some point.

    A get_available_slots call counts equally (A4.3), but only if its own
    date_from is at or before today — not merely that it was called.
    scheduling_tool.py's _compute_candidates reviews exactly
    [date_from - (date_to - date_from), date_from) internally (verified
    by reading the source): the reviewed period's own end is that call's
    date_from, so checking date_from <= today here is the equivalent of
    checking review_habit_week's date_to <= today above — the "genuinely
    preceding" assertion moves inside the tool call's own arguments
    rather than being dropped."""
    if not world.today:
        return InvariantResult(True, "no `today` on this scenario — not applicable")
    today = date.fromisoformat(world.today)

    prior_habit_ids = set()
    for event in world.calendar_events:
        habit_id = _habit_id_of(event)
        start = event.get("start", {}).get("dateTime")
        if habit_id and start and _parse_wall_clock(start).date() < today:
            prior_habit_ids.add(habit_id)
    if not prior_habit_ids:
        return InvariantResult(True, "no habit in this fixture has prior sessions — not applicable")

    replanned_habit_ids = prior_habit_ids & {hid for _, hid in _habit_tagged_events(placed_events)}
    if not replanned_habit_ids:
        return InvariantResult(True, "no habit with prior sessions was replanned this trial")

    relevant_add_indices = [
        i for i in _habit_tagged_add_indices(tool_calls)
        if tool_calls[i]["args"].get("habit_id") in replanned_habit_ids
    ]
    first_add_index = min(relevant_add_indices)

    for call in tool_calls[:first_add_index]:
        if call["name"] == "review_habit_week":
            try:
                date_to = date.fromisoformat(call["args"]["date_to"])
            except (KeyError, ValueError):
                continue
            if date_to <= today:
                return InvariantResult(True)
        elif call["name"] == "get_available_slots":
            try:
                date_from = date.fromisoformat(call["args"]["date_from"])
            except (KeyError, ValueError):
                continue
            if date_from <= today:
                return InvariantResult(True)
    return InvariantResult(
        False,
        f"no review_habit_week call for a period ending by {today}, nor a "
        f"get_available_slots call starting by {today}, found before the "
        f"first replacement of {replanned_habit_ids} (index {first_add_index})",
    )


def _overlaps_window(start: datetime, end: datetime, other_start: datetime, other_end: datetime) -> bool:
    return start < other_end and other_start < end


def _bumped_by(
    start: datetime, end: datetime, *, exclude_id: str, calendar_events: list[dict]
) -> str | None:
    """Mirrors habit_tools.py's _find_conflict: the title of whatever
    else on the same calendar overlaps [start, end), excluding the
    session's own event."""
    for other in calendar_events:
        if other.get("id") == exclude_id:
            continue
        try:
            other_start = _parse_wall_clock(other["start"]["dateTime"])
            other_end = _parse_wall_clock(other["end"]["dateTime"])
        except (KeyError, ValueError):
            continue
        if _overlaps_window(start, end, other_start, other_end):
            return other.get("summary")
    return None


def _same_wall_clock_instant(a: str, b: str) -> bool:
    try:
        return _parse_wall_clock(a) == _parse_wall_clock(b)
    except (ValueError, TypeError):
        return a == b


def avoids_repeatedly_bumped_slot(
    world: World, placed_events: list[dict], tool_calls: list[dict], reply_text: str = ""
) -> InvariantResult:
    """instruction.md's placement paragraph (repeat-bump rule, A4.3): if
    the prior review shows the same time slot bumped more than once by
    the same unrelated conflict, treat it as weaker and prefer a
    comparably good alternative. A soft tie-breaker, not a hard
    guardrail — this only checks that a newly-placed session for the
    affected habit doesn't land on the exact repeatedly-bumped
    weekday+time, not that the target went unmet or that every
    alternative was avoided too.

    Derives which slot(s) actually qualify as repeatedly-bumped from
    world.habit_sessions (the prior, previously-placed sessions) and
    world.calendar_events (the calendar's current state) directly,
    mirroring habit_tools.py's compute_habit_review diff (kept/moved/
    gone, then _find_conflict for bumped_by) rather than trusting the
    scenario author's own intent — stays correct if the fixture
    changes. _REPEAT_BUMP_THRESHOLD matches scheduling/scoring.py's own
    constant of the same name: 2, "more than once."""
    events_by_id = {e["id"]: e for e in world.calendar_events if "id" in e}
    counts: dict[tuple[str, tuple[str, str], str], int] = {}
    for session in world.habit_sessions:
        current = events_by_id.get(session["event_id"])
        planned_start = _parse_wall_clock(session["planned_start"])
        if current is not None and _same_wall_clock_instant(
            current["start"]["dateTime"], session["planned_start"]
        ):
            continue  # "kept" — nothing to bump

        planned_end = _parse_wall_clock(session["planned_end"])
        bumped_by = _bumped_by(
            planned_start, planned_end,
            exclude_id=session["event_id"], calendar_events=world.calendar_events,
        )
        if not bumped_by:
            continue

        key = (session["habit_id"], (_day_key(planned_start.date()), planned_start.strftime("%H:%M")), bumped_by)
        counts[key] = counts.get(key, 0) + 1

    weak_slots = {(habit_id, slot) for (habit_id, slot, _bumper), n in counts.items() if n >= 2}
    if not weak_slots:
        return InvariantResult(True, "no slot in this fixture qualifies as repeatedly-bumped")

    violations = []
    for event, habit_id in _habit_tagged_events(placed_events):
        start = _parse_wall_clock(event["start"]["dateTime"])
        slot = (_day_key(start.date()), start.strftime("%H:%M"))
        if (habit_id, slot) in weak_slots:
            violations.append(
                f"{event.get('summary')!r} placed at {event['start']['dateTime']} — "
                f"weekday+time {slot} was repeatedly bumped for this habit"
            )
    return InvariantResult(not violations, "; ".join(violations))


TIER2_INVARIANTS = {
    "heavier_load_on_lighter_days": heavier_load_on_lighter_days,
    "weekend_preferred_when_weekend_is_free": weekend_preferred_when_weekend_is_free,
    "chosen_slot_ranks_above_median": chosen_slot_ranks_above_median,
    "explanation_cites_real_entities": explanation_cites_real_entities,
    "calendar_checked_before_habit_placement": calendar_checked_before_habit_placement,
    "list_habits_precedes_placement": list_habits_precedes_placement,
    "review_habit_week_precedes_replan": review_habit_week_precedes_replan,
    "avoids_repeatedly_bumped_slot": avoids_repeatedly_bumped_slot,
}

ALL_INVARIANTS = {**TIER1_INVARIANTS, **TIER2_INVARIANTS}
