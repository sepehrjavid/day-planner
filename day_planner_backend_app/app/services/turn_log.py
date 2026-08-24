"""Turns Agent Engine's per-event stream into one structured log line per
turn, instead of discarding it once the visible reply text is pulled out
(see agent_client.py's send_message/_visible_text). When a user reports
"it booked my gym during work hours" this is the only way to reconstruct
which tools fired, in what order, and what they returned.

Redaction is the default. Tool *names* and returned *statuses* are always
logged — they're what you need to diagnose a loop or a failure class. Tool
*arguments* carry event titles, times, and locations, so they're only
logged when the caller opts into diagnostic mode (AgentClient's
log_tool_args, off by default) and never for a tool on
_NEVER_LOG_ARGS_FOR — profile and memory payloads must not appear in logs
at any level, diagnostic mode or not (see A0.6, which removed exactly this
kind of leak once already). Tool *response* payloads are never logged at
all here, for any tool, diagnostic mode or not — only the response's
"status" field, and (A2.3) its "retry_count" field when present, are
kept. Both are call metadata, never event/profile/memory content, so
retry_count carries the same redaction guarantee status already did.

Loop detection (A1.3) needs to compare call *arguments* for equality — the
most expensive class of agent bug is the same tool called with the same
arguments repeatedly — but must do that without ever logging the raw
values, even with redaction on. Each call gets a one-way fingerprint
(_fingerprint_args) computed unconditionally, independent of
log_tool_args; three or more calls in a turn sharing both a tool name and
a fingerprint sets loop_detected, which terraform/monitoring.tf alerts on.

Habit session completion/survival telemetry (A1.4) rides the same
state_delta channel preload_ok already established: review_habit_week
writes its per-session outcomes into tool_context.state rather than its
return value, so it reaches habit_session_outcomes here without adding a
single token to what the model sees — see habit_tools.py's
_emit_habit_session_telemetry and turn_log_queries.sql for the queries
this feeds.

schedule_shadow_comparisons (A4.2) is the same channel again:
day_planner_agent/scheduling_tool.py's log_shadow_comparison — invoked
from agent.py's after_tool_callback whenever a real
add_calendar_event/update_calendar_event places or moves a habit-tagged
session — records what the scheduling engine (day_planner_agent/
scheduling/, A4.1) would have suggested for that day and whether the
model's real placement agrees, entirely in shadow mode: nothing in
instruction.md tells the model to use the engine, and nothing here gates
placement on it. This is the data A4.2's own acceptance criterion
("disagreement rate is measured and reviewed") depends on existing at
all — reviewing it is a separate, later step once enough of it has
accumulated from real usage.

retry_count itself comes straight off the tool's own return value (not
state_delta, unlike preload_ok/habit_session_outcomes above) — set by
calendar_tool.py's add_calendar_event only when a transient failure had
to be retried before an insert succeeded.

cached_tokens (A2.5) comes off the same usage_metadata dict as
input_tokens/output_tokens/thinking_tokens above — Vertex AI reports it
as cached_content_token_count whenever a model call's prefix hit Gemini's
implicit context cache. It only means anything once agent.py's
instruction is structured so the static rules+tools prefix is actually
byte-identical across calls (see instruction.md and agent._build_instruction
— the volatile today/profile/zones injections were moved to the end of
the prompt specifically so this prefix can be recognized as a cache hit
at all); this field is what makes that measurable, alongside
turn_log_queries.sql.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import time
from dataclasses import dataclass, field

logger = logging.getLogger("day_planner.turn")
logger.setLevel(logging.INFO)
# Bypass main.py's logging.basicConfig() formatter (adds a
# "INFO:day_planner.turn:" prefix) and don't propagate to the root logger,
# which would emit that prefixed copy a second time. Cloud Logging on Cloud
# Run auto-parses a stdout line into structured jsonPayload only when the
# *entire* line is valid JSON — a level/name prefix breaks that, and A1.2's
# BigQuery sink depends on it (see terraform/bigquery.tf's sink filter,
# which matches on jsonPayload.turn_id). Explicit stream=stdout: a bare
# StreamHandler() defaults to stderr, which Cloud Run captures and parses
# just as well, but stdout is the deliberate convention here so this
# doesn't silently depend on Python's default.
logger.propagate = False
if not logger.handlers:
    _handler = logging.StreamHandler(stream=sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_handler)

# Preload state written by day_planner_agent's before_agent_callbacks (see
# day_planner_agent/agent.py's _PRELOAD_OK_KEY) — surfaces on whichever
# event's actions.state_delta first carries it, normally the first event
# of the turn.
_PRELOAD_OK_STATE_KEY = "day_planner:preload_ok"

# Per-session completion/survival telemetry written by
# day_planner_agent/habit_tools.py's review_habit_week (A1.4) — same
# state_delta channel as preload_ok above, chosen specifically so this
# data reaches here without adding a single token to what
# review_habit_week returns to the model.
_HABIT_SESSION_OUTCOMES_STATE_KEY = "day_planner:habit_session_outcomes"

# Shadow-mode engine-vs-model comparisons written by
# day_planner_agent/scheduling_tool.py's log_shadow_comparison (A4.2) —
# same state_delta channel, same reason: zero-token telemetry.
_SCHEDULE_SHADOW_COMPARISONS_STATE_KEY = "day_planner:schedule_shadow_comparisons"

# Tools whose call arguments must never be logged even in diagnostic mode.
# Response payloads are never logged for any tool (see module docstring),
# so this only governs the args side.
_NEVER_LOG_ARGS_FOR = frozenset({"get_profile", "update_profile", "save_memory", "load_memory"})

# Three or more calls to the same tool with the same arguments in one turn
# is the loop-detector threshold — see terraform/monitoring.tf.
_LOOP_THRESHOLD = 3


def hash_user_id(user_id: str) -> str:
    """One-way, so a raw user_id never appears in a log line."""
    return hashlib.sha256(user_id.encode()).hexdigest()[:16]


def _fingerprint_args(args: dict | None) -> str:
    """One-way and order-independent (sort_keys), so equal argument dicts
    always fingerprint the same regardless of key order — computed
    unconditionally for loop detection, never reversible back to the
    actual values."""
    canonical = json.dumps(args or {}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


@dataclass
class _ToolCall:
    name: str
    started_at: float
    args_fingerprint: str
    args: dict | None = None
    duration_ms: float | None = None
    status: object | None = None
    retry_count: int | None = None


@dataclass
class TurnRecorder:
    """Accumulates one turn's record while the caller walks the ADK event
    stream, then emits it as a single structured log line via emit().

    One instance per turn — not reused across turns or shared across
    requests.
    """

    turn_id: str
    session_id: str | None
    user_id: str
    log_tool_args: bool = False

    _start: float = field(default_factory=time.monotonic, init=False)
    _open_calls: dict[str, _ToolCall] = field(default_factory=dict, init=False)
    _finished_calls: list[_ToolCall] = field(default_factory=list, init=False)
    model_calls: int = field(default=0, init=False)
    input_tokens: int = field(default=0, init=False)
    output_tokens: int = field(default=0, init=False)
    thinking_tokens: int = field(default=0, init=False)
    cached_tokens: int = field(default=0, init=False)
    preload_ok: bool | None = field(default=None, init=False)
    habit_session_outcomes: list = field(default_factory=list, init=False)
    schedule_shadow_comparisons: list = field(default_factory=list, init=False)

    def observe(self, event: dict) -> None:
        """Call once per event yielded by async_stream_query, in order."""
        usage = event.get("usage_metadata")
        if usage:
            self.model_calls += 1
            self.input_tokens += usage.get("prompt_token_count") or 0
            self.output_tokens += usage.get("candidates_token_count") or 0
            self.thinking_tokens += usage.get("thoughts_token_count") or 0
            self.cached_tokens += usage.get("cached_content_token_count") or 0

        content = event.get("content") or {}
        for part in content.get("parts") or []:
            call = part.get("function_call")
            if call:
                self._start_call(call)
            response = part.get("function_response")
            if response:
                self._finish_call(response)

        state_delta = (event.get("actions") or {}).get("state_delta") or {}
        if _PRELOAD_OK_STATE_KEY in state_delta:
            self.preload_ok = state_delta[_PRELOAD_OK_STATE_KEY]
        if _HABIT_SESSION_OUTCOMES_STATE_KEY in state_delta:
            # Extended, not overwritten: review_habit_week can run more
            # than once in a turn (different periods/habits), each call
            # its own event with its own state_delta entry for this key —
            # unlike preload_ok, which is only ever set once per session.
            self.habit_session_outcomes.extend(
                state_delta[_HABIT_SESSION_OUTCOMES_STATE_KEY] or []
            )
        if _SCHEDULE_SHADOW_COMPARISONS_STATE_KEY in state_delta:
            # log_shadow_comparison itself appends to the full list on
            # every habit-tagged placement in the turn (see its own
            # docstring), so each event's state_delta already carries
            # the whole list-so-far, not just its own new entry —
            # replacing (not extending) this accumulator each time is
            # what keeps a turn with several placements from
            # over-counting the earlier ones.
            self.schedule_shadow_comparisons = (
                state_delta[_SCHEDULE_SHADOW_COMPARISONS_STATE_KEY] or []
            )

    def _start_call(self, call: dict) -> None:
        name = call.get("name", "unknown")
        call_id = call.get("id") or f"{name}:{len(self._open_calls) + len(self._finished_calls)}"
        args = call.get("args")
        entry = _ToolCall(
            name=name, started_at=time.monotonic(), args_fingerprint=_fingerprint_args(args)
        )
        if self.log_tool_args and name not in _NEVER_LOG_ARGS_FOR:
            entry.args = args
        self._open_calls[call_id] = entry

    def _finish_call(self, response: dict) -> None:
        call_id = response.get("id")
        entry = self._open_calls.pop(call_id, None) if call_id else None
        if entry is None:
            # No id on this event shape, or it didn't match a call we saw —
            # fall back to the oldest still-open call of the same name so a
            # status is still recorded rather than lost.
            name = response.get("name", "unknown")
            for candidate_id, candidate in self._open_calls.items():
                if candidate.name == name:
                    entry = self._open_calls.pop(candidate_id)
                    break
            if entry is None:
                # No corresponding function_call was ever observed for this
                # response (unexpected event ordering) — args are unknown,
                # so this fingerprints as "no args" rather than being left
                # unset, at the cost of a rare false loop-match if this
                # happens 3+ times in one turn.
                entry = _ToolCall(
                    name=name, started_at=time.monotonic(), args_fingerprint=_fingerprint_args(None)
                )
        entry.duration_ms = (time.monotonic() - entry.started_at) * 1000
        payload = response.get("response") or {}
        entry.status = payload.get("status")
        entry.retry_count = payload.get("retry_count")
        self._finished_calls.append(entry)

    def emit(self, *, outcome: str) -> None:
        """Call exactly once, when the turn ends (success, error, or
        timeout) — logs the accumulated record as one JSON line."""
        wall_ms = (time.monotonic() - self._start) * 1000

        # A call that never got a matching function_response genuinely
        # didn't complete within the turn — record it as such instead of
        # silently dropping it from the record.
        for entry in self._open_calls.values():
            entry.duration_ms = (time.monotonic() - entry.started_at) * 1000
            self._finished_calls.append(entry)
        self._open_calls.clear()

        loop_detected = self._detect_loop()

        record = {
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "user_ref": hash_user_id(self.user_id),
            "tool_calls": [
                {
                    "name": c.name,
                    "args_fingerprint": c.args_fingerprint,
                    "duration_ms": round(c.duration_ms, 1) if c.duration_ms is not None else None,
                    "status": c.status,
                    **({"args": c.args} if c.args is not None else {}),
                    **({"retry_count": c.retry_count} if c.retry_count is not None else {}),
                }
                for c in self._finished_calls
            ],
            "model_calls": self.model_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "thinking_tokens": self.thinking_tokens,
            "cached_tokens": self.cached_tokens,
            "preload_ok": self.preload_ok,
            "outcome": outcome,
            "wall_ms": round(wall_ms, 1),
            "loop_detected": loop_detected,
            "habit_session_outcomes": self.habit_session_outcomes,
            "schedule_shadow_comparisons": self.schedule_shadow_comparisons,
        }
        # WARNING severity for a detected loop makes it visually distinct
        # in Cloud Logging on top of the boolean field itself — the
        # boolean is what terraform/monitoring.tf's log-based metric
        # actually filters on, so this is belt and suspenders, not load
        # bearing.
        (logger.warning if loop_detected else logger.info)(json.dumps(record))

    def _detect_loop(self) -> bool:
        """True if the same tool was called with the same arguments (by
        fingerprint) _LOOP_THRESHOLD times or more anywhere in this turn."""
        counts: dict[tuple[str, str], int] = {}
        for c in self._finished_calls:
            key = (c.name, c.args_fingerprint)
            counts[key] = counts.get(key, 0) + 1
            if counts[key] >= _LOOP_THRESHOLD:
                return True
        return False
