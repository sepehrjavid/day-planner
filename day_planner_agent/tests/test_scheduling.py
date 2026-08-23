"""Exhaustive coverage of the scheduling package (A4.1) — pure interval
arithmetic, goal-text parsing, and candidate scoring, with no agent, ADK,
or backend involved anywhere. Every rule tested here is transcribed
directly from instruction.md's placement paragraph; see each module's own
docstring for which sentence a given function implements.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from day_planner_agent.scheduling import (
    DayOverride,
    Interval,
    ReviewEntry,
    SleepSchedule,
    Zone,
    collisions_with,
    free_intervals,
    parse_session_length_range,
    parse_weekly_target_minutes,
    score_candidates,
    target_accounting,
    zone_occurrences,
)

TZ = ZoneInfo("America/New_York")


def _dt(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=TZ)


def _iv(y, m, d, h1, mi1, h2, mi2):
    return Interval(_dt(y, m, d, h1, mi1), _dt(y, m, d, h2, mi2))


# ---------------------------------------------------------------------------
# free_intervals
# ---------------------------------------------------------------------------


def test_free_intervals_whole_range_when_nothing_blocks():
    free = free_intervals((date(2026, 8, 3), date(2026, 8, 4)), tz=TZ)
    assert free == [Interval(_dt(2026, 8, 3), _dt(2026, 8, 4))]


def test_free_intervals_empty_range_is_empty():
    assert free_intervals((date(2026, 8, 3), date(2026, 8, 3)), tz=TZ) == []
    assert free_intervals((date(2026, 8, 4), date(2026, 8, 3)), tz=TZ) == []


def test_zone_blocks_matching_weekday():
    # 2026-08-03 is a Monday.
    work = Zone(label="Work", start_time="09:00", end_time="17:00", days_of_week=("mon",))
    free = free_intervals(
        (date(2026, 8, 3), date(2026, 8, 4)), tz=TZ, zones=[work]
    )
    assert free == [
        Interval(_dt(2026, 8, 3), _dt(2026, 8, 3, 9, 0)),
        Interval(_dt(2026, 8, 3, 17, 0), _dt(2026, 8, 4)),
    ]


def test_zone_does_not_block_non_matching_weekday():
    # 2026-08-08 is a Saturday; the zone only applies Mon-Fri.
    work = Zone(label="Work", start_time="09:00", end_time="17:00", days_of_week=("mon", "tue", "wed", "thu", "fri"))
    free = free_intervals((date(2026, 8, 8), date(2026, 8, 9)), tz=TZ, zones=[work])
    assert free == [Interval(_dt(2026, 8, 8), _dt(2026, 8, 9))]


def test_allowed_zones_overrides_a_zone():
    work = Zone(label="Work", start_time="09:00", end_time="17:00", days_of_week=("mon",))
    free = free_intervals(
        (date(2026, 8, 3), date(2026, 8, 4)), tz=TZ, zones=[work], allowed_zones=["Work"]
    )
    assert free == [Interval(_dt(2026, 8, 3), _dt(2026, 8, 4))]


def test_sleep_window_blocks_and_is_never_overridable():
    sleep = SleepSchedule(sleep_time="23:00", wake_time="07:00")
    free = free_intervals(
        (date(2026, 8, 3), date(2026, 8, 4)),
        tz=TZ,
        sleep_schedule=sleep,
        # Even naming "sleep" (not a real override label) must have no effect.
        allowed_zones=["sleep", "cool-down", "wake-up"],
    )
    # Blocked: [prior night's tail 00:00-07:00] and [23:00-midnight].
    assert free == [Interval(_dt(2026, 8, 3, 7, 0), _dt(2026, 8, 3, 23, 0))]


def test_sleep_window_crosses_midnight_correctly():
    sleep = SleepSchedule(sleep_time="23:30", wake_time="06:30")
    free = free_intervals((date(2026, 8, 3), date(2026, 8, 5)), tz=TZ, sleep_schedule=sleep)
    assert free == [
        Interval(_dt(2026, 8, 3, 6, 30), _dt(2026, 8, 3, 23, 30)),
        Interval(_dt(2026, 8, 4, 6, 30), _dt(2026, 8, 4, 23, 30)),
    ]


def test_sleep_day_override_replaces_only_that_day():
    sleep = SleepSchedule(
        sleep_time="23:00",
        wake_time="07:00",
        day_overrides={"sun": DayOverride(wake_time="10:00")},
    )
    # 2026-08-09 is a Sunday.
    free = free_intervals((date(2026, 8, 9), date(2026, 8, 10)), tz=TZ, sleep_schedule=sleep)
    assert free == [Interval(_dt(2026, 8, 9, 10, 0), _dt(2026, 8, 9, 23, 0))]


def test_sleep_day_override_is_partial_only_overridden_field_changes():
    sleep = SleepSchedule(
        sleep_time="23:00",
        wake_time="07:00",
        day_overrides={"sun": DayOverride(wake_time="10:00")},  # sleep_time not overridden
    )
    free = free_intervals((date(2026, 8, 9), date(2026, 8, 10)), tz=TZ, sleep_schedule=sleep)
    # sleep_time for Sunday still defaults to 23:00, only wake_time changed.
    assert free[0].start == _dt(2026, 8, 9, 10, 0)
    assert free[0].end == _dt(2026, 8, 9, 23, 0)


def test_cool_down_and_wake_up_block_by_default():
    sleep = SleepSchedule(
        sleep_time="23:00", wake_time="07:00", cool_down_minutes=30, wake_up_buffer_minutes=15
    )
    free = free_intervals((date(2026, 8, 3), date(2026, 8, 4)), tz=TZ, sleep_schedule=sleep)
    assert free == [Interval(_dt(2026, 8, 3, 7, 15), _dt(2026, 8, 3, 22, 30))]


def test_cool_down_override_via_allowed_zones():
    sleep = SleepSchedule(
        sleep_time="23:00", wake_time="07:00", cool_down_minutes=30, wake_up_buffer_minutes=15
    )
    free = free_intervals(
        (date(2026, 8, 3), date(2026, 8, 4)),
        tz=TZ,
        sleep_schedule=sleep,
        allowed_zones=["cool-down"],
    )
    assert free == [Interval(_dt(2026, 8, 3, 7, 15), _dt(2026, 8, 3, 23, 0))]


def test_wake_up_override_via_allowed_zones():
    sleep = SleepSchedule(
        sleep_time="23:00", wake_time="07:00", cool_down_minutes=30, wake_up_buffer_minutes=15
    )
    free = free_intervals(
        (date(2026, 8, 3), date(2026, 8, 4)),
        tz=TZ,
        sleep_schedule=sleep,
        allowed_zones=["wake-up"],
    )
    assert free == [Interval(_dt(2026, 8, 3, 7, 0), _dt(2026, 8, 3, 22, 30))]


def test_zero_length_cool_down_blocks_nothing():
    sleep = SleepSchedule(
        sleep_time="23:00", wake_time="07:00", cool_down_minutes=0, wake_up_buffer_minutes=0
    )
    free = free_intervals((date(2026, 8, 3), date(2026, 8, 4)), tz=TZ, sleep_schedule=sleep)
    assert free == [Interval(_dt(2026, 8, 3, 7, 0), _dt(2026, 8, 3, 23, 0))]


def test_zero_length_zone_blocks_nothing():
    zone = Zone(label="Weird", start_time="09:00", end_time="09:00", days_of_week=("mon",))
    free = free_intervals((date(2026, 8, 3), date(2026, 8, 4)), tz=TZ, zones=[zone])
    assert free == [Interval(_dt(2026, 8, 3), _dt(2026, 8, 4))]


def test_overlapping_zones_merge_into_one_gap():
    a = Zone(label="A", start_time="09:00", end_time="13:00", days_of_week=("mon",))
    b = Zone(label="B", start_time="11:00", end_time="15:00", days_of_week=("mon",))
    free = free_intervals((date(2026, 8, 3), date(2026, 8, 4)), tz=TZ, zones=[a, b])
    assert free == [
        Interval(_dt(2026, 8, 3), _dt(2026, 8, 3, 9, 0)),
        Interval(_dt(2026, 8, 3, 15, 0), _dt(2026, 8, 4)),
    ]


def test_busy_events_block_regardless_of_allowed_zones():
    busy = [_iv(2026, 8, 3, 10, 0, 11, 0)]
    free = free_intervals(
        (date(2026, 8, 3), date(2026, 8, 4)), tz=TZ, busy=busy, allowed_zones=["anything"]
    )
    assert free == [
        Interval(_dt(2026, 8, 3), _dt(2026, 8, 3, 10, 0)),
        Interval(_dt(2026, 8, 3, 11, 0), _dt(2026, 8, 4)),
    ]


def test_free_intervals_across_dst_spring_forward_loses_an_hour():
    # 2026-03-08 is the US spring-forward day in America/New_York (2am -> 3am).
    free = free_intervals((date(2026, 3, 8), date(2026, 3, 9)), tz=TZ)
    assert len(free) == 1
    assert free[0].duration_minutes == 23 * 60


def test_free_intervals_across_dst_fall_back_gains_an_hour():
    # 2026-11-01 is the US fall-back day in America/New_York (2am -> 1am).
    free = free_intervals((date(2026, 11, 1), date(2026, 11, 2)), tz=TZ)
    assert len(free) == 1
    assert free[0].duration_minutes == 25 * 60


def test_zone_spanning_dst_transition_keeps_correct_wall_clock_boundaries():
    # A zone 1:00-4:00 on the spring-forward day is still 1:00-4:00 by the
    # wall clock (a 3-hour span on paper), but 2:00-3:00 doesn't exist
    # that day, so only 2 real hours actually elapse.
    early = Zone(label="Early", start_time="01:00", end_time="04:00", days_of_week=("sun",))
    occurrences = zone_occurrences(early, (date(2026, 3, 8), date(2026, 3, 9)), tz=TZ)
    assert len(occurrences) == 1
    assert occurrences[0].start == _dt(2026, 3, 8, 1, 0)
    assert occurrences[0].end == _dt(2026, 3, 8, 4, 0)
    assert occurrences[0].duration_minutes == 2 * 60


# ---------------------------------------------------------------------------
# zone_occurrences
# ---------------------------------------------------------------------------


def test_zone_occurrences_matches_every_day_in_range():
    commute = Zone(
        label="Commute", start_time="08:30", end_time="09:00", days_of_week=("mon", "tue", "wed", "thu", "fri")
    )
    occurrences = zone_occurrences(commute, (date(2026, 8, 3), date(2026, 8, 10)), tz=TZ)
    assert [o.start.date() for o in occurrences] == [
        date(2026, 8, 3),
        date(2026, 8, 4),
        date(2026, 8, 5),
        date(2026, 8, 6),
        date(2026, 8, 7),
    ]


def test_zone_occurrences_empty_when_no_day_matches():
    weekend_only = Zone(label="Brunch", start_time="10:00", end_time="12:00", days_of_week=("sat", "sun"))
    occurrences = zone_occurrences(weekend_only, (date(2026, 8, 3), date(2026, 8, 7)), tz=TZ)
    assert occurrences == []


def test_zone_occurrences_handles_midnight_crossing():
    night_shift = Zone(label="Night shift", start_time="22:00", end_time="06:00", days_of_week=("mon",))
    occurrences = zone_occurrences(night_shift, (date(2026, 8, 3), date(2026, 8, 4)), tz=TZ)
    assert occurrences == [Interval(_dt(2026, 8, 3, 22, 0), _dt(2026, 8, 4, 6, 0))]


# ---------------------------------------------------------------------------
# collisions_with
# ---------------------------------------------------------------------------


def test_collisions_with_finds_overlapping_sessions():
    new_zone_occurrence = [_iv(2026, 8, 3, 9, 0, 17, 0)]
    placed = [
        ("gym-mon", _iv(2026, 8, 3, 12, 0, 13, 0)),  # inside the new zone
        ("gym-tue", _iv(2026, 8, 4, 12, 0, 13, 0)),  # different day, no overlap
    ]
    assert collisions_with(new_zone_occurrence, placed) == ["gym-mon"]


def test_collisions_with_touching_endpoints_do_not_collide():
    new_zone_occurrence = [_iv(2026, 8, 3, 9, 0, 17, 0)]
    placed = [("right-after", _iv(2026, 8, 3, 17, 0, 18, 0))]
    assert collisions_with(new_zone_occurrence, placed) == []


def test_collisions_with_matches_against_any_constraint_interval():
    occurrences = [_iv(2026, 8, 3, 9, 0, 10, 0), _iv(2026, 8, 10, 9, 0, 10, 0)]
    placed = [("second-week", _iv(2026, 8, 10, 9, 30, 10, 0))]
    assert collisions_with(occurrences, placed) == ["second-week"]


def test_collisions_with_empty_when_nothing_overlaps():
    occurrences = [_iv(2026, 8, 3, 9, 0, 10, 0)]
    placed = [("elsewhere", _iv(2026, 8, 3, 11, 0, 12, 0))]
    assert collisions_with(occurrences, placed) == []


# ---------------------------------------------------------------------------
# target_accounting / parsing
# ---------------------------------------------------------------------------


def test_parses_target_and_range_together():
    assert parse_weekly_target_minutes("180 min/week, sessions 30-60 minutes") == 180
    assert parse_session_length_range("180 min/week, sessions 30-60 minutes") == (30, 60)


def test_parses_target_even_when_the_session_range_comes_first():
    goal = "gym sessions 30-60 minutes, 180 minutes of exercise a week"
    assert parse_weekly_target_minutes(goal) == 180
    assert parse_session_length_range(goal) == (30, 60)


def test_no_weekly_target_in_a_cadence_only_goal():
    # "most nights" names a cadence, not a weekly total — must not be guessed.
    assert parse_weekly_target_minutes("read for 20-40 minutes most nights") is None
    assert parse_session_length_range("read for 20-40 minutes most nights") == (20, 40)


def test_zone_anchored_goal_has_no_target_or_range():
    goal = "listen to an audiobook, whenever I have commute"
    assert parse_weekly_target_minutes(goal) is None
    assert parse_session_length_range(goal) is None


def test_session_range_normalizes_reversed_order():
    assert parse_session_length_range("60-30 minutes") == (30, 60)


def test_target_accounting_sums_placed_minutes():
    placed = [_iv(2026, 8, 3, 6, 0, 6, 45), _iv(2026, 8, 5, 6, 0, 6, 30)]
    result = target_accounting("180 min/week, sessions 30-60 minutes", placed)
    assert result.target_minutes == 180
    assert result.session_min_minutes == 30
    assert result.session_max_minutes == 60
    assert result.placed_minutes == 75
    assert result.remaining_minutes == 105


def test_target_accounting_remaining_floors_at_zero_when_overshot():
    placed = [_iv(2026, 8, 3, 6, 0, 9, 0)]  # 180 minutes
    result = target_accounting("120 min/week", placed)
    assert result.placed_minutes == 180
    assert result.remaining_minutes == 0


def test_target_accounting_remaining_is_none_without_a_target():
    result = target_accounting("whenever I have commute", [])
    assert result.target_minutes is None
    assert result.remaining_minutes is None
    assert result.placed_minutes == 0


# ---------------------------------------------------------------------------
# score_candidates
# ---------------------------------------------------------------------------


def test_lighter_day_scores_higher_for_the_same_duration():
    light_day = _iv(2026, 8, 3, 18, 0, 18, 30)
    busy_day = _iv(2026, 8, 4, 18, 0, 18, 30)
    scored = score_candidates(
        [light_day, busy_day],
        day_loads={date(2026, 8, 3): 0.0, date(2026, 8, 4): 480.0},
    )
    by_interval = {c.interval: c for c in scored}
    assert by_interval[light_day].score > by_interval[busy_day].score


def test_shorter_session_scores_higher_than_longer_on_a_busy_day():
    short = _iv(2026, 8, 3, 18, 0, 18, 30)
    long = _iv(2026, 8, 3, 18, 0, 19, 30)
    scored = score_candidates([short, long], day_loads={date(2026, 8, 3): 480.0})
    by_interval = {c.interval: c for c in scored}
    assert by_interval[short].score > by_interval[long].score


def test_duration_does_not_matter_on_a_fully_free_day():
    short = _iv(2026, 8, 3, 18, 0, 18, 30)
    long = _iv(2026, 8, 3, 18, 0, 19, 30)
    scored = score_candidates([short, long], day_loads={})
    by_interval = {c.interval: c for c in scored}
    assert by_interval[short].score == by_interval[long].score


def test_weekend_candidate_scores_higher_than_an_equal_weekday_one():
    # 2026-08-08 is a Saturday, 2026-08-03 is a Monday.
    weekend = _iv(2026, 8, 8, 9, 0, 9, 30)
    weekday = _iv(2026, 8, 3, 9, 0, 9, 30)
    scored = score_candidates([weekend, weekday])
    by_interval = {c.interval: c for c in scored}
    assert by_interval[weekend].score > by_interval[weekday].score
    assert by_interval[weekend].is_weekend is True
    assert by_interval[weekday].is_weekend is False


def test_repeatedly_bumped_slot_scores_lower():
    candidate = _iv(2026, 8, 10, 6, 0, 6, 30)  # Monday 06:00
    review = [
        ReviewEntry(planned_start=_dt(2026, 7, 27, 6, 0), outcome="moved", bumped_by="Standup"),
        ReviewEntry(planned_start=_dt(2026, 8, 3, 6, 0), outcome="moved", bumped_by="Standup"),
    ]
    scored_with_history = score_candidates([candidate], prior_review=review)
    scored_without_history = score_candidates([candidate])
    assert scored_with_history[0].score < scored_without_history[0].score
    assert scored_with_history[0].repeat_bump_penalty > 0


def test_a_single_bump_is_not_a_pattern():
    candidate = _iv(2026, 8, 10, 6, 0, 6, 30)
    review = [ReviewEntry(planned_start=_dt(2026, 8, 3, 6, 0), outcome="moved", bumped_by="Standup")]
    scored = score_candidates([candidate], prior_review=review)
    assert scored[0].repeat_bump_penalty == 0


def test_bump_by_a_known_habit_is_not_penalized():
    """A slot repeatedly 'bumped' by another one of the user's own habits
    is the guardrails working as intended, not an unrelated conflict —
    instruction.md is explicit that this must not be treated the same."""
    candidate = _iv(2026, 8, 10, 6, 0, 6, 30)
    review = [
        ReviewEntry(planned_start=_dt(2026, 7, 27, 6, 0), outcome="moved", bumped_by="Tennis"),
        ReviewEntry(planned_start=_dt(2026, 8, 3, 6, 0), outcome="moved", bumped_by="Tennis"),
    ]
    scored = score_candidates([candidate], prior_review=review, known_habit_labels=frozenset({"Tennis"}))
    assert scored[0].repeat_bump_penalty == 0


def test_kept_sessions_never_count_toward_a_bump_pattern():
    candidate = _iv(2026, 8, 10, 6, 0, 6, 30)
    review = [
        ReviewEntry(planned_start=_dt(2026, 7, 27, 6, 0), outcome="kept", bumped_by=None),
        ReviewEntry(planned_start=_dt(2026, 8, 3, 6, 0), outcome="kept", bumped_by=None),
    ]
    scored = score_candidates([candidate], prior_review=review)
    assert scored[0].repeat_bump_penalty == 0


def test_results_are_sorted_best_first():
    a = _iv(2026, 8, 3, 18, 0, 18, 30)
    b = _iv(2026, 8, 8, 18, 0, 18, 30)  # weekend, should outrank a
    scored = score_candidates([a, b])
    assert [c.interval for c in scored] == [b, a]
