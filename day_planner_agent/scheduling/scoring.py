"""Ranking candidate placement intervals (A4.1).

This package never chooses a placement — "choosing among them stays with
the model" is A4.1's own explicit scope boundary — it only attaches a
score reflecting the three heuristics instruction.md's placement
paragraph already documents in prose: day-load sizing, a weekend
preference, and a repeat-bump penalty. Higher score is better; candidates
come back sorted best-first, but the caller decides how many to offer
the model and whether to act on the ranking at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Sequence

from .models import DAYS_OF_WEEK, Interval

# Both weights are in "minutes of score," chosen to be comparable in
# magnitude to a typical habit session (30-60 minutes) rather than
# derived from any formal model — tune against A3.1's eval suite if the
# ranking doesn't match real placements well in practice.
_WEEKEND_BONUS_MINUTES = 30.0
_REPEAT_BUMP_PENALTY_MINUTES = 60.0

# A slot bumped by the very same thing this many times or more counts as
# a pattern, not a coincidence — matches instruction.md's own "more than
# once" wording (i.e. two is already enough, not three).
_REPEAT_BUMP_THRESHOLD = 2


@dataclass(frozen=True)
class ReviewEntry:
    """The one slice of a review_habit_week result this module needs.
    Adapting the tool's actual (JSON-shaped) result into this — parsing
    planned_start into a real datetime — is the caller's job; see this
    package's own __init__.py docstring for why no parsing lives here."""

    planned_start: datetime
    outcome: str  # "kept" | "moved" | "gone"
    bumped_by: str | None = None


@dataclass(frozen=True)
class ScoredCandidate:
    interval: Interval
    score: float
    day_load_minutes: float
    is_weekend: bool
    repeat_bump_penalty: float
    # The bumped_by name that triggered the penalty, for a human-readable
    # reason string (e.g. "repeatedly bumped by Standup") — informational
    # only, never read back into the score itself. None whenever
    # repeat_bump_penalty is 0. If more than one bumped_by independently
    # cleared the threshold for the same slot, this is whichever was seen
    # first (insertion order) — a real case, but rare enough that picking
    # one deterministically over reporting a list was judged not worth
    # the added shape complexity here (A4.2).
    repeat_bump_reason: str | None = None


def _slot_key(dt: datetime) -> tuple[str, str]:
    """(weekday code, wall-clock HH:MM) — "same time slot" is matched
    exactly on both, not fuzzily. A genuinely different time the model
    picked for a rescheduled attempt is a different slot on purpose;
    this only recognizes a pattern when the model (or the user) landed
    on the literal same weekday-and-time repeatedly."""
    return DAYS_OF_WEEK[dt.weekday()], dt.strftime("%H:%M")


def _weak_slots(
    prior_review: Sequence[ReviewEntry], known_habit_labels: frozenset[str]
) -> dict[tuple[str, str], str]:
    """Slots bumped _REPEAT_BUMP_THRESHOLD+ times by the same
    bumped_by — excluding "kept" entries (nothing to penalize), entries
    with no bumped_by at all (there's no repeat pattern to name), and any
    bumped_by that names one of the user's own tracked habits, which is
    the guardrails working as intended, not the "unrelated conflict"
    instruction.md's repeat-bump rule is about.

    Maps each weak slot to the bumped_by that made it weak (first one
    seen, in prior_review's own order, if more than one independently
    qualifies) — see ScoredCandidate.repeat_bump_reason for why this is
    a single name rather than a list."""
    counts: dict[tuple[tuple[str, str], str], int] = {}
    for entry in prior_review:
        if entry.outcome == "kept" or not entry.bumped_by:
            continue
        if entry.bumped_by in known_habit_labels:
            continue
        key = (_slot_key(entry.planned_start), entry.bumped_by)
        counts[key] = counts.get(key, 0) + 1
    weak: dict[tuple[str, str], str] = {}
    for (slot, bumped_by), n in counts.items():
        if n >= _REPEAT_BUMP_THRESHOLD and slot not in weak:
            weak[slot] = bumped_by
    return weak


def score_candidates(
    intervals: Sequence[Interval],
    *,
    day_loads: dict[date, float] | None = None,
    prior_review: Sequence[ReviewEntry] = (),
    known_habit_labels: frozenset[str] = frozenset(),
) -> list[ScoredCandidate]:
    """Scores and ranks `intervals` best-first.

    day_loads maps a calendar date to however many minutes are already
    committed that day (existing events plus already-placed habit
    sessions) — used only to size the day-load component below, never to
    filter or block anything (that's free_intervals' job). A date absent
    from day_loads is treated as fully free (load 0).

    Components, all additive:
    - day-load: `-day_load_minutes * duration_minutes / 60`. Zero on a
      completely free day regardless of duration, so this never penalizes
      a long session on a light day — it only makes a long session score
      worse than a short one *on the same busy day*, which is
      instruction.md's "give a packed day a shorter session" as a ranking
      preference rather than a length choice.
    - weekend: a flat bonus for any candidate landing on Saturday or
      Sunday (instruction.md's "prefer loading a larger share ... onto
      Saturday/Sunday").
    - repeat-bump: a flat penalty for a candidate landing on a slot
      _weak_slots flags (see its own docstring).
    """
    day_loads = day_loads or {}
    weak_slots = _weak_slots(prior_review, known_habit_labels)

    scored: list[ScoredCandidate] = []
    for iv in intervals:
        day = iv.start.date()
        load = day_loads.get(day, 0.0)
        is_weekend = DAYS_OF_WEEK[iv.start.weekday()] in ("sat", "sun")
        bump_reason = weak_slots.get(_slot_key(iv.start))
        penalty = _REPEAT_BUMP_PENALTY_MINUTES if bump_reason is not None else 0.0

        score = (
            -load * iv.duration_minutes / 60.0
            + (_WEEKEND_BONUS_MINUTES if is_weekend else 0.0)
            - penalty
        )
        scored.append(
            ScoredCandidate(
                interval=iv,
                score=score,
                day_load_minutes=load,
                is_weekend=is_weekend,
                repeat_bump_penalty=penalty,
                repeat_bump_reason=bump_reason,
            )
        )

    return sorted(scored, key=lambda c: c.score, reverse=True)
