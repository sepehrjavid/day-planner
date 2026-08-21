"""Unit coverage of evals/invariants.py's tier-1 and tier-2 predicates —
pure functions over fixture data, no model calls, so these run in the
normal fast pytest suite even though the eval scenarios themselves
(evals/runner.py) need a real model and are invoked separately.
"""

from day_planner_agent.evals import invariants as inv


def _world(
    *, zones=None, sleep_schedule=None, habits=None, calendar_events=None, today=None
) -> inv.World:
    return inv.World(
        zones=zones or [],
        sleep_schedule=sleep_schedule,
        habits=habits or [],
        calendar_events=calendar_events or [],
        today=today,
    )


def _event(summary, start, end, habit_id=None):
    item = {"summary": summary, "start": {"dateTime": start}, "end": {"dateTime": end}}
    if habit_id:
        item["extendedProperties"] = {"private": {"day_planner_habit_id": habit_id}}
    return item


WORK_ZONE = {
    "zone_id": "z1",
    "label": "Work",
    "start_time": "09:00",
    "end_time": "17:30",
    "days_of_week": ["mon", "tue", "wed", "thu", "fri"],
}
GYM_HABIT = {
    "habit_id": "h1",
    "label": "Gym",
    "goal": "180 min/week, sessions 30-60 minutes",
    "status": "active",
    "allowed_zones": [],
}
SLEEP = {
    "sleep_time": "23:00",
    "wake_time": "07:00",
    "cool_down_minutes": 30,
    "wake_up_buffer_minutes": 15,
}


# ---------------------------------------------------------------------------
# no_session_overlaps_any_zone
# ---------------------------------------------------------------------------


def test_zone_overlap_detected():
    scenario = _world(zones=[WORK_ZONE], habits=[GYM_HABIT])
    # 2026-08-24 is a Monday.
    events = [_event("Gym", "2026-08-24T10:00:00", "2026-08-24T10:30:00", habit_id="h1")]
    result = inv.no_session_overlaps_any_zone(scenario, events, [])
    assert result.passed is False
    assert "Work" in result.detail


def test_zone_no_overlap_when_outside_hours():
    scenario = _world(zones=[WORK_ZONE], habits=[GYM_HABIT])
    events = [_event("Gym", "2026-08-24T18:00:00", "2026-08-24T18:30:00", habit_id="h1")]
    assert inv.no_session_overlaps_any_zone(scenario, events, []).passed is True


def test_zone_no_overlap_on_a_day_not_in_days_of_week():
    scenario = _world(zones=[WORK_ZONE], habits=[GYM_HABIT])
    # 2026-08-29 is a Saturday — Work zone doesn't apply.
    events = [_event("Gym", "2026-08-29T10:00:00", "2026-08-29T10:30:00", habit_id="h1")]
    assert inv.no_session_overlaps_any_zone(scenario, events, [])
    assert inv.no_session_overlaps_any_zone(scenario, events, []).passed is True


def test_zone_ignores_plain_appointments():
    scenario = _world(zones=[WORK_ZONE], habits=[GYM_HABIT])
    events = [_event("Dentist", "2026-08-24T10:00:00", "2026-08-24T10:30:00")]
    assert inv.no_session_overlaps_any_zone(scenario, events, []).passed is True


def test_zone_override_via_allowed_zones():
    habit = dict(GYM_HABIT, allowed_zones=["Work"])
    scenario = _world(zones=[WORK_ZONE], habits=[habit])
    events = [_event("Gym", "2026-08-24T10:00:00", "2026-08-24T10:30:00", habit_id="h1")]
    assert inv.no_session_overlaps_any_zone(scenario, events, []).passed is True


def test_zone_boundary_is_half_open():
    """A session starting exactly at the zone's own end_time is outside
    it — [start, end) is deliberately exclusive on the right, matching
    instruction.md's own [start_time, end_time) framing."""
    scenario = _world(zones=[WORK_ZONE], habits=[GYM_HABIT])
    events = [_event("Gym", "2026-08-24T17:30:00", "2026-08-24T18:00:00", habit_id="h1")]
    assert inv.no_session_overlaps_any_zone(scenario, events, []).passed is True


# ---------------------------------------------------------------------------
# no_session_overlaps_sleep_or_cooldown
# ---------------------------------------------------------------------------


def test_sleep_overlap_detected():
    scenario = _world(sleep_schedule=SLEEP, habits=[GYM_HABIT])
    events = [_event("Gym", "2026-08-24T23:30:00", "2026-08-25T00:00:00", habit_id="h1")]
    result = inv.no_session_overlaps_sleep_or_cooldown(scenario, events, [])
    assert result.passed is False
    assert "sleep" in result.detail


def test_cooldown_overlap_detected():
    scenario = _world(sleep_schedule=SLEEP, habits=[GYM_HABIT])
    # cool_down starts 22:30 (30 min before 23:00 sleep_time).
    events = [_event("Gym", "2026-08-24T22:45:00", "2026-08-24T23:00:00", habit_id="h1")]
    result = inv.no_session_overlaps_sleep_or_cooldown(scenario, events, [])
    assert result.passed is False
    assert "cool-down" in result.detail


def test_wake_buffer_overlap_detected():
    scenario = _world(sleep_schedule=SLEEP, habits=[GYM_HABIT])
    # wake_up_buffer 07:00-07:15.
    events = [_event("Gym", "2026-08-24T07:05:00", "2026-08-24T07:20:00", habit_id="h1")]
    result = inv.no_session_overlaps_sleep_or_cooldown(scenario, events, [])
    assert result.passed is False
    assert "wake-up" in result.detail


def test_sleep_window_not_overridable_even_with_allowed_zones():
    habit = dict(GYM_HABIT, allowed_zones=["cool-down", "wake-up"])
    scenario = _world(sleep_schedule=SLEEP, habits=[habit])
    events = [_event("Gym", "2026-08-24T23:30:00", "2026-08-25T00:00:00", habit_id="h1")]
    assert inv.no_session_overlaps_sleep_or_cooldown(scenario, events, []).passed is False


def test_cooldown_override_via_allowed_zones():
    habit = dict(GYM_HABIT, allowed_zones=["cool-down"])
    scenario = _world(sleep_schedule=SLEEP, habits=[habit])
    events = [_event("Gym", "2026-08-24T22:45:00", "2026-08-24T23:00:00", habit_id="h1")]
    assert inv.no_session_overlaps_sleep_or_cooldown(scenario, events, []).passed is True


def test_sleep_ok_during_the_day():
    scenario = _world(sleep_schedule=SLEEP, habits=[GYM_HABIT])
    events = [_event("Gym", "2026-08-24T18:00:00", "2026-08-24T18:30:00", habit_id="h1")]
    assert inv.no_session_overlaps_sleep_or_cooldown(scenario, events, []).passed is True


def test_no_sleep_schedule_in_scenario_passes_trivially():
    scenario = _world(sleep_schedule=None, habits=[GYM_HABIT])
    events = [_event("Gym", "2026-08-24T23:30:00", "2026-08-25T00:00:00", habit_id="h1")]
    assert inv.no_session_overlaps_sleep_or_cooldown(scenario, events, []).passed is True


def test_day_override_sleep_time_is_used_on_that_day():
    """Sleeping in on Sundays: wake_time overridden to 09:00 means a
    session right after that (09:15, past the 15min wake-up buffer) is
    fine on that specific day, even though 09:15 would be well inside a
    normal weekday's already-woken hours regardless — the point is the
    override is actually being read, not that this time is special."""
    schedule = dict(SLEEP, day_overrides={"sun": {"sleep_time": "01:00", "wake_time": "09:00"}})
    scenario = _world(sleep_schedule=schedule, habits=[GYM_HABIT])
    # 2026-08-23 is a Sunday.
    events = [_event("Gym", "2026-08-23T09:15:00", "2026-08-23T09:45:00", habit_id="h1")]
    assert inv.no_session_overlaps_sleep_or_cooldown(scenario, events, []).passed is True


def test_day_override_still_catches_a_violation_on_that_day():
    """08:30 falls inside the overridden [01:00, 09:00) sleep window —
    on a normal day (default wake_time 07:00) it would be a perfectly
    fine, already-awake time, so this also confirms the override
    actually shifts the window rather than being ignored."""
    schedule = dict(SLEEP, day_overrides={"sun": {"sleep_time": "01:00", "wake_time": "09:00"}})
    scenario = _world(sleep_schedule=schedule, habits=[GYM_HABIT])
    events = [_event("Gym", "2026-08-23T08:30:00", "2026-08-23T09:00:00", habit_id="h1")]
    result = inv.no_session_overlaps_sleep_or_cooldown(scenario, events, [])
    assert result.passed is False
    assert "sleep" in result.detail


def test_day_override_does_not_affect_other_days():
    schedule = dict(SLEEP, day_overrides={"sun": {"sleep_time": "01:00", "wake_time": "09:00"}})
    scenario = _world(sleep_schedule=schedule, habits=[GYM_HABIT])
    # Monday still uses the default 07:00 wake_time + 15min buffer.
    events = [_event("Gym", "2026-08-24T07:05:00", "2026-08-24T07:20:00", habit_id="h1")]
    result = inv.no_session_overlaps_sleep_or_cooldown(scenario, events, [])
    assert result.passed is False


# ---------------------------------------------------------------------------
# every_habit_session_passes_habit_id / no_habit_id_on_plain_appointment
# ---------------------------------------------------------------------------


def test_every_habit_session_passes_habit_id_fails_when_missing():
    scenario = _world(habits=[GYM_HABIT])
    calls = [{"name": "add_calendar_event", "args": {"summary": "Gym"}}]
    result = inv.every_habit_session_passes_habit_id(scenario, [], calls)
    assert result.passed is False


def test_every_habit_session_passes_habit_id_fails_on_unknown_id():
    scenario = _world(habits=[GYM_HABIT])
    calls = [{"name": "add_calendar_event", "args": {"summary": "Gym", "habit_id": "nonexistent"}}]
    assert inv.every_habit_session_passes_habit_id(scenario, [], calls).passed is False


def test_every_habit_session_passes_habit_id_passes():
    scenario = _world(habits=[GYM_HABIT])
    calls = [{"name": "add_calendar_event", "args": {"summary": "Gym", "habit_id": "h1"}}]
    assert inv.every_habit_session_passes_habit_id(scenario, [], calls).passed is True


def test_every_habit_session_passes_habit_id_ignores_other_tools():
    scenario = _world(habits=[GYM_HABIT])
    calls = [{"name": "get_calendar_events", "args": {}}]
    assert inv.every_habit_session_passes_habit_id(scenario, [], calls).passed is True


def test_no_habit_id_on_plain_appointment_fails_when_tagged():
    scenario = _world()
    calls = [{"name": "add_calendar_event", "args": {"summary": "Dinner", "habit_id": "h1"}}]
    assert inv.no_habit_id_on_plain_appointment(scenario, [], calls).passed is False


def test_no_habit_id_on_plain_appointment_passes_when_untagged():
    scenario = _world()
    calls = [{"name": "add_calendar_event", "args": {"summary": "Dinner"}}]
    assert inv.no_habit_id_on_plain_appointment(scenario, [], calls).passed is True


# ---------------------------------------------------------------------------
# placed_minutes_meets_target
# ---------------------------------------------------------------------------


def test_placed_minutes_meets_target_passes_when_total_reached():
    scenario = _world(habits=[GYM_HABIT])
    events = [
        _event("Gym", "2026-08-24T18:00:00", "2026-08-24T19:00:00", habit_id="h1"),
        _event("Gym", "2026-08-26T18:00:00", "2026-08-26T19:30:00", habit_id="h1"),
    ]  # 60 + 90 = 150 min, still under 180
    assert inv.placed_minutes_meets_target(scenario, events, []).passed is False

    events.append(_event("Gym", "2026-08-28T18:00:00", "2026-08-28T18:30:00", habit_id="h1"))
    # + 30 = 180, meets target exactly
    assert inv.placed_minutes_meets_target(scenario, events, []).passed is True


def test_placed_minutes_meets_target_ignores_habits_without_a_parseable_target():
    zone_anchored = {
        "habit_id": "h2",
        "label": "Audiobook",
        "goal": "whenever I have my Commute zone",
        "status": "active",
        "allowed_zones": ["Commute"],
    }
    scenario = _world(habits=[zone_anchored])
    assert inv.placed_minutes_meets_target(scenario, [], []).passed is True


# ---------------------------------------------------------------------------
# zone_anchored_sessions_match_zone_times
# ---------------------------------------------------------------------------


COMMUTE_ZONE = {
    "zone_id": "z2",
    "label": "Commute",
    "start_time": "08:00",
    "end_time": "08:30",
    "days_of_week": ["mon", "tue", "wed", "thu", "fri"],
}
AUDIOBOOK_HABIT = {
    "habit_id": "h2",
    "label": "Audiobook",
    "goal": "whenever I have my Commute zone",
    "status": "active",
    "allowed_zones": ["Commute"],
}


def test_zone_anchored_matches_zone_time_passes():
    scenario = _world(zones=[COMMUTE_ZONE], habits=[AUDIOBOOK_HABIT])
    events = [_event("Audiobook", "2026-08-24T08:00:00", "2026-08-24T08:30:00", habit_id="h2")]
    assert inv.zone_anchored_sessions_match_zone_times(scenario, events, []).passed is True


def test_zone_anchored_wrong_time_fails():
    scenario = _world(zones=[COMMUTE_ZONE], habits=[AUDIOBOOK_HABIT])
    events = [_event("Audiobook", "2026-08-24T09:00:00", "2026-08-24T09:30:00", habit_id="h2")]
    assert inv.zone_anchored_sessions_match_zone_times(scenario, events, []).passed is False


def test_zone_anchored_skips_habits_with_a_frequency_target():
    scenario = _world(zones=[COMMUTE_ZONE], habits=[GYM_HABIT])
    events = [_event("Gym", "2026-08-24T09:00:00", "2026-08-24T09:30:00", habit_id="h1")]
    # Gym has no allowed_zones naming Commute, so this invariant has
    # nothing to check for it and must not flag it.
    assert inv.zone_anchored_sessions_match_zone_times(scenario, events, []).passed is True


# ---------------------------------------------------------------------------
# Tier 2 — heavier_load_on_lighter_days
# ---------------------------------------------------------------------------


def test_lighter_day_getting_a_shorter_session_fails():
    # Monday heavily booked (8h), Wednesday empty.
    calendar_events = [_event("Meetings", "2026-08-24T09:00:00", "2026-08-24T17:00:00")]
    scenario = _world(habits=[GYM_HABIT], calendar_events=calendar_events)
    placed = [
        _event("Gym", "2026-08-24T18:00:00", "2026-08-24T18:30:00", habit_id="h1"),  # heavier day, 30min
        _event("Gym", "2026-08-26T18:00:00", "2026-08-26T18:15:00", habit_id="h1"),  # lighter day, 15min
    ]
    result = inv.heavier_load_on_lighter_days(scenario, placed, [])
    assert result.passed is False


def test_lighter_day_getting_a_longer_session_passes():
    calendar_events = [_event("Meetings", "2026-08-24T09:00:00", "2026-08-24T17:00:00")]
    scenario = _world(habits=[GYM_HABIT], calendar_events=calendar_events)
    placed = [
        _event("Gym", "2026-08-24T18:00:00", "2026-08-24T18:30:00", habit_id="h1"),  # heavier, 30min
        _event("Gym", "2026-08-26T18:00:00", "2026-08-26T19:00:00", habit_id="h1"),  # lighter, 60min
    ]
    assert inv.heavier_load_on_lighter_days(scenario, placed, []).passed is True


def test_single_placement_has_nothing_to_compare_and_passes():
    scenario = _world(habits=[GYM_HABIT])
    placed = [_event("Gym", "2026-08-24T18:00:00", "2026-08-24T18:30:00", habit_id="h1")]
    assert inv.heavier_load_on_lighter_days(scenario, placed, []).passed is True


def test_different_habits_are_not_compared_against_each_other():
    calendar_events = [_event("Meetings", "2026-08-24T09:00:00", "2026-08-24T17:00:00")]
    scenario = _world(habits=[GYM_HABIT, AUDIOBOOK_HABIT], calendar_events=calendar_events)
    placed = [
        _event("Gym", "2026-08-24T18:00:00", "2026-08-24T18:15:00", habit_id="h1"),  # heavier day
        _event("Audiobook", "2026-08-26T08:00:00", "2026-08-26T09:00:00", habit_id="h2"),  # lighter day, longer
    ]
    # Different habits, so this is not "the lighter day's session was
    # shorter" for the same habit — must not be flagged.
    assert inv.heavier_load_on_lighter_days(scenario, placed, []).passed is True


# ---------------------------------------------------------------------------
# Tier 2 — weekend_preferred_when_weekend_is_free
# ---------------------------------------------------------------------------


def test_weekend_free_but_underused_fails():
    # today=2026-08-24 (Mon) -> week is Mon 24 - Sun 30; weekend = Sat 29/Sun 30.
    scenario = _world(habits=[GYM_HABIT], today="2026-08-24")
    placed = [
        _event("Gym", "2026-08-24T18:00:00", "2026-08-24T18:30:00", habit_id="h1"),
        _event("Gym", "2026-08-26T18:00:00", "2026-08-26T19:00:00", habit_id="h1"),
    ]
    result = inv.weekend_preferred_when_weekend_is_free(scenario, placed, [])
    assert result.passed is False


def test_weekend_free_and_used_at_or_above_fair_share_passes():
    scenario = _world(habits=[GYM_HABIT], today="2026-08-24")
    placed = [
        _event("Gym", "2026-08-24T18:00:00", "2026-08-24T18:30:00", habit_id="h1"),  # 30min weekday
        _event("Gym", "2026-08-29T09:00:00", "2026-08-29T10:00:00", habit_id="h1"),  # 60min Saturday
    ]
    # weekend share = 60/90 = 67%, well above the 2/7 (~29%) fair share.
    assert inv.weekend_preferred_when_weekend_is_free(scenario, placed, []).passed is True


def test_weekend_with_existing_load_is_not_applicable():
    calendar_events = [_event("Brunch", "2026-08-29T11:00:00", "2026-08-29T12:00:00")]
    scenario = _world(habits=[GYM_HABIT], today="2026-08-24", calendar_events=calendar_events)
    placed = [_event("Gym", "2026-08-24T18:00:00", "2026-08-24T18:30:00", habit_id="h1")]
    result = inv.weekend_preferred_when_weekend_is_free(scenario, placed, [])
    assert result.passed is True
    assert "doesn't apply" in result.detail or "apply" in result.detail


def test_no_today_is_not_applicable():
    scenario = _world(habits=[GYM_HABIT], today=None)
    placed = [_event("Gym", "2026-08-24T18:00:00", "2026-08-24T18:30:00", habit_id="h1")]
    assert inv.weekend_preferred_when_weekend_is_free(scenario, placed, []).passed is True


# ---------------------------------------------------------------------------
# Tier 2 — chosen_slot_ranks_above_median
# ---------------------------------------------------------------------------


def test_placement_on_the_single_heaviest_day_fails():
    # Only Monday has any load; every other day of the week is empty.
    calendar_events = [_event("Meetings", "2026-08-24T09:00:00", "2026-08-24T17:00:00")]
    scenario = _world(habits=[GYM_HABIT], today="2026-08-24", calendar_events=calendar_events)
    placed = [_event("Gym", "2026-08-24T18:00:00", "2026-08-24T18:30:00", habit_id="h1")]
    result = inv.chosen_slot_ranks_above_median(scenario, placed, [])
    assert result.passed is False


def test_placement_on_a_light_day_passes():
    calendar_events = [_event("Meetings", "2026-08-24T09:00:00", "2026-08-24T17:00:00")]
    scenario = _world(habits=[GYM_HABIT], today="2026-08-24", calendar_events=calendar_events)
    placed = [_event("Gym", "2026-08-26T18:00:00", "2026-08-26T18:30:00", habit_id="h1")]
    assert inv.chosen_slot_ranks_above_median(scenario, placed, []).passed is True


def test_no_today_is_not_applicable_for_slot_ranking():
    scenario = _world(habits=[GYM_HABIT], today=None)
    placed = [_event("Gym", "2026-08-24T18:00:00", "2026-08-24T18:30:00", habit_id="h1")]
    assert inv.chosen_slot_ranks_above_median(scenario, placed, []).passed is True


# ---------------------------------------------------------------------------
# no_events_actually_placed (A3.2)
# ---------------------------------------------------------------------------


def test_no_events_actually_placed_passes_when_nothing_placed():
    scenario = _world()
    assert inv.no_events_actually_placed(scenario, [], []).passed is True


def test_no_events_actually_placed_fails_when_something_was_placed():
    scenario = _world()
    events = [_event("Gym", "2026-08-24T18:00:00", "2026-08-24T18:30:00", habit_id="h1")]
    result = inv.no_events_actually_placed(scenario, events, [])
    assert result.passed is False
    assert "Gym" in result.detail


def test_no_events_actually_placed_ignores_tool_calls_that_didnt_insert():
    """The not_writable case: add_calendar_event still gets *called*
    (and comes back with a status the model should relay), it just must
    not have produced a successful insert — this invariant only looks at
    placed_events, not whether the tool was invoked."""
    scenario = _world()
    calls = [{"name": "add_calendar_event", "args": {"summary": "Gym", "habit_id": "h1"}}]
    assert inv.no_events_actually_placed(scenario, [], calls).passed is True


# ---------------------------------------------------------------------------
# connect_url_handed_to_user / reply_reports_readonly_calendar (A3.2 review)
# ---------------------------------------------------------------------------


def test_connect_url_handed_to_user_passes_when_url_in_reply():
    scenario = _world()
    reply = f"You'll need to reconnect that account: {inv.NEEDS_AUTH_CONNECT_URL}"
    assert inv.connect_url_handed_to_user(scenario, [], [], reply).passed is True


def test_connect_url_handed_to_user_fails_when_missing():
    scenario = _world()
    reply = "You'll need to reconnect that calendar account."
    result = inv.connect_url_handed_to_user(scenario, [], [], reply)
    assert result.passed is False
    assert inv.NEEDS_AUTH_CONNECT_URL in result.detail


def test_connect_url_handed_to_user_fails_on_empty_reply():
    scenario = _world()
    assert inv.connect_url_handed_to_user(scenario, [], [], "").passed is False


def test_reply_reports_readonly_calendar_passes_when_summary_in_reply():
    scenario = _world()
    reply = f"Your {inv.READ_ONLY_CALENDAR_SUMMARY!r} calendar is read-only, so I couldn't add it."
    assert inv.reply_reports_readonly_calendar(scenario, [], [], reply).passed is True


def test_reply_reports_readonly_calendar_fails_when_missing():
    scenario = _world()
    reply = "I couldn't add that — the calendar is read-only."
    result = inv.reply_reports_readonly_calendar(scenario, [], [], reply)
    assert result.passed is False
    assert inv.READ_ONLY_CALENDAR_SUMMARY in result.detail
