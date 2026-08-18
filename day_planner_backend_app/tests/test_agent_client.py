"""Coverage of AgentClient.send_message's turn-record logging (A1.1).

The ADK event stream is the complete record of what the agent did in a
turn; until now it was walked once for visible text and thrown away. These
tests exercise the real event-walking logic directly (test_chat.py's
FakeAgentClient bypasses it entirely), against event dicts shaped like
google.genai.types.Content/Part/FunctionCall/FunctionResponse and
google.adk.events.Event — verified field names, not guessed.
"""

import json

import pytest

from app.services.agent_client import AgentClient


class FakeApp:
    """Stands in for the object AgentClient._get_app() would normally
    fetch over the network — installed directly on agent._app so no real
    Agent Engine call happens."""

    def __init__(self, events: list[dict]):
        self._events = events
        self._raise_error: Exception | None = None
        self.stream_calls: list[dict] = []

    async def async_get_session(self, *, user_id, session_id):
        return {"id": session_id}

    async def async_create_session(self, *, user_id):
        return {"id": "new-session"}

    async def async_stream_query(self, *, user_id, session_id, message):
        self.stream_calls.append(
            {"user_id": user_id, "session_id": session_id, "message": message}
        )
        for event in self._events:
            yield event
        if self._raise_error is not None:
            raise self._raise_error


def _agent_client(events, *, log_tool_args: bool = False) -> tuple[AgentClient, FakeApp]:
    client = AgentClient(
        project_id="test-proj",
        location="us-central1",
        reasoning_engine="projects/test-proj/locations/us-central1/reasoningEngines/1",
        log_tool_args=log_tool_args,
    )
    app = FakeApp(events)
    client._app = app
    return client, app


def _model_text_event(text: str) -> dict:
    return {"content": {"role": "model", "parts": [{"text": text}]}}


def _function_call_event(name: str, call_id: str, args: dict | None = None) -> dict:
    return {
        "content": {
            "role": "model",
            "parts": [{"function_call": {"id": call_id, "name": name, "args": args or {}}}],
        }
    }


def _function_response_event(name: str, call_id: str, response: dict) -> dict:
    return {
        "content": {
            "role": "user",
            "parts": [
                {"function_response": {"id": call_id, "name": name, "response": response}}
            ],
        }
    }


def _usage_event(prompt=100, candidates=20, thoughts=5, cached=0) -> dict:
    return {
        "usage_metadata": {
            "prompt_token_count": prompt,
            "candidates_token_count": candidates,
            "thoughts_token_count": thoughts,
            "cached_content_token_count": cached,
        }
    }


def _preload_ok_event(value: bool) -> dict:
    return {"actions": {"state_delta": {"day_planner:preload_ok": value}}}


def _habit_session_outcomes_event(outcomes: list[dict]) -> dict:
    return {"actions": {"state_delta": {"day_planner:habit_session_outcomes": outcomes}}}


def _one_record(caplog):
    records = [r for r in caplog.records if r.name == "day_planner.turn"]
    assert len(records) == 1, f"expected exactly one turn record, got {len(records)}"
    return json.loads(records[0].message)


async def test_reply_text_unchanged_by_turn_logging(caplog):
    events = [
        _usage_event(),
        _function_call_event("get_calendar_events", "c1"),
        _function_response_event("get_calendar_events", "c1", {"status": "success"}),
        _model_text_event("Here's your week."),
    ]
    client, app = _agent_client(events)

    with caplog.at_level("INFO", logger="day_planner.turn"):
        session_id, reply = await client.send_message(
            user_id="user-1", session_id="s1", message="plan my week"
        )

    assert session_id == "s1"
    assert reply == "Here's your week."


async def test_emits_exactly_one_record_per_turn(caplog):
    events = [_model_text_event("ok")]
    client, app = _agent_client(events)

    with caplog.at_level("INFO", logger="day_planner.turn"):
        await client.send_message(user_id="user-1", session_id="s1", message="hi")

    _one_record(caplog)  # raises if not exactly one


async def test_records_tool_call_name_and_status(caplog):
    events = [
        _function_call_event("add_calendar_event", "c1"),
        _function_response_event("add_calendar_event", "c1", {"status": "success"}),
    ]
    client, app = _agent_client(events)

    with caplog.at_level("INFO", logger="day_planner.turn"):
        await client.send_message(user_id="user-1", session_id="s1", message="book gym")

    record = _one_record(caplog)
    assert len(record["tool_calls"]) == 1
    call = record["tool_calls"][0]
    assert call["name"] == "add_calendar_event"
    assert call["status"] == "success"
    assert call["duration_ms"] is not None
    assert "args" not in call
    assert "retry_count" not in call


async def test_retry_count_surfaces_when_present(caplog):
    """A2.3: add_calendar_event includes retry_count in its own response
    (not state_delta) only when a transient failure had to be retried
    before an insert succeeded."""
    events = [
        _function_call_event("add_calendar_event", "c1"),
        _function_response_event(
            "add_calendar_event", "c1", {"status": "success", "retry_count": 2}
        ),
    ]
    client, app = _agent_client(events)

    with caplog.at_level("INFO", logger="day_planner.turn"):
        await client.send_message(user_id="user-1", session_id="s1", message="book gym")

    record = _one_record(caplog)
    assert record["tool_calls"][0]["retry_count"] == 2


async def test_tool_args_withheld_by_default(caplog):
    events = [
        _function_call_event("add_calendar_event", "c1", args={"summary": "Gym"}),
        _function_response_event("add_calendar_event", "c1", {"status": "success"}),
    ]
    client, app = _agent_client(events, log_tool_args=False)

    with caplog.at_level("INFO", logger="day_planner.turn"):
        await client.send_message(user_id="user-1", session_id="s1", message="book gym")

    record = _one_record(caplog)
    assert "args" not in record["tool_calls"][0]
    assert "Gym" not in caplog.text


async def test_tool_args_logged_in_diagnostic_mode(caplog):
    events = [
        _function_call_event("add_calendar_event", "c1", args={"summary": "Gym"}),
        _function_response_event("add_calendar_event", "c1", {"status": "success"}),
    ]
    client, app = _agent_client(events, log_tool_args=True)

    with caplog.at_level("INFO", logger="day_planner.turn"):
        await client.send_message(user_id="user-1", session_id="s1", message="book gym")

    record = _one_record(caplog)
    assert record["tool_calls"][0]["args"] == {"summary": "Gym"}


@pytest.mark.parametrize("tool_name", ["get_profile", "update_profile", "save_memory", "load_memory"])
async def test_memory_and_profile_tool_args_never_logged_even_in_diagnostic_mode(
    caplog, tool_name
):
    events = [
        _function_call_event(tool_name, "c1", args={"preferences": "no gym after 8pm"}),
        _function_response_event(tool_name, "c1", {"status": "success"}),
    ]
    client, app = _agent_client(events, log_tool_args=True)

    with caplog.at_level("INFO", logger="day_planner.turn"):
        await client.send_message(user_id="user-1", session_id="s1", message="remember this")

    record = _one_record(caplog)
    assert "args" not in record["tool_calls"][0]
    assert "no gym after 8pm" not in caplog.text


async def test_tool_response_payload_never_logged_even_in_diagnostic_mode(caplog):
    """Only "status" is ever kept from a tool's response — the rest of the
    payload (event titles, times, locations) never enters the log, for any
    tool, diagnostic mode or not."""
    events = [
        _function_call_event("get_calendar_events", "c1"),
        _function_response_event(
            "get_calendar_events",
            "c1",
            {"status": "success", "events": [{"title": "Therapy"}]},
        ),
    ]
    client, app = _agent_client(events, log_tool_args=True)

    with caplog.at_level("INFO", logger="day_planner.turn"):
        await client.send_message(user_id="user-1", session_id="s1", message="what's today")

    record = _one_record(caplog)
    assert record["tool_calls"][0]["status"] == "success"
    assert "Therapy" not in caplog.text


async def test_model_call_tokens_are_summed_across_events(caplog):
    events = [_usage_event(100, 20, 5), _usage_event(50, 10, 0)]
    client, app = _agent_client(events)

    with caplog.at_level("INFO", logger="day_planner.turn"):
        await client.send_message(user_id="user-1", session_id="s1", message="hi")

    record = _one_record(caplog)
    assert record["model_calls"] == 2
    assert record["input_tokens"] == 150
    assert record["output_tokens"] == 30
    assert record["thinking_tokens"] == 5
    assert record["cached_tokens"] == 0


async def test_cached_tokens_summed_across_events(caplog):
    """A2.5: cached_tokens comes off the same usage_metadata dict as the
    other token fields — Vertex AI's cachedContentTokenCount, reported
    whenever a model call's prefix hit Gemini's implicit context cache."""
    events = [_usage_event(cached=8000), _usage_event(cached=8200)]
    client, app = _agent_client(events)

    with caplog.at_level("INFO", logger="day_planner.turn"):
        await client.send_message(user_id="user-1", session_id="s1", message="hi")

    record = _one_record(caplog)
    assert record["cached_tokens"] == 16200


async def test_preload_ok_surfaces_from_state_delta(caplog):
    events = [_preload_ok_event(False), _model_text_event("ok")]
    client, app = _agent_client(events)

    with caplog.at_level("INFO", logger="day_planner.turn"):
        await client.send_message(user_id="user-1", session_id="s1", message="hi")

    record = _one_record(caplog)
    assert record["preload_ok"] is False


async def test_preload_ok_null_when_never_observed(caplog):
    events = [_model_text_event("ok")]
    client, app = _agent_client(events)

    with caplog.at_level("INFO", logger="day_planner.turn"):
        await client.send_message(user_id="user-1", session_id="s1", message="hi")

    record = _one_record(caplog)
    assert record["preload_ok"] is None


# ---------------------------------------------------------------------------
# habit_session_outcomes (A1.4)
# ---------------------------------------------------------------------------


async def test_habit_session_outcomes_surfaces_from_state_delta(caplog):
    outcomes = [
        {
            "habit_id": "h1",
            "session_status": "completed",
            "outcome": "kept",
            "hour_of_day": 7,
            "day_of_week": "tue",
            "zone_constrained": False,
            "source": "organic",
        }
    ]
    events = [_habit_session_outcomes_event(outcomes), _model_text_event("ok")]
    client, app = _agent_client(events)

    with caplog.at_level("INFO", logger="day_planner.turn"):
        await client.send_message(user_id="user-1", session_id="s1", message="how'd my week go")

    record = _one_record(caplog)
    assert record["habit_session_outcomes"] == outcomes


async def test_habit_session_outcomes_empty_when_never_observed(caplog):
    client, app = _agent_client([_model_text_event("ok")])

    with caplog.at_level("INFO", logger="day_planner.turn"):
        await client.send_message(user_id="user-1", session_id="s1", message="hi")

    record = _one_record(caplog)
    assert record["habit_session_outcomes"] == []


async def test_habit_session_outcomes_accumulate_across_multiple_calls_in_one_turn(caplog):
    """review_habit_week can run more than once in a turn (different
    periods or habits) — each call's own event must add to the total, not
    overwrite the previous call's outcomes."""
    first_call = [{"habit_id": "h1", "session_status": "completed", "outcome": "kept"}]
    second_call = [{"habit_id": "h2", "session_status": "pending", "outcome": "moved"}]
    events = [
        _habit_session_outcomes_event(first_call),
        _habit_session_outcomes_event(second_call),
        _model_text_event("ok"),
    ]
    client, app = _agent_client(events)

    with caplog.at_level("INFO", logger="day_planner.turn"):
        await client.send_message(user_id="user-1", session_id="s1", message="hi")

    record = _one_record(caplog)
    assert record["habit_session_outcomes"] == first_call + second_call


async def test_outcome_completed_on_success(caplog):
    client, app = _agent_client([_model_text_event("ok")])

    with caplog.at_level("INFO", logger="day_planner.turn"):
        await client.send_message(user_id="user-1", session_id="s1", message="hi")

    assert _one_record(caplog)["outcome"] == "completed"


async def test_outcome_errored_and_record_still_emitted_on_exception(caplog):
    client, app = _agent_client([_model_text_event("partial")])
    app._raise_error = RuntimeError("boom")

    with caplog.at_level("INFO", logger="day_planner.turn"):
        with pytest.raises(RuntimeError):
            await client.send_message(user_id="user-1", session_id="s1", message="hi")

    assert _one_record(caplog)["outcome"] == "errored"


async def test_outcome_timed_out_and_record_still_emitted(caplog):
    client, app = _agent_client([_model_text_event("partial")])
    app._raise_error = TimeoutError("deadline exceeded")

    with caplog.at_level("INFO", logger="day_planner.turn"):
        with pytest.raises(TimeoutError):
            await client.send_message(user_id="user-1", session_id="s1", message="hi")

    assert _one_record(caplog)["outcome"] == "timed_out"


async def test_open_call_with_no_response_is_recorded_as_incomplete(caplog):
    """A tool call whose function_response never arrives within the turn
    must not silently vanish from the record."""
    events = [_function_call_event("add_calendar_event", "c1")]
    client, app = _agent_client(events)

    with caplog.at_level("INFO", logger="day_planner.turn"):
        await client.send_message(user_id="user-1", session_id="s1", message="book gym")

    record = _one_record(caplog)
    assert len(record["tool_calls"]) == 1
    assert record["tool_calls"][0]["name"] == "add_calendar_event"
    assert record["tool_calls"][0]["status"] is None


async def test_user_id_is_hashed_not_raw(caplog):
    client, app = _agent_client([_model_text_event("ok")])

    with caplog.at_level("INFO", logger="day_planner.turn"):
        await client.send_message(user_id="a-very-identifiable-user-id", session_id="s1", message="hi")

    record = _one_record(caplog)
    assert record["user_ref"] != "a-very-identifiable-user-id"
    assert "a-very-identifiable-user-id" not in caplog.text


# ---------------------------------------------------------------------------
# Loop detection (A1.3)
# ---------------------------------------------------------------------------


def _repeated_call(name: str, args: dict, n: int) -> list[dict]:
    events = []
    for i in range(n):
        call_id = f"{name}-{i}"
        events.append(_function_call_event(name, call_id, args))
        events.append(_function_response_event(name, call_id, {"status": "success"}))
    return events


async def test_loop_detected_at_three_identical_calls(caplog):
    events = _repeated_call("add_calendar_event", {"summary": "Gym"}, 3)
    client, app = _agent_client(events)

    with caplog.at_level("INFO", logger="day_planner.turn"):
        await client.send_message(user_id="user-1", session_id="s1", message="book gym")

    record = _one_record(caplog)
    assert record["loop_detected"] is True


async def test_loop_not_detected_below_threshold(caplog):
    events = _repeated_call("add_calendar_event", {"summary": "Gym"}, 2)
    client, app = _agent_client(events)

    with caplog.at_level("INFO", logger="day_planner.turn"):
        await client.send_message(user_id="user-1", session_id="s1", message="book gym")

    record = _one_record(caplog)
    assert record["loop_detected"] is False


async def test_loop_not_detected_when_arguments_differ(caplog):
    """Three calls to the same tool is not itself a loop — placing three
    different habit sessions across a week looks exactly like this and
    must not alert."""
    events = [
        _function_call_event("add_calendar_event", "c1", {"summary": "Gym Mon"}),
        _function_response_event("add_calendar_event", "c1", {"status": "success"}),
        _function_call_event("add_calendar_event", "c2", {"summary": "Gym Wed"}),
        _function_response_event("add_calendar_event", "c2", {"status": "success"}),
        _function_call_event("add_calendar_event", "c3", {"summary": "Gym Fri"}),
        _function_response_event("add_calendar_event", "c3", {"status": "success"}),
    ]
    client, app = _agent_client(events)

    with caplog.at_level("INFO", logger="day_planner.turn"):
        await client.send_message(user_id="user-1", session_id="s1", message="plan my gym sessions")

    record = _one_record(caplog)
    assert record["loop_detected"] is False


async def test_loop_not_detected_when_tool_names_differ(caplog):
    events = _repeated_call("get_calendar_events", {}, 1) + _repeated_call(
        "list_zones", {}, 1
    ) + _repeated_call("list_habits", {}, 1)
    client, app = _agent_client(events)

    with caplog.at_level("INFO", logger="day_planner.turn"):
        await client.send_message(user_id="user-1", session_id="s1", message="hi")

    record = _one_record(caplog)
    assert record["loop_detected"] is False


async def test_loop_detected_uses_warning_severity(caplog):
    events = _repeated_call("add_calendar_event", {"summary": "Gym"}, 3)
    client, app = _agent_client(events)

    with caplog.at_level("INFO", logger="day_planner.turn"):
        await client.send_message(user_id="user-1", session_id="s1", message="book gym")

    turn_records = [r for r in caplog.records if r.name == "day_planner.turn"]
    assert len(turn_records) == 1
    assert turn_records[0].levelname == "WARNING"


async def test_non_loop_turn_uses_info_severity(caplog):
    client, app = _agent_client([_model_text_event("ok")])

    with caplog.at_level("INFO", logger="day_planner.turn"):
        await client.send_message(user_id="user-1", session_id="s1", message="hi")

    turn_records = [r for r in caplog.records if r.name == "day_planner.turn"]
    assert turn_records[0].levelname == "INFO"


async def test_args_fingerprint_present_and_stable_even_without_diagnostic_mode(caplog):
    """Loop detection must work even with log_tool_args=False (the
    default) — the fingerprint is computed unconditionally and never
    exposes the raw values."""
    events = _repeated_call("add_calendar_event", {"summary": "Gym"}, 3)
    client, app = _agent_client(events, log_tool_args=False)

    with caplog.at_level("INFO", logger="day_planner.turn"):
        await client.send_message(user_id="user-1", session_id="s1", message="book gym")

    record = _one_record(caplog)
    fingerprints = {c["args_fingerprint"] for c in record["tool_calls"]}
    assert len(fingerprints) == 1
    assert "args" not in record["tool_calls"][0]
    assert "Gym" not in caplog.text


async def test_args_fingerprint_differs_for_different_arguments(caplog):
    events = [
        _function_call_event("add_calendar_event", "c1", {"summary": "Gym"}),
        _function_response_event("add_calendar_event", "c1", {"status": "success"}),
        _function_call_event("add_calendar_event", "c2", {"summary": "Dentist"}),
        _function_response_event("add_calendar_event", "c2", {"status": "success"}),
    ]
    client, app = _agent_client(events)

    with caplog.at_level("INFO", logger="day_planner.turn"):
        await client.send_message(user_id="user-1", session_id="s1", message="hi")

    record = _one_record(caplog)
    fingerprints = [c["args_fingerprint"] for c in record["tool_calls"]]
    assert fingerprints[0] != fingerprints[1]
