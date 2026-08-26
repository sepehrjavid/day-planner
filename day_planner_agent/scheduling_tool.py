"""Wires the scheduling engine (day_planner_agent/scheduling/, A4.1) into
the agent — `get_available_slots` still in shadow mode, `find_zone_collisions`
now a registered, model-callable tool (A4.3).

**get_available_slots stays in shadow mode**: fully built and unit-tested,
but deliberately **not** registered in agent.py's `Agent(...).tools=[...]`
list — the model never sees it and cannot call it. A same-day, controlled
A3.1 run (with vs. without this module's tool registered) found
double-digit-point tier regressions from merely adding an unexplained,
uninstructed tool to the model's tool list; instruction.md was never
going to be changed in that task either way, so the model would have had
a capability it was never told how or when to use. See agent.py's
`_log_schedule_shadow_comparison` for the full reasoning and for the
actual shadow-mode mechanism: an `after_tool_callback` that calls this
module's engine directly, out-of-band, whenever a real
`add_calendar_event`/`update_calendar_event` call places or moves a
habit-tagged session — never through the model, so there is no tool-list
change for the model to react to at all. Registering it requires the
same add-then-cut discipline `find_zone_collisions` follows below:
instruction text explaining it ships in the same PR as the registration,
never before or after.

**find_zone_collisions is different**: A4.3's roadmap entry requires the
tool and its usage text to ship together (the exact gap that caused the
regression above), so this one is registered in agent.py's tools=[...]
list *and* instruction.md's paragraph 17 explains when to call it, in the
same PR ("PR A" — additive, see evals/BASELINE.md's "A4.3" section for
the verification that PR did). A follow-up "PR B" removed the old
mechanical-scan prose for the zone case specifically once those numbers
held — see paragraph 17's current text for exactly what's left: it still
directs a manual get_calendar_events scan for the update_profile/
set_sleep_schedule cases, which this tool doesn't cover, and the
"ask the user how to resolve it" clause was kept verbatim across both
PRs since it sits in the same sentence flow as the part that changed.

`_compute_candidates` is the single source of truth for both
`get_available_slots` and the shadow comparison — one code path, so a
bug fixed for one is fixed for both, and the two can never quietly
diverge in what they consider "the engine's answer."

Known limitation, inherited rather than introduced here: like
review_habit_week, the habit-session lookup below treats `date_from`/
`date_to` as UTC calendar days (`f"{date}T00:00:00Z"`), not the user's
own local calendar days — day_planner_backend_app has no per-user
timezone field to do better with yet (see A6.3's zones.py docstring).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from google.adk.tools import ToolContext

from . import backend_client, calendar_tool, domain_client, habit_tools, scheduling

logger = logging.getLogger(__name__)

# Session-state key the shadow-comparison hook (agent.py) appends to.
# Read out by day_planner_backend_app's turn_log.py the same zero-token
# way _HABIT_SESSION_OUTCOMES_STATE_KEY already does (habit_tools.py).
SHADOW_COMPARISONS_STATE_KEY = "day_planner:schedule_shadow_comparisons"

_DEFAULT_MIN_MINUTES = 15
_DEFAULT_MAX_MINUTES = 60
_MAX_CANDIDATES_RETURNED = 10

# Labeling only — "reasons" text for the model/a human reviewer, never
# fed back into score_candidates' own ranking, which already uses the
# continuous day_load value.
_LIGHT_DAY_THRESHOLD_MINUTES = 120


def _adapt_zone(raw: dict) -> scheduling.Zone:
    return scheduling.Zone(
        label=raw["label"],
        start_time=raw["start_time"],
        end_time=raw["end_time"],
        days_of_week=tuple(raw["days_of_week"]),
    )


def _adapt_sleep_schedule(raw: dict | None) -> scheduling.SleepSchedule | None:
    if raw is None:
        return None
    overrides = {
        day: scheduling.DayOverride(
            sleep_time=override.get("sleep_time"), wake_time=override.get("wake_time")
        )
        for day, override in (raw.get("day_overrides") or {}).items()
    }
    return scheduling.SleepSchedule(
        sleep_time=raw["sleep_time"],
        wake_time=raw["wake_time"],
        cool_down_minutes=raw.get("cool_down_minutes") or 0,
        wake_up_buffer_minutes=raw.get("wake_up_buffer_minutes") or 0,
        day_overrides=overrides,
    )


def _adapt_busy_events(events: list[dict], *, tz: ZoneInfo) -> list[scheduling.Interval]:
    """Every event that blocks time, as an Interval in the reference tz.

    Two shapes calendar_tool.py's _trim_google_event can produce for
    start_time/end_time, both handled here rather than dropped:

    - A "date"-only string ("2026-08-29", no "T") for an all-day event —
      Google Calendar has no dateTime field at all for these, only date.
      Treated as a whole-day block from midnight to midnight in the
      reference tz (the end date is exclusive, matching every other
      half-open interval in this codebase), not skipped: a real all-day
      "Vacation" or "Conference" is exactly the kind of thing a habit
      session must never get placed on top of. Originally (wrongly)
      excluded here as "not a scheduling constraint" (A4.2) — found via a
      full-suite run's genuine no_session_overlaps_existing_events
      violation once this tool started actually driving placement
      instead of only shadow-comparing.
    - A full dateTime string for a timed event — offset-aware if it came
      from a real Google Calendar response (the API always includes one),
      naive if it's this project's own plain-local-wall-clock convention
      (e.g. every eval fixture's calendar_events; see instruction.md's
      "give start_time/end_time as plain local wall-clock time" rule for
      add_calendar_event). A naive value is localized to `tz` — the same
      reference timezone resolve_reference_timezone already resolves
      zones/sleep-schedule wall-clock strings against — rather than
      silently dropped, which is what every naive-format busy event got
      before this fix: invisible to this engine's free-interval
      computation, in every eval scenario, with nothing to reveal it
      until a real placement actually landed on top of one.
    """
    intervals: list[scheduling.Interval] = []
    for event in events:
        start, end = event.get("start_time"), event.get("end_time")
        if not start or not end:
            continue
        if "T" not in start or "T" not in end:
            try:
                start_day, end_day = date.fromisoformat(start), date.fromisoformat(end)
            except ValueError:
                continue
            if end_day < start_day:
                continue
            intervals.append(
                scheduling.Interval(
                    datetime.combine(start_day, time.min, tzinfo=tz),
                    datetime.combine(end_day, time.min, tzinfo=tz),
                )
            )
            continue
        try:
            start_dt, end_dt = datetime.fromisoformat(start), datetime.fromisoformat(end)
        except ValueError:
            continue
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=tz)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=tz)
        if end_dt < start_dt:
            continue
        intervals.append(scheduling.Interval(start_dt, end_dt))
    return intervals


def _adapt_placed_sessions(sessions: list[dict]) -> list[scheduling.Interval]:
    intervals: list[scheduling.Interval] = []
    for session in sessions:
        try:
            start_dt = datetime.fromisoformat(session["planned_start"])
            end_dt = datetime.fromisoformat(session["planned_end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end_dt < start_dt:
            continue
        intervals.append(scheduling.Interval(start_dt, end_dt))
    return intervals


def _adapt_review(sessions: list[dict]) -> list[scheduling.ReviewEntry]:
    entries: list[scheduling.ReviewEntry] = []
    for session in sessions:
        try:
            planned_start = datetime.fromisoformat(session["planned_start"])
        except (KeyError, TypeError, ValueError):
            continue
        entries.append(
            scheduling.ReviewEntry(
                planned_start=planned_start,
                outcome=session.get("outcome", "kept"),
                bumped_by=session.get("bumped_by"),
            )
        )
    return entries


def _day_loads(busy: list[scheduling.Interval]) -> dict[date, float]:
    """Existing calendar load per day, from busy events only — not
    placed_sessions, which is a separate signal used only for
    target_accounting below. A day's load reflects everything actually on
    the calendar regardless of source, including this same habit's own
    earlier sessions that week; there's no reason to exclude those from
    "how busy is this day"."""
    loads: dict[date, float] = {}
    for iv in busy:
        d = iv.start.date()
        loads[d] = loads.get(d, 0.0) + iv.duration_minutes
    return loads


def _slice_candidates(
    free: list[scheduling.Interval], min_minutes: int, max_minutes: int
) -> list[scheduling.Interval]:
    """Coarse, first-pass candidate generation (A4.2): at most two
    candidates per free gap — one at min_minutes, one at
    min(max_minutes, the gap's own length) — both anchored at the gap's
    own start. This is deliberately simple: it's what score_candidates'
    day-load component needs to actually differentiate a short session
    from a long one on the same busy day, without sweeping every
    possible start offset within a gap. Revisit once shadow-mode
    disagreement data shows whether coarser slicing is actually costing
    real disagreements against what the model independently chooses."""
    candidates: list[scheduling.Interval] = []
    for gap in free:
        if gap.duration_minutes < min_minutes:
            continue
        lengths = {min_minutes, min(max_minutes, int(gap.duration_minutes))}
        for length in lengths:
            candidates.append(
                scheduling.Interval(gap.start, gap.start + timedelta(minutes=length))
            )
    return candidates


async def _compute_candidates(
    tool_context: ToolContext,
    user_id: str,
    date_from: str,
    date_to: str,
    habit_id: str,
    min_minutes: int | None,
    max_minutes: int | None,
) -> dict:
    habits = await domain_client.list_habits(user_id)
    habit = next((h for h in habits if h["habit_id"] == habit_id), None)
    if habit is None:
        return {"status": "not_found", "message": f"No habit {habit_id!r}."}

    tz_name = await calendar_tool.resolve_reference_timezone(tool_context, user_id)
    if tz_name is None:
        return {
            "status": "needs_auth",
            "message": "No connected calendar to resolve a reference timezone from.",
        }
    tz = ZoneInfo(tz_name)

    calendar_state = await calendar_tool.get_calendar_events(tool_context, date_from, date_to)
    if calendar_state["status"] != "success":
        return calendar_state

    zones = [_adapt_zone(z) for z in await domain_client.list_zones(user_id)]
    sleep_schedule = _adapt_sleep_schedule(await domain_client.get_sleep_schedule(user_id))

    start_date = date.fromisoformat(date_from)
    end_date = date.fromisoformat(date_to)

    all_sessions = await domain_client.list_habit_sessions(
        user_id, planned_from=f"{date_from}T00:00:00Z", planned_to=f"{date_to}T00:00:00Z"
    )
    placed_sessions = _adapt_placed_sessions(
        [s for s in all_sessions if s["habit_id"] == habit_id]
    )

    accounting = scheduling.target_accounting(habit["goal"], placed_sessions)
    allowed_zones = habit.get("allowed_zones") or []
    zone_labels = {z.label for z in zones}
    zone_anchored = accounting.target_minutes is None and bool(
        set(allowed_zones) & zone_labels
    )

    if zone_anchored:
        candidates = []
        for zone in zones:
            if zone.label not in allowed_zones:
                continue
            for occurrence in scheduling.zone_occurrences(zone, (start_date, end_date), tz=tz):
                candidates.append(
                    {
                        "start": occurrence.start.isoformat(),
                        "end": occurrence.end.isoformat(),
                        "score": None,
                        "reasons": [f"zone-anchored: {zone.label}"],
                        "constraints_applied": [f"zone:{zone.label}"],
                    }
                )
        candidates.sort(key=lambda c: c["start"])
        return {
            "status": "success",
            "zone_anchored": True,
            "candidates": candidates,
            "remaining_target_minutes": None,
        }

    # Prior period, same length, immediately preceding this one — the
    # same rule instruction.md's placement paragraph states for when to
    # call review_habit_week before placing a new period. Uses
    # compute_habit_review, not review_habit_week itself: the latter
    # also emits A1.4 telemetry attributing the review to the model,
    # which never asked for this one — see compute_habit_review's own
    # docstring.
    period_length = end_date - start_date
    prior_from = start_date - period_length
    review_result = await habit_tools.compute_habit_review(
        tool_context, prior_from.isoformat(), start_date.isoformat()
    )
    prior_review = (
        _adapt_review(review_result["sessions"])
        if review_result.get("status") == "success"
        else []
    )
    known_habit_labels = frozenset(h["label"] for h in habits)

    resolved_min = min_minutes if min_minutes is not None else (
        accounting.session_min_minutes or _DEFAULT_MIN_MINUTES
    )
    resolved_max = max_minutes if max_minutes is not None else (
        accounting.session_max_minutes or _DEFAULT_MAX_MINUTES
    )
    if resolved_min > resolved_max:
        resolved_min, resolved_max = resolved_max, resolved_min
    resolved_min = max(1, resolved_min)
    resolved_max = max(resolved_min, resolved_max)

    busy = _adapt_busy_events(calendar_state["events"], tz=tz)
    free = scheduling.free_intervals(
        (start_date, end_date),
        tz=tz,
        zones=zones,
        sleep_schedule=sleep_schedule,
        busy=busy,
        allowed_zones=allowed_zones,
    )

    raw_candidates = _slice_candidates(free, resolved_min, resolved_max)
    scored = scheduling.score_candidates(
        raw_candidates,
        day_loads=_day_loads(busy),
        prior_review=prior_review,
        known_habit_labels=known_habit_labels,
    )

    constraints_applied = sorted(zone_labels - set(allowed_zones))
    if sleep_schedule is not None:
        constraints_applied.append("sleep_schedule")

    candidates_out = []
    for c in scored[:_MAX_CANDIDATES_RETURNED]:
        reasons = []
        if c.is_weekend:
            reasons.append("weekend")
        if c.day_load_minutes < _LIGHT_DAY_THRESHOLD_MINUTES:
            reasons.append("light day")
        if c.repeat_bump_reason:
            reasons.append(f"repeatedly bumped by {c.repeat_bump_reason}")
        candidates_out.append(
            {
                "start": c.interval.start.isoformat(),
                "end": c.interval.end.isoformat(),
                "score": c.score,
                "reasons": reasons,
                "constraints_applied": constraints_applied,
            }
        )

    return {
        "status": "success",
        "zone_anchored": False,
        "candidates": candidates_out,
        "remaining_target_minutes": accounting.remaining_minutes,
    }


async def get_available_slots(
    tool_context: ToolContext,
    date_from: str,
    date_to: str,
    habit_id: str,
    min_minutes: int | None = None,
    max_minutes: int | None = None,
) -> dict:
    """Ranked candidate time slots for placing a habit's next session(s),
    computed by the scheduling engine rather than derived in prose.

    **Not currently registered as a model-callable tool (A4.2) — see
    this module's own docstring for why.** This function is exercised
    directly by agent.py's shadow-comparison callback and by its own
    tests, not by the model. A future task (A4.3) may register it
    alongside instruction.md changes that explain how to use it.

    Args:
        date_from: Start date, inclusive, "YYYY-MM-DD".
        date_to: End date, exclusive, "YYYY-MM-DD".
        habit_id: Which tracked habit to find slots for (see list_habits).
        min_minutes: Shortest acceptable session length. Omit to use the
            habit's own goal-stated range (see create_habit/update_habit).
        max_minutes: Longest acceptable session length. Same default rule
            as min_minutes.

    Returns:
        dict with "status". On "success": "zone_anchored" is True for a
        habit whose allowed_zones names a zone with no target of its own
        (see instruction.md's placement paragraph) — its "candidates" are
        that zone's own occurrences, each carrying "start"/"end" only,
        with "score" null (there is nothing to rank; every occurrence is
        placed). Otherwise each candidate carries "start", "end", "score"
        (higher is better, candidates are pre-sorted best-first),
        "reasons" (e.g. "weekend", "light day", "repeatedly bumped by
        Standup" — human-readable, not exhaustive), and
        "constraints_applied" (the zone labels and/or "sleep_schedule"
        that shaped this computation). "remaining_target_minutes" is the
        habit's weekly target minus what's already placed in this range,
        or null for a zone-anchored habit or an unparseable goal. On
        "not_found", habit_id doesn't exist for this user. On
        "needs_auth"/"error", handle identically to get_calendar_events —
        the engine could not compute candidates, not that none exist.
    """
    user_id = tool_context.session.user_id
    try:
        return await _compute_candidates(
            tool_context, user_id, date_from, date_to, habit_id, min_minutes, max_minutes
        )
    except backend_client.NeedsAuth as exc:
        return {"status": "needs_auth", "connect_url": exc.connect_url, "message": exc.message}
    except domain_client.BACKEND_ERROR:
        logger.warning("get_available_slots backend call failed", exc_info=True)
        return {
            "status": "error",
            "error_message": "Could not compute candidate slots right now due to a backend error.",
        }


def _adapt_habit_tagged_sessions(
    events: list[dict],
) -> list[tuple[dict, scheduling.Interval]]:
    """Pairs each habit-tagged, timed event (see get_calendar_events) with
    its own Interval, for collisions_with. A plain event (no "habit_id")
    can't be the "conflict you create by learning something new" case in
    instruction.md's second placement paragraph — that's specifically
    about sessions *you* placed for a tracked habit — so it's excluded
    here rather than left for the caller to filter back out."""
    paired: list[tuple[dict, scheduling.Interval]] = []
    for event in events:
        if not event.get("habit_id"):
            continue
        start, end = event.get("start_time"), event.get("end_time")
        if not start or not end or "T" not in start or "T" not in end:
            continue
        try:
            start_dt, end_dt = datetime.fromisoformat(start), datetime.fromisoformat(end)
        except ValueError:
            continue
        if start_dt.tzinfo is None or end_dt.tzinfo is None or end_dt < start_dt:
            continue
        paired.append((event, scheduling.Interval(start_dt, end_dt)))
    return paired


async def _compute_zone_collisions(
    tool_context: ToolContext, user_id: str, zone_label: str, date_from: str, date_to: str
) -> dict:
    zones = await domain_client.list_zones(user_id)
    zone_raw = next((z for z in zones if z["label"] == zone_label), None)
    if zone_raw is None:
        return {"status": "not_found", "message": f"No zone named {zone_label!r}."}

    tz_name = await calendar_tool.resolve_reference_timezone(tool_context, user_id)
    if tz_name is None:
        return {
            "status": "needs_auth",
            "message": "No connected calendar to resolve a reference timezone from.",
        }
    tz = ZoneInfo(tz_name)

    calendar_state = await calendar_tool.get_calendar_events(tool_context, date_from, date_to)
    if calendar_state["status"] != "success":
        return calendar_state

    occurrences = scheduling.zone_occurrences(
        _adapt_zone(zone_raw), (date.fromisoformat(date_from), date.fromisoformat(date_to)), tz=tz
    )
    paired = _adapt_habit_tagged_sessions(calendar_state["events"])
    colliding = scheduling.collisions_with(occurrences, paired)

    return {"status": "success", "colliding_sessions": colliding}


async def find_zone_collisions(
    tool_context: ToolContext, zone_label: str, date_from: str, date_to: str
) -> dict:
    """Which already-placed habit sessions a zone's window now collides
    with, computed by the scheduling engine rather than by scanning
    get_calendar_events yourself.

    For the "conflict you create by learning something new" case in
    instruction.md's second placement paragraph — call this right after
    create_zone or update_zone, over at least the next 1-2 weeks, the
    same range you'd otherwise scan by hand. It covers a zone specifically;
    a changed profile preference or sleep schedule still needs the manual
    check described there.

    Args:
        zone_label: The zone to check, by its label (see create_zone,
            update_zone, list_zones).
        date_from: Start date, inclusive, "YYYY-MM-DD".
        date_to: End date, exclusive, "YYYY-MM-DD".

    Returns:
        dict with "status". On "success", "colliding_sessions" is every
        habit-tagged event (see get_calendar_events — same shape:
        event_id, calendar_id, title, start_time, end_time, habit_id)
        whose time falls inside one of the zone's occurrences in this
        range, ready to act on without a second lookup. An empty list
        means no collision, not that the check failed. On "not_found", no
        zone named zone_label exists for this user. On "needs_auth"/
        "error", handle the same as get_calendar_events — the check could
        not run, not that nothing collides.
    """
    user_id = tool_context.session.user_id
    try:
        return await _compute_zone_collisions(tool_context, user_id, zone_label, date_from, date_to)
    except backend_client.NeedsAuth as exc:
        return {"status": "needs_auth", "connect_url": exc.connect_url, "message": exc.message}
    except domain_client.BACKEND_ERROR:
        logger.warning("find_zone_collisions backend call failed", exc_info=True)
        return {
            "status": "error",
            "error_message": "Could not check for zone collisions right now due to a backend error.",
        }


async def log_shadow_comparison(
    tool_context: ToolContext, user_id: str, habit_id: str, event: dict
) -> None:
    """Best-effort, called from agent.py's after_tool_callback whenever
    add_calendar_event/update_calendar_event successfully places or moves
    a habit-tagged event — never from calendar_tool.py itself, which
    would create an import cycle (this module already imports
    calendar_tool for its calendar/timezone helpers).

    Computes what the engine would have suggested for this habit on the
    single calendar day the event actually landed on, and records how the
    real placement compares. A failure here must never affect the tool
    call whose result triggered it — see this function's own try/except.
    """
    try:
        start_time = event.get("start_time")
        if not start_time or "T" not in start_time:
            return  # an all-day event has no comparable time slot
        actual_start = datetime.fromisoformat(start_time)
        day = actual_start.date()

        result = await _compute_candidates(
            tool_context, user_id, day.isoformat(), (day + timedelta(days=1)).isoformat(),
            habit_id, None, None,
        )

        if result.get("status") != "success":
            comparison = {
                "habit_id": habit_id,
                "actual_start": start_time,
                "engine_status": result.get("status"),
            }
        else:
            candidates = result["candidates"]
            top = candidates[0] if candidates else None
            comparison = {
                "habit_id": habit_id,
                "actual_start": start_time,
                "engine_status": "success",
                "zone_anchored": result["zone_anchored"],
                "engine_candidate_count": len(candidates),
                "engine_top_candidate_start": top["start"] if top else None,
                "engine_top_candidate_score": top["score"] if top else None,
                "agrees_with_top_candidate": (
                    top is not None
                    and datetime.fromisoformat(top["start"]) == actual_start
                ),
            }

        existing = tool_context.state.get(SHADOW_COMPARISONS_STATE_KEY) or []
        tool_context.state[SHADOW_COMPARISONS_STATE_KEY] = [*existing, comparison]
    except Exception:
        logger.warning(
            "Failed to log schedule shadow comparison for habit_id=%s", habit_id, exc_info=True
        )
