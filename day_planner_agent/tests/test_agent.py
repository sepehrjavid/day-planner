"""Verifies the preload callbacks' fail-closed behaviour (A0.2): a
transient backend failure must not be indistinguishable from "the user
genuinely has none of this data yet" — the flag that gates a one-time
preload must only latch on success, so a later turn retries, and
_build_instruction must say something different for the two cases.

There were previously zero tests covering these callbacks.
"""

from types import SimpleNamespace

from day_planner_agent import agent


class FakeCallbackContext:
    """The only surface _preload_profile/_preload_zones touch on a real
    CallbackContext: .state (dict-like), .session.user_id, and
    ._invocation_context.memory_service (memory_tools.get_profile's own
    dependency, passed straight through)."""

    def __init__(self, user_id: str = "user-1") -> None:
        self.state: dict = {}
        self.session = SimpleNamespace(user_id=user_id)
        self._invocation_context = SimpleNamespace(memory_service=SimpleNamespace())


class FakeReadonlyContext:
    def __init__(self, state: dict) -> None:
        self.state = state


# ---------------------------------------------------------------------------
# _preload_profile
# ---------------------------------------------------------------------------


async def test_preload_profile_success_sets_flag_and_data(monkeypatch):
    async def fake_get_profile(ctx):
        return {"status": "success", "profile": {"foo": "bar"}}

    monkeypatch.setattr(agent, "get_profile", fake_get_profile)
    ctx = FakeCallbackContext()

    await agent._preload_profile(ctx)

    assert ctx.state[agent._PROFILE_PRELOADED_KEY] is True
    assert ctx.state[agent._PROFILE_PRELOAD_FAILED_KEY] is False
    assert ctx.state[agent._PRELOADED_PROFILE_KEY] == {"foo": "bar"}
    assert ctx.state[agent._PRELOAD_OK_KEY] is True


async def test_preload_profile_genuinely_empty_sets_flag_no_data(monkeypatch):
    async def fake_get_profile(ctx):
        return {"status": "success", "profile": {}}

    monkeypatch.setattr(agent, "get_profile", fake_get_profile)
    ctx = FakeCallbackContext()

    await agent._preload_profile(ctx)

    assert ctx.state[agent._PROFILE_PRELOADED_KEY] is True
    assert ctx.state[agent._PROFILE_PRELOAD_FAILED_KEY] is False
    assert agent._PRELOADED_PROFILE_KEY not in ctx.state
    assert ctx.state[agent._PRELOAD_OK_KEY] is True


async def test_preload_profile_exception_leaves_flag_unset_and_marks_failed(monkeypatch):
    async def fake_get_profile(ctx):
        raise RuntimeError("boom")

    monkeypatch.setattr(agent, "get_profile", fake_get_profile)
    ctx = FakeCallbackContext()

    await agent._preload_profile(ctx)

    assert agent._PROFILE_PRELOADED_KEY not in ctx.state
    assert ctx.state[agent._PROFILE_PRELOAD_FAILED_KEY] is True
    assert agent._PRELOADED_PROFILE_KEY not in ctx.state
    assert ctx.state[agent._PRELOAD_OK_KEY] is False


async def test_preload_profile_error_status_leaves_flag_unset_and_marks_failed(monkeypatch):
    async def fake_get_profile(ctx):
        return {"status": "error", "message": "Memory Bank is not configured.", "profile": {}}

    monkeypatch.setattr(agent, "get_profile", fake_get_profile)
    ctx = FakeCallbackContext()

    await agent._preload_profile(ctx)

    assert agent._PROFILE_PRELOADED_KEY not in ctx.state
    assert ctx.state[agent._PROFILE_PRELOAD_FAILED_KEY] is True


async def test_preload_profile_is_a_noop_once_already_preloaded(monkeypatch):
    calls = []

    async def fake_get_profile(ctx):
        calls.append(1)
        return {"status": "success", "profile": {}}

    monkeypatch.setattr(agent, "get_profile", fake_get_profile)
    ctx = FakeCallbackContext()
    ctx.state[agent._PROFILE_PRELOADED_KEY] = True

    await agent._preload_profile(ctx)

    assert calls == []


async def test_preload_profile_retries_after_a_previous_failure(monkeypatch):
    """The core A0.2 acceptance criterion: a failing fetch must not latch
    the preloaded flag, so the very next turn's callback tries again."""
    calls = []

    async def fake_get_profile(ctx):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return {"status": "success", "profile": {"foo": "bar"}}

    monkeypatch.setattr(agent, "get_profile", fake_get_profile)
    ctx = FakeCallbackContext()

    await agent._preload_profile(ctx)
    assert ctx.state[agent._PROFILE_PRELOAD_FAILED_KEY] is True

    await agent._preload_profile(ctx)
    assert ctx.state[agent._PROFILE_PRELOADED_KEY] is True
    assert ctx.state[agent._PROFILE_PRELOAD_FAILED_KEY] is False
    assert ctx.state[agent._PRELOADED_PROFILE_KEY] == {"foo": "bar"}
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# _preload_zones
# ---------------------------------------------------------------------------


async def test_preload_zones_success_sets_flag_and_data(monkeypatch):
    async def fake_list_zones(ctx):
        return {"status": "success", "zones": [{"label": "Work"}]}

    async def fake_get_sleep_schedule(ctx):
        return {"status": "success", "exists": True, "schedule": {"sleep_time": "23:00"}}

    monkeypatch.setattr(agent, "list_zones", fake_list_zones)
    monkeypatch.setattr(agent, "get_sleep_schedule", fake_get_sleep_schedule)
    ctx = FakeCallbackContext()

    await agent._preload_zones(ctx)

    assert ctx.state[agent._ZONES_PRELOADED_KEY] is True
    assert ctx.state[agent._ZONES_PRELOAD_FAILED_KEY] is False
    assert ctx.state[agent._PRELOADED_ZONES_KEY] == [{"label": "Work"}]
    assert ctx.state[agent._PRELOADED_SLEEP_SCHEDULE_KEY] == {"sleep_time": "23:00"}
    assert ctx.state[agent._PRELOAD_OK_KEY] is True


async def test_preload_zones_genuinely_empty_sets_flag_no_data(monkeypatch):
    async def fake_list_zones(ctx):
        return {"status": "success", "zones": []}

    async def fake_get_sleep_schedule(ctx):
        return {"status": "success", "exists": False}

    monkeypatch.setattr(agent, "list_zones", fake_list_zones)
    monkeypatch.setattr(agent, "get_sleep_schedule", fake_get_sleep_schedule)
    ctx = FakeCallbackContext()

    await agent._preload_zones(ctx)

    assert ctx.state[agent._ZONES_PRELOADED_KEY] is True
    assert ctx.state[agent._ZONES_PRELOAD_FAILED_KEY] is False
    assert agent._PRELOADED_ZONES_KEY not in ctx.state
    assert agent._PRELOADED_SLEEP_SCHEDULE_KEY not in ctx.state


async def test_preload_zones_exception_leaves_flag_unset_and_marks_failed(monkeypatch):
    async def fake_list_zones(ctx):
        raise RuntimeError("backend down")

    async def fake_get_sleep_schedule(ctx):
        raise AssertionError("must not be called if list_zones already failed")

    monkeypatch.setattr(agent, "list_zones", fake_list_zones)
    monkeypatch.setattr(agent, "get_sleep_schedule", fake_get_sleep_schedule)
    ctx = FakeCallbackContext()

    await agent._preload_zones(ctx)

    assert agent._ZONES_PRELOADED_KEY not in ctx.state
    assert ctx.state[agent._ZONES_PRELOAD_FAILED_KEY] is True
    assert ctx.state[agent._PRELOAD_OK_KEY] is False


async def test_preload_zones_sleep_schedule_failure_marks_whole_section_failed(monkeypatch):
    """Even if list_zones succeeds, a failure on the sleep-schedule half
    must still mark the combined zones/sleep guardrail section as failed —
    the agent must not treat a half-loaded state as fully loaded."""

    async def fake_list_zones(ctx):
        return {"status": "success", "zones": []}

    async def fake_get_sleep_schedule(ctx):
        return {"status": "error"}

    monkeypatch.setattr(agent, "list_zones", fake_list_zones)
    monkeypatch.setattr(agent, "get_sleep_schedule", fake_get_sleep_schedule)
    ctx = FakeCallbackContext()

    await agent._preload_zones(ctx)

    assert agent._ZONES_PRELOADED_KEY not in ctx.state
    assert ctx.state[agent._ZONES_PRELOAD_FAILED_KEY] is True


async def test_preload_zones_retries_after_a_previous_failure(monkeypatch):
    calls = []

    async def fake_list_zones(ctx):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return {"status": "success", "zones": [{"label": "Work"}]}

    async def fake_get_sleep_schedule(ctx):
        return {"status": "success", "exists": False}

    monkeypatch.setattr(agent, "list_zones", fake_list_zones)
    monkeypatch.setattr(agent, "get_sleep_schedule", fake_get_sleep_schedule)
    ctx = FakeCallbackContext()

    await agent._preload_zones(ctx)
    assert ctx.state[agent._ZONES_PRELOAD_FAILED_KEY] is True

    await agent._preload_zones(ctx)
    assert ctx.state[agent._ZONES_PRELOADED_KEY] is True
    assert ctx.state[agent._ZONES_PRELOAD_FAILED_KEY] is False
    assert ctx.state[agent._PRELOADED_ZONES_KEY] == [{"label": "Work"}]
    assert len(calls) == 2


async def test_preload_zones_is_a_noop_once_already_preloaded(monkeypatch):
    calls = []

    async def fake_list_zones(ctx):
        calls.append(1)
        return {"status": "success", "zones": []}

    monkeypatch.setattr(agent, "list_zones", fake_list_zones)
    ctx = FakeCallbackContext()
    ctx.state[agent._ZONES_PRELOADED_KEY] = True

    await agent._preload_zones(ctx)

    assert calls == []


# ---------------------------------------------------------------------------
# preload_ok combines both callbacks (for A1.1)
# ---------------------------------------------------------------------------


async def test_preload_ok_false_if_either_preload_failed(monkeypatch):
    async def failing_get_profile(ctx):
        raise RuntimeError("boom")

    async def ok_list_zones(ctx):
        return {"status": "success", "zones": []}

    async def ok_get_sleep_schedule(ctx):
        return {"status": "success", "exists": False}

    monkeypatch.setattr(agent, "get_profile", failing_get_profile)
    monkeypatch.setattr(agent, "list_zones", ok_list_zones)
    monkeypatch.setattr(agent, "get_sleep_schedule", ok_get_sleep_schedule)
    ctx = FakeCallbackContext()

    await agent._preload_profile(ctx)
    await agent._preload_zones(ctx)

    assert ctx.state[agent._PRELOAD_OK_KEY] is False


async def test_preload_ok_true_if_both_preloads_succeed(monkeypatch):
    async def ok_get_profile(ctx):
        return {"status": "success", "profile": {}}

    async def ok_list_zones(ctx):
        return {"status": "success", "zones": []}

    async def ok_get_sleep_schedule(ctx):
        return {"status": "success", "exists": False}

    monkeypatch.setattr(agent, "get_profile", ok_get_profile)
    monkeypatch.setattr(agent, "list_zones", ok_list_zones)
    monkeypatch.setattr(agent, "get_sleep_schedule", ok_get_sleep_schedule)
    ctx = FakeCallbackContext()

    await agent._preload_profile(ctx)
    await agent._preload_zones(ctx)

    assert ctx.state[agent._PRELOAD_OK_KEY] is True


# ---------------------------------------------------------------------------
# _build_instruction: three-way section text
# ---------------------------------------------------------------------------


def test_build_instruction_profile_loaded_with_data():
    ctx = FakeReadonlyContext({agent._PRELOADED_PROFILE_KEY: {"foo": "bar"}})
    text = agent._build_instruction(ctx)
    assert "already loaded for this session: {'foo': 'bar'}" in text


def test_build_instruction_profile_genuinely_empty():
    ctx = FakeReadonlyContext({})
    text = agent._build_instruction(ctx)
    assert "No standing preferences are on file for this user yet." in text


def test_build_instruction_profile_fetch_failed():
    ctx = FakeReadonlyContext({agent._PROFILE_PRELOAD_FAILED_KEY: True})
    text = agent._build_instruction(ctx)
    assert "could not be loaded for this session due to a backend error" in text
    assert "Do not tell the user their preferences are unknown or missing" in text
    assert "No standing preferences are on file" not in text


def test_build_instruction_zones_loaded_with_data():
    ctx = FakeReadonlyContext({agent._PRELOADED_ZONES_KEY: [{"label": "Work"}]})
    text = agent._build_instruction(ctx)
    assert "standing day zones, already loaded for this session" in text


def test_build_instruction_zones_genuinely_empty():
    ctx = FakeReadonlyContext({})
    text = agent._build_instruction(ctx)
    assert "No day zones or sleep schedule are on file for this user yet." in text


def test_build_instruction_zones_fetch_failed():
    ctx = FakeReadonlyContext({agent._ZONES_PRELOAD_FAILED_KEY: True})
    text = agent._build_instruction(ctx)
    assert "Day zones and the sleep schedule could not be loaded" in text
    assert "hard constraints" in text
    assert "No day zones or sleep schedule are on file" not in text


# ---------------------------------------------------------------------------
# _build_instruction: injectable clock (A0.4)
# ---------------------------------------------------------------------------


def test_build_instruction_uses_pinned_today(monkeypatch):
    from datetime import datetime

    monkeypatch.setattr(agent, "_now", lambda: datetime(2026, 8, 17))
    text = agent._build_instruction(FakeReadonlyContext({}))
    assert "August 17, 2026" in text


def test_build_instruction_re_resolves_today_every_call(monkeypatch):
    """_build_instruction must call _now() fresh each time, not capture a
    date once — Agent Engine keeps this Agent instance (and its bound
    instruction callable) alive across many turns and requests."""
    from datetime import datetime

    dates = iter([datetime(2026, 8, 17), datetime(2026, 8, 18)])
    monkeypatch.setattr(agent, "_now", lambda: next(dates))

    first = agent._build_instruction(FakeReadonlyContext({}))
    second = agent._build_instruction(FakeReadonlyContext({}))

    assert "August 17, 2026" in first
    assert "August 18, 2026" in second


# ---------------------------------------------------------------------------
# _build_instruction: static prefix stability for context caching (A2.5)
# ---------------------------------------------------------------------------


def test_build_instruction_volatile_content_sits_after_the_static_rules():
    """The whole point of A2.5's reorder: today/profile/zones must render
    after every rule paragraph, not interleaved with them — an implicit
    cache hit only covers a byte-identical prefix, and this is the one
    property that keeps the ~11k-token rules+tools prefix identical across
    every user and every day. A regression here wouldn't fail any other
    test (the three-way section tests above only check substring presence,
    not position), so this needs its own check."""
    ctx = FakeReadonlyContext(
        {
            agent._PRELOADED_PROFILE_KEY: {"foo": "bar"},
            agent._PRELOADED_ZONES_KEY: [{"label": "Work"}],
        }
    )
    text = agent._build_instruction(ctx)

    # The last sentence of the static rules block (instruction.md), used
    # as the boundary — everything at or before this index must be
    # identical across users/days; everything after it is the volatile
    # tail (today, profile, zones).
    static_end = text.index("briefly confirm what you saved.")
    today_start = text.index("Today is ")
    profile_start = text.index("already loaded for this session: {'foo': 'bar'}")
    zones_start = text.index("standing day zones, already loaded for this session")

    assert static_end < today_start < profile_start < zones_start


def test_build_instruction_volatile_content_sits_after_static_rules_on_preload_failure():
    """Same ordering property, but for the failure-text branches — those
    are static strings too (chosen by preload state, not injected data),
    so they must stay before the volatile tail exactly like the success
    branches do."""
    ctx = FakeReadonlyContext(
        {agent._PROFILE_PRELOAD_FAILED_KEY: True, agent._ZONES_PRELOAD_FAILED_KEY: True}
    )
    text = agent._build_instruction(ctx)

    static_end = text.index("briefly confirm what you saved.")
    today_start = text.index("Today is ")

    assert static_end < today_start


# ---------------------------------------------------------------------------
# Model pin and thinking budget (A0.5)
# ---------------------------------------------------------------------------


def test_model_is_pinned_explicitly():
    assert agent._llm_agent.model == "gemini-2.5-flash"


def test_thinking_budget_is_set_explicitly_not_defaulted():
    config = agent._llm_agent.generate_content_config
    assert config is not None
    assert config.thinking_config is not None
    assert config.thinking_config.thinking_budget == 1024


# ---------------------------------------------------------------------------
# get_available_slots registration and the shadow-comparison callback (A4.2)
# ---------------------------------------------------------------------------


def test_get_available_slots_is_not_registered_as_a_model_tool():
    """A same-day, controlled A3.1 comparison found double-digit-point
    tier regressions from merely adding this tool to the model's tool
    list, with instruction.md unchanged — see agent.py's
    _log_schedule_shadow_comparison and scheduling_tool.py's own module
    docstring for the full reasoning. The function itself is still
    fully built and tested (test_scheduling_tool.py); shadow mode's
    actual mechanism is the after_tool_callback below, which calls it
    directly, never through the model."""
    tool_names = {getattr(t, "__name__", None) for t in agent._llm_agent.tools}
    assert "get_available_slots" not in tool_names
    assert "get_available_slots" not in agent._build_instruction(FakeReadonlyContext({}))


def test_find_zone_collisions_is_registered_with_instruction_text():
    """Unlike get_available_slots, find_zone_collisions ships as a
    registered tool in the same PR as the instruction text explaining it
    (A4.3's add-then-cut rule) — the exact discipline the regression
    above exists to enforce."""
    tool_names = {getattr(t, "__name__", None) for t in agent._llm_agent.tools}
    assert "find_zone_collisions" in tool_names
    assert "find_zone_collisions" in agent._build_instruction(FakeReadonlyContext({}))


class FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


async def test_shadow_comparison_callback_fires_for_add_calendar_event(
    tool_context, monkeypatch
):
    calls = []

    async def fake_log(ctx, user_id, habit_id, event):
        calls.append((user_id, habit_id, event))

    monkeypatch.setattr(agent, "log_shadow_comparison", fake_log)

    result = await agent._log_schedule_shadow_comparison(
        FakeTool("add_calendar_event"),
        {"habit_id": "h1"},
        tool_context,
        {"status": "success", "event": {"event_id": "e1"}},
    )

    assert result is None
    assert calls == [("user-1", "h1", {"event_id": "e1"})]


async def _noop_log_shadow_comparison(*args, **kwargs) -> None:
    return None


async def test_shadow_comparison_callback_skips_non_calendar_tools(tool_context, monkeypatch):
    calls = []

    async def fake_log(*args, **kwargs):
        calls.append(1)

    monkeypatch.setattr(agent, "log_shadow_comparison", fake_log)

    await agent._log_schedule_shadow_comparison(
        FakeTool("list_habits"),
        {"habit_id": "h1"},
        tool_context,
        {"status": "success"},
    )

    assert calls == []


async def test_shadow_comparison_callback_skips_failed_calls(tool_context, monkeypatch):
    calls = []

    async def fake_log(*args, **kwargs):
        calls.append(1)

    monkeypatch.setattr(agent, "log_shadow_comparison", fake_log)

    await agent._log_schedule_shadow_comparison(
        FakeTool("add_calendar_event"),
        {"habit_id": "h1"},
        tool_context,
        {"status": "error"},
    )

    assert calls == []


async def test_shadow_comparison_callback_skips_plain_appointments(tool_context, monkeypatch):
    """No habit_id means this wasn't a habit session — nothing to
    shadow-compare against the engine, which only ranks habit
    placements."""
    calls = []

    async def fake_log(*args, **kwargs):
        calls.append(1)

    monkeypatch.setattr(agent, "log_shadow_comparison", fake_log)

    await agent._log_schedule_shadow_comparison(
        FakeTool("add_calendar_event"),
        {},
        tool_context,
        {"status": "success", "event": {"event_id": "e1"}},
    )

    assert calls == []


async def test_shadow_comparison_callback_never_overrides_the_real_result(
    tool_context, monkeypatch
):
    monkeypatch.setattr(agent, "log_shadow_comparison", _noop_log_shadow_comparison)

    result = await agent._log_schedule_shadow_comparison(
        FakeTool("update_calendar_event"),
        {"habit_id": "h1"},
        tool_context,
        {"status": "success", "event": {"event_id": "e1"}},
    )

    assert result is None
