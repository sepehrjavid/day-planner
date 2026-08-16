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
"status" field is kept, which every tool in this codebase already returns
as its status contract.
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

# Tools whose call arguments must never be logged even in diagnostic mode.
# Response payloads are never logged for any tool (see module docstring),
# so this only governs the args side.
_NEVER_LOG_ARGS_FOR = frozenset({"get_profile", "update_profile", "save_memory", "load_memory"})


def hash_user_id(user_id: str) -> str:
    """One-way, so a raw user_id never appears in a log line."""
    return hashlib.sha256(user_id.encode()).hexdigest()[:16]


@dataclass
class _ToolCall:
    name: str
    started_at: float
    args: dict | None = None
    duration_ms: float | None = None
    status: object | None = None


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
    preload_ok: bool | None = field(default=None, init=False)

    def observe(self, event: dict) -> None:
        """Call once per event yielded by async_stream_query, in order."""
        usage = event.get("usage_metadata")
        if usage:
            self.model_calls += 1
            self.input_tokens += usage.get("prompt_token_count") or 0
            self.output_tokens += usage.get("candidates_token_count") or 0
            self.thinking_tokens += usage.get("thoughts_token_count") or 0

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

    def _start_call(self, call: dict) -> None:
        name = call.get("name", "unknown")
        call_id = call.get("id") or f"{name}:{len(self._open_calls) + len(self._finished_calls)}"
        entry = _ToolCall(name=name, started_at=time.monotonic())
        if self.log_tool_args and name not in _NEVER_LOG_ARGS_FOR:
            entry.args = call.get("args")
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
                entry = _ToolCall(name=name, started_at=time.monotonic())
        entry.duration_ms = (time.monotonic() - entry.started_at) * 1000
        entry.status = (response.get("response") or {}).get("status")
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

        record = {
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "user_ref": hash_user_id(self.user_id),
            "tool_calls": [
                {
                    "name": c.name,
                    "duration_ms": round(c.duration_ms, 1) if c.duration_ms is not None else None,
                    "status": c.status,
                    **({"args": c.args} if c.args is not None else {}),
                }
                for c in self._finished_calls
            ],
            "model_calls": self.model_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "thinking_tokens": self.thinking_tokens,
            "preload_ok": self.preload_ok,
            "outcome": outcome,
            "wall_ms": round(wall_ms, 1),
        }
        logger.info(json.dumps(record))
