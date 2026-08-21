"""Unit coverage of evals/scenario.py's YAML parsing and field
normalization — pure, no model calls."""

import textwrap

from day_planner_agent.evals.scenario import load_scenario_file

SCENARIO_YAML = textwrap.dedent(
    """\
    name: does not place a session during work hours
    tier: constraint
    given:
      today: "2026-08-17"
      zones:
        - {label: Work, start: "09:00", end: "17:30", days: [Mon, Tue, Wed, Thu, Fri]}
      sleep_schedule: {sleep: "23:00", wake: "07:00", cool_down: 30, wake_up_buffer: 15}
      habits:
        - {label: Gym, goal: "180 min/week, sessions 30-60 minutes"}
      calendar_events:
        - {summary: Standup, start: "2026-08-17T09:00:00", end: "2026-08-17T09:15:00"}
    when:
      user_says: "plan my gym sessions for this week"
    expect:
      tool_calls:
        - {name: add_calendar_event, min_count: 3}
      invariants:
        - no_session_overlaps_any_zone
        - no_session_overlaps_sleep_or_cooldown
    """
)


def test_parses_top_level_fields(tmp_path):
    path = tmp_path / "scenario.yaml"
    path.write_text(SCENARIO_YAML)

    scenario = load_scenario_file(path)

    assert scenario.name == "does not place a session during work hours"
    assert scenario.tier == "constraint"
    assert scenario.user_says == "plan my gym sessions for this week"
    assert scenario.given.today == "2026-08-17"
    assert scenario.source_file == path


def test_normalizes_zone_fields(tmp_path):
    path = tmp_path / "scenario.yaml"
    path.write_text(SCENARIO_YAML)
    scenario = load_scenario_file(path)

    assert scenario.given.zones == [
        {
            "zone_id": "z1",
            "label": "Work",
            "start_time": "09:00",
            "end_time": "17:30",
            "days_of_week": ["mon", "tue", "wed", "thu", "fri"],
        }
    ]


def test_normalizes_sleep_schedule_fields(tmp_path):
    path = tmp_path / "scenario.yaml"
    path.write_text(SCENARIO_YAML)
    scenario = load_scenario_file(path)

    assert scenario.given.sleep_schedule == {
        "sleep_time": "23:00",
        "wake_time": "07:00",
        "cool_down_minutes": 30,
        "wake_up_buffer_minutes": 15,
        "day_overrides": {},
    }


def test_normalizes_habit_fields_with_synthetic_id(tmp_path):
    path = tmp_path / "scenario.yaml"
    path.write_text(SCENARIO_YAML)
    scenario = load_scenario_file(path)

    assert scenario.given.habits == [
        {
            "habit_id": "h1",
            "label": "Gym",
            "goal": "180 min/week, sessions 30-60 minutes",
            "status": "active",
            "allowed_zones": [],
        }
    ]


def test_normalizes_calendar_events_to_google_item_shape(tmp_path):
    path = tmp_path / "scenario.yaml"
    path.write_text(SCENARIO_YAML)
    scenario = load_scenario_file(path)

    assert scenario.given.calendar_events == [
        {
            "id": "evt1",
            "summary": "Standup",
            "start": {"dateTime": "2026-08-17T09:00:00"},
            "end": {"dateTime": "2026-08-17T09:15:00"},
        }
    ]


def test_parses_tool_call_and_invariant_expectations(tmp_path):
    path = tmp_path / "scenario.yaml"
    path.write_text(SCENARIO_YAML)
    scenario = load_scenario_file(path)

    assert len(scenario.expect.tool_calls) == 1
    assert scenario.expect.tool_calls[0].name == "add_calendar_event"
    assert scenario.expect.tool_calls[0].min_count == 3
    assert scenario.expect.invariants == [
        "no_session_overlaps_any_zone",
        "no_session_overlaps_sleep_or_cooldown",
    ]


def test_habit_id_tagged_calendar_event(tmp_path):
    yaml_text = textwrap.dedent(
        """\
        name: reschedule keeps habit tagging
        given:
          today: "2026-08-17"
          calendar_events:
            - {summary: Gym, start: "2026-08-17T07:00:00", end: "2026-08-17T07:30:00", habit_id: h1}
        when:
          user_says: "irrelevant"
        """
    )
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml_text)
    scenario = load_scenario_file(path)

    event = scenario.given.calendar_events[0]
    assert event["extendedProperties"]["private"]["day_planner_habit_id"] == "h1"


def test_failure_injection_fields_default_to_off(tmp_path):
    yaml_text = textwrap.dedent(
        """\
        name: no failure injection here
        given:
          today: "2026-08-17"
        when:
          user_says: "irrelevant"
        """
    )
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml_text)
    scenario = load_scenario_file(path)

    assert scenario.given.zones_fetch_fails is False
    assert scenario.given.needs_auth is False
    assert scenario.given.calendar_access_role == "owner"


def test_failure_injection_fields_parse_when_set(tmp_path):
    yaml_text = textwrap.dedent(
        """\
        name: zone fetch fails
        given:
          today: "2026-08-17"
          zones_fetch_fails: true
          needs_auth: true
          calendar_access_role: reader
        when:
          user_says: "irrelevant"
        """
    )
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml_text)
    scenario = load_scenario_file(path)

    assert scenario.given.zones_fetch_fails is True
    assert scenario.given.needs_auth is True
    assert scenario.given.calendar_access_role == "reader"


def test_model_invoked_defaults_to_false(tmp_path):
    yaml_text = textwrap.dedent(
        """\
        name: no model_invoked expectation here
        given:
          today: "2026-08-17"
        when:
          user_says: "irrelevant"
        """
    )
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml_text)
    scenario = load_scenario_file(path)

    assert scenario.expect.model_invoked is False


def test_model_invoked_parses_when_set(tmp_path):
    yaml_text = textwrap.dedent(
        """\
        name: model_invoked expectation set
        given:
          today: "2026-08-17"
        when:
          user_says: "irrelevant"
        expect:
          model_invoked: true
        """
    )
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml_text)
    scenario = load_scenario_file(path)

    assert scenario.expect.model_invoked is True
