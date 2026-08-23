"""Parsing a habit's free-text goal into a weekly target and a session
length range, and accounting placed time against it (A4.1).

A habit's `goal` (day_planner_backend_app/app/schemas/habits.py's
CreateHabitRequest.goal) is deliberately free text the user states in
their own words — "180 min/week, sessions 30-60 minutes", "gym sessions
30-60 minutes, 180 minutes of exercise a week", "read for 20-40 minutes
most nights". This is a small, explicitly-scoped pattern matcher, not a
general NLP parser: it recognizes a handful of phrasings for "N
minutes/week" and "N-M minute sessions" and returns None for whatever it
can't find. None is a conservative "not found," never a guessed number —
a zone-anchored habit's goal (instruction.md: "whenever I have that
zone", no frequency/duration of its own) is expected to parse to
target_minutes=None, and so is a goal this parser simply doesn't
recognize (like "most nights" above, which names a cadence but no
weekly total). Callers must not treat None as zero.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from .models import Interval

# "30-60 minutes" / "30 to 60 min" — the session-length range. Requires
# both numbers in the same breath, deliberately not "sessions of 45
# minutes" (a single fixed length) since instruction.md's own model
# always speaks of a range, even a degenerate one ("30-30 minutes").
_SESSION_RANGE_RE = re.compile(
    r"(\d+)\s*(?:-|to)\s*(\d+)\s*min(?:ute)?s?", re.IGNORECASE
)

# "180 min/week" / "180 minutes a week" / "180 minutes of exercise per
# week" / "180 minutes weekly". The bridge between the number and the
# week-marker excludes , ; . so it can't reach across a different clause
# of the same goal string (see test_scheduling.py for why that matters:
# "sessions 30-60 minutes, 180 minutes ... a week" must resolve to 180,
# not the 60 from the session range that happens to precede it).
_WEEKLY_TARGET_RE = re.compile(
    r"(\d+)\s*min(?:ute)?s?(?:(?!\d)[^,;.])*?(?:/\s*week|\ba\s+week\b|\bper\s+week\b|\bweekly\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TargetAccounting:
    target_minutes: int | None
    session_min_minutes: int | None
    session_max_minutes: int | None
    placed_minutes: float
    remaining_minutes: float | None


def parse_weekly_target_minutes(goal: str) -> int | None:
    match = _WEEKLY_TARGET_RE.search(goal)
    return int(match.group(1)) if match else None


def parse_session_length_range(goal: str) -> tuple[int, int] | None:
    match = _SESSION_RANGE_RE.search(goal)
    if not match:
        return None
    low, high = int(match.group(1)), int(match.group(2))
    return (low, high) if low <= high else (high, low)


def target_accounting(goal: str, placed_sessions: Sequence[Interval]) -> TargetAccounting:
    """Parses `goal` and sums `placed_sessions`' durations against
    whatever weekly target was found. remaining_minutes is floored at 0
    (a habit that overshot its target isn't "negative remaining," it's
    done) and is None whenever target_minutes is — there's nothing to
    count down from a target that wasn't there in the first place."""
    target_minutes = parse_weekly_target_minutes(goal)
    session_range = parse_session_length_range(goal)
    placed_minutes = sum((iv.duration_minutes for iv in placed_sessions), start=0.0)
    remaining_minutes = (
        max(0.0, target_minutes - placed_minutes) if target_minutes is not None else None
    )
    return TargetAccounting(
        target_minutes=target_minutes,
        session_min_minutes=session_range[0] if session_range else None,
        session_max_minutes=session_range[1] if session_range else None,
        placed_minutes=placed_minutes,
        remaining_minutes=remaining_minutes,
    )
