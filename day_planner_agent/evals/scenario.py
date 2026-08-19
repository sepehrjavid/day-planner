"""Declarative eval scenario format (A3.1) and its loader.

A scenario describes fixture state (`given`), a single user message
(`when`), and what a compliant agent must do in response (`expect`) — see
scenarios/zone_sleep/*.yaml for real examples, or the format sketch in
docs/roadmaps/1-agent.md's A3.1 task.

`given`'s zones/sleep_schedule/habits use short, scenario-authoring-
friendly field names (`start`/`end`, `Mon`/`Tue`, `sleep`/`wake`) rather
than the real backend_client shapes (`start_time`/`end_time`,
`days_of_week: ["mon", ...]`, `sleep_time`/`wake_time`) — this module
normalizes between the two so a scenario file reads like the domain, not
like a Firestore document. See conftest.py's ScenarioFixture for what the
normalized shapes feed into.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

_DAY_ABBREVIATIONS = {
    "mon": "mon",
    "tue": "tue",
    "wed": "wed",
    "thu": "thu",
    "fri": "fri",
    "sat": "sat",
    "sun": "sun",
}


def _normalize_day(day: str) -> str:
    key = day.strip().lower()[:3]
    if key not in _DAY_ABBREVIATIONS:
        raise ValueError(f"unrecognized day of week: {day!r}")
    return _DAY_ABBREVIATIONS[key]


def normalize_zone(raw: dict, *, zone_id: str) -> dict:
    return {
        "zone_id": zone_id,
        "label": raw["label"],
        "start_time": raw["start"],
        "end_time": raw["end"],
        "days_of_week": [_normalize_day(d) for d in raw["days"]],
    }


def normalize_sleep_schedule(raw: dict | None) -> dict | None:
    if raw is None:
        return None
    return {
        "sleep_time": raw["sleep"],
        "wake_time": raw["wake"],
        "cool_down_minutes": raw.get("cool_down", 0),
        "wake_up_buffer_minutes": raw.get("wake_up_buffer", 0),
        "day_overrides": raw.get("day_overrides", {}),
    }


def normalize_habit(raw: dict, *, habit_id: str) -> dict:
    return {
        "habit_id": habit_id,
        "label": raw["label"],
        "goal": raw["goal"],
        "status": raw.get("status", "active"),
        "allowed_zones": raw.get("allowed_zones", []),
    }


def normalize_calendar_event(raw: dict, *, event_id: str) -> dict:
    """raw: {summary, start, end, habit_id?} with start/end as local
    wall-clock ISO datetimes (matching add_calendar_event's own
    convention) — converted to the raw Google Calendar item shape
    conftest.py's FakeEventsResource serves."""
    item = {
        "id": raw.get("id", event_id),
        "summary": raw["summary"],
        "start": {"dateTime": raw["start"]},
        "end": {"dateTime": raw["end"]},
    }
    if raw.get("habit_id"):
        item["extendedProperties"] = {"private": {"day_planner_habit_id": raw["habit_id"]}}
    return item


@dataclass
class Given:
    today: str
    zones: list[dict] = field(default_factory=list)
    sleep_schedule: dict | None = None
    habits: list[dict] = field(default_factory=list)
    calendar_events: list[dict] = field(default_factory=list)


@dataclass
class ToolCallExpectation:
    name: str
    min_count: int = 1
    max_count: int | None = None


@dataclass
class Expect:
    tool_calls: list[ToolCallExpectation] = field(default_factory=list)
    invariants: list[str] = field(default_factory=list)


@dataclass
class Scenario:
    name: str
    tier: str  # "constraint" | "decision" | "quality"
    given: Given
    user_says: str
    expect: Expect
    source_file: Path


def _parse_scenario(raw: dict, source_file: Path) -> Scenario:
    given_raw = raw["given"]
    given = Given(
        today=given_raw["today"],
        zones=[
            normalize_zone(z, zone_id=f"z{i + 1}")
            for i, z in enumerate(given_raw.get("zones", []))
        ],
        sleep_schedule=normalize_sleep_schedule(given_raw.get("sleep_schedule")),
        habits=[
            normalize_habit(h, habit_id=f"h{i + 1}")
            for i, h in enumerate(given_raw.get("habits", []))
        ],
        calendar_events=[
            normalize_calendar_event(e, event_id=f"evt{i + 1}")
            for i, e in enumerate(given_raw.get("calendar_events", []))
        ],
    )
    expect_raw = raw.get("expect", {})
    expect = Expect(
        tool_calls=[
            ToolCallExpectation(
                name=tc["name"], min_count=tc.get("min_count", 1), max_count=tc.get("max_count")
            )
            for tc in expect_raw.get("tool_calls", [])
        ],
        invariants=list(expect_raw.get("invariants", [])),
    )
    return Scenario(
        name=raw["name"],
        tier=raw.get("tier", "constraint"),
        given=given,
        user_says=raw["when"]["user_says"],
        expect=expect,
        source_file=source_file,
    )


def load_scenario_file(path: Path) -> Scenario:
    raw = yaml.safe_load(path.read_text())
    return _parse_scenario(raw, source_file=path)


def load_scenarios(root: Path) -> list[Scenario]:
    """All *.yaml scenarios under root, recursively, sorted by path for a
    deterministic run order."""
    return [load_scenario_file(p) for p in sorted(root.rglob("*.yaml"))]
