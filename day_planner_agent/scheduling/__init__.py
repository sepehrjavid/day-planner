"""Pure-function constraint solving for habit placement (A4.1).

Extracted from instruction.md's placement paragraph (~900 words asking
Gemini Flash to do interval arithmetic across zones, sleep, cool-down,
wake-up, and per-habit allowed_zones exceptions in prose) into ordinary,
exhaustively-unit-tested Python. No agent, no tool registration, no
Vertex/ADK/httpx import anywhere in this package — every function takes
plain data in and returns plain data out. Wiring this into an actual
tool the model can call, and shrinking instruction.md now that the
arithmetic doesn't have to live in prose, are both later tasks (A4.2,
A4.3) — this one only has to be correct and tested on its own.

- `free_intervals` — the core: given a date range, zones, the sleep
  schedule, and existing busy time, what's actually open.
- `zone_occurrences` — concrete occurrences of one zone, for placing a
  zone-anchored habit at exactly the zone's own times.
- `collisions_with` — which already-placed sessions a new or changed
  constraint now conflicts with.
- `target_accounting` — parse a habit's free-text goal into a weekly
  target and session-length range, and account placed time against it.
- `score_candidates` — rank candidate intervals by day-load fit, a
  weekend preference, and a repeat-bump penalty; never chooses among
  them, that stays with the model.

See models.py for the plain dataclasses (Interval, Zone, SleepSchedule,
DayOverride) every function here operates on, and each submodule's own
docstring for the specific rule it implements.
"""

from .intervals import collisions_with, free_intervals, zone_occurrences
from .models import DAYS_OF_WEEK, DayOverride, Interval, SleepSchedule, Zone
from .scoring import ReviewEntry, ScoredCandidate, score_candidates
from .targets import (
    TargetAccounting,
    parse_session_length_range,
    parse_weekly_target_minutes,
    target_accounting,
)

__all__ = [
    "DAYS_OF_WEEK",
    "DayOverride",
    "Interval",
    "ReviewEntry",
    "ScoredCandidate",
    "SleepSchedule",
    "TargetAccounting",
    "Zone",
    "collisions_with",
    "free_intervals",
    "parse_session_length_range",
    "parse_weekly_target_minutes",
    "score_candidates",
    "target_accounting",
    "zone_occurrences",
]
