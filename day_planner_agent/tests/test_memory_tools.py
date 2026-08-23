"""Coverage of memory_tools.py's write path (A2.4) — previously untested.

update_profile/save_memory used to await the Vertex AI write inline,
stalling the turn for however long Memory Bank's server-side extraction
took. They now schedule it as a background asyncio.Task and return as
soon as it's dispatched — these tests verify that split: the tool returns
before the write resolves, the write itself is unchanged (still
wait_for_completion=True) and observably completes, and a failure is
retried and logged rather than silently dropped.

Doesn't reuse conftest.py's shared FakeToolContext/tool_context fixture —
this module needs a different surface (._invocation_context.memory_service,
session.app_name) that no other test file needs, same reasoning
test_agent.py already used for its own FakeCallbackContext.
"""

import asyncio
import logging
from types import SimpleNamespace

import google.auth.exceptions
import google.genai.errors
import pytest

from day_planner_agent import memory_tools


class FakeMemoryService:
    def __init__(self, project="test-proj", location="us-central1", agent_engine_id="123"):
        self._project = project
        self._location = location
        self._agent_engine_id = agent_engine_id


class FakeSession:
    def __init__(self, user_id="user-1", app_name="day-planner"):
        self.user_id = user_id
        self.app_name = app_name


class FakeToolContext:
    def __init__(self, memory_service=None, user_id="user-1"):
        self.session = FakeSession(user_id=user_id)
        self._invocation_context = SimpleNamespace(
            memory_service=memory_service if memory_service is not None else FakeMemoryService()
        )


class FakeOperation:
    def __init__(self, error=None):
        self.error = error


class FakeMemoriesResource:
    """generate()/create() effects are independent queues — a queued
    Exception is raised, an Exception subclass instance specifically; a
    FakeOperation is returned. None configured means always a bare
    success."""

    def __init__(self, generate_effects=None, create_effects=None):
        self.generate_calls: list[dict] = []
        self.create_calls: list[dict] = []
        self._generate_effects = list(generate_effects) if generate_effects is not None else None
        self._create_effects = list(create_effects) if create_effects is not None else None

    async def generate(self, **kwargs):
        self.generate_calls.append(kwargs)
        if self._generate_effects is not None:
            effect = self._generate_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            return effect
        return FakeOperation()

    async def create(self, **kwargs):
        self.create_calls.append(kwargs)
        if self._create_effects is not None:
            effect = self._create_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            return effect
        return FakeOperation()


def _install_fake_client(monkeypatch, memories: FakeMemoriesResource):
    fake_client = SimpleNamespace(
        aio=SimpleNamespace(agent_engines=SimpleNamespace(memories=memories))
    )
    monkeypatch.setattr(memory_tools.vertexai, "Client", lambda **kwargs: fake_client)


async def _fast_sleep(_delay: float) -> None:
    return None


@pytest.fixture(autouse=True)
async def _clean_pending_writes(monkeypatch):
    """A leaked task from a failed/interrupted test run must not bleed
    into the next test's assertions about how many writes are pending."""
    monkeypatch.setattr(memory_tools, "_sleep", _fast_sleep)
    memory_tools._pending_writes.clear()
    yield
    for task in list(memory_tools._pending_writes):
        task.cancel()
    memory_tools._pending_writes.clear()


def _client_error(status="UNAVAILABLE", code=503):
    # Matches the shape a real failure actually has — confirmed by
    # forcing one against the live API (a nonexistent reasoning engine
    # id, an unresolvable region): google.genai.errors.ClientError takes
    # (code, response_json), not a plain string message.
    return google.genai.errors.ClientError(code, {"error": {"message": "boom", "status": status}})


def _server_error(status="INTERNAL", code=500):
    return google.genai.errors.ServerError(code, {"error": {"message": "boom", "status": status}})


# ---------------------------------------------------------------------------
# update_profile
# ---------------------------------------------------------------------------


async def test_update_profile_returns_before_the_write_resolves(monkeypatch):
    gate = asyncio.Event()
    memories = FakeMemoriesResource()

    async def slow_generate(**kwargs):
        memories.generate_calls.append(kwargs)
        await gate.wait()
        return FakeOperation()

    memories.generate = slow_generate
    _install_fake_client(monkeypatch, memories)

    result = await memory_tools.update_profile(FakeToolContext(), preferences="gym at 6am")

    assert result == {"status": "success", "message": "Saving profile."}
    assert len(memory_tools._pending_writes) == 1
    pending = next(iter(memory_tools._pending_writes))
    assert not pending.done()

    gate.set()
    await pending
    assert memory_tools._pending_writes == set()


async def test_update_profile_write_still_completes_and_is_observable(monkeypatch):
    memories = FakeMemoriesResource()
    _install_fake_client(monkeypatch, memories)

    await memory_tools.update_profile(FakeToolContext(), preferences="gym at 6am")
    await asyncio.gather(*memory_tools._pending_writes)

    assert len(memories.generate_calls) == 1
    call = memories.generate_calls[0]
    assert call["name"] == "reasoningEngines/123"
    # The exact bug this must not reintroduce: wait_for_completion must
    # still be True on the call that actually executes the write.
    assert call["config"] == {"wait_for_completion": True}
    assert call["scope"] == {"app_name": "day-planner", "user_id": "user-1"}


async def test_update_profile_no_fields_is_a_synchronous_error_not_scheduled():
    result = await memory_tools.update_profile(FakeToolContext())

    assert result == {"status": "error", "message": "No fields provided to save."}
    assert memory_tools._pending_writes == set()


async def test_update_profile_memory_bank_not_configured_is_synchronous_error():
    # _memory_service treats a service with no _agent_engine_id as "not
    # configured" — same fake shape test_agent.py's preload tests use.
    tool_context = FakeToolContext(memory_service=SimpleNamespace())

    result = await memory_tools.update_profile(tool_context, preferences="gym at 6am")

    assert result == {"status": "error", "message": "Memory Bank is not configured."}
    assert memory_tools._pending_writes == set()


async def test_update_profile_write_failure_is_retried_and_eventually_succeeds(
    monkeypatch, caplog
):
    memories = FakeMemoriesResource(
        generate_effects=[FakeOperation(error="boom"), FakeOperation(error="boom"), FakeOperation()]
    )
    _install_fake_client(monkeypatch, memories)

    with caplog.at_level(logging.WARNING, logger="day_planner_agent.memory_tools"):
        await memory_tools.update_profile(FakeToolContext(), preferences="gym at 6am")
        await asyncio.gather(*memory_tools._pending_writes)

    assert len(memories.generate_calls) == 3
    assert sum("failed" in r.message for r in caplog.records) == 2


async def test_update_profile_write_permanently_failing_is_logged_not_raised(monkeypatch, caplog):
    memories = FakeMemoriesResource(
        generate_effects=[FakeOperation(error="boom")] * memory_tools._BACKGROUND_WRITE_MAX_ATTEMPTS
    )
    _install_fake_client(monkeypatch, memories)

    with caplog.at_level(logging.WARNING, logger="day_planner_agent.memory_tools"):
        await memory_tools.update_profile(FakeToolContext(), preferences="gym at 6am")
        # Awaiting the task itself must not raise — a background write's
        # failure has nowhere left to report to except the log.
        await asyncio.gather(*memory_tools._pending_writes)

    assert len(memories.generate_calls) == memory_tools._BACKGROUND_WRITE_MAX_ATTEMPTS
    assert any("permanently failed" in r.message for r in caplog.records)


async def test_update_profile_write_raising_an_exception_is_retried_too(monkeypatch, caplog):
    memories = FakeMemoriesResource(generate_effects=[RuntimeError("transport error"), FakeOperation()])
    _install_fake_client(monkeypatch, memories)

    with caplog.at_level(logging.WARNING, logger="day_planner_agent.memory_tools"):
        await memory_tools.update_profile(FakeToolContext(), preferences="gym at 6am")
        await asyncio.gather(*memory_tools._pending_writes)

    assert len(memories.generate_calls) == 2


async def test_update_profile_async_write_failure_with_real_exception_type_does_not_escape(
    monkeypatch, caplog
):
    """A2.6/A2.4 interaction: the try/except this task added wraps only
    the synchronous vertexai.Client(...) construction — the write itself
    executes later, inside the detached background task scheduled by
    _write_with_retry, which is a different code path with its own
    (pre-existing, A2.4) error handling. This confirms that path also
    survives the *real* Memory Bank exception type, not just a generic
    RuntimeError stand-in — awaiting the pending task must not raise."""
    memories = FakeMemoriesResource(generate_effects=[_client_error(), FakeOperation()])
    _install_fake_client(monkeypatch, memories)

    with caplog.at_level(logging.WARNING, logger="day_planner_agent.memory_tools"):
        await memory_tools.update_profile(FakeToolContext(), preferences="gym at 6am")
        # Must not raise — the whole point of moving the write off the
        # request path (A2.4) is that its failures can't reach a caller
        # that's already gone.
        await asyncio.gather(*memory_tools._pending_writes)

    assert len(memories.generate_calls) == 2


# ---------------------------------------------------------------------------
# save_memory
# ---------------------------------------------------------------------------


async def test_save_memory_returns_before_the_write_resolves(monkeypatch):
    gate = asyncio.Event()
    memories = FakeMemoriesResource()

    async def slow_create(**kwargs):
        memories.create_calls.append(kwargs)
        await gate.wait()
        return FakeOperation()

    memories.create = slow_create
    _install_fake_client(monkeypatch, memories)

    result = await memory_tools.save_memory(FakeToolContext(), "took the 6am gym slot today")

    assert result == {"status": "success", "message": "Saving."}
    assert len(memory_tools._pending_writes) == 1
    pending = next(iter(memory_tools._pending_writes))
    assert not pending.done()

    gate.set()
    await pending


async def test_save_memory_write_still_completes_and_is_observable(monkeypatch):
    memories = FakeMemoriesResource()
    _install_fake_client(monkeypatch, memories)

    await memory_tools.save_memory(FakeToolContext(), "took the 6am gym slot today")
    await asyncio.gather(*memory_tools._pending_writes)

    assert len(memories.create_calls) == 1
    call = memories.create_calls[0]
    assert call["fact"] == "took the 6am gym slot today"
    assert call["config"] == {"wait_for_completion": True}


async def test_save_memory_memory_bank_not_configured_is_synchronous_error():
    tool_context = FakeToolContext(memory_service=SimpleNamespace())

    result = await memory_tools.save_memory(tool_context, "a fact")

    assert result == {"status": "error", "message": "Memory Bank is not configured."}
    assert memory_tools._pending_writes == set()


async def test_save_memory_write_failure_is_retried_and_eventually_succeeds(monkeypatch, caplog):
    memories = FakeMemoriesResource(
        create_effects=[FakeOperation(error="boom"), FakeOperation()]
    )
    _install_fake_client(monkeypatch, memories)

    with caplog.at_level(logging.WARNING, logger="day_planner_agent.memory_tools"):
        await memory_tools.save_memory(FakeToolContext(), "a fact")
        await asyncio.gather(*memory_tools._pending_writes)

    assert len(memories.create_calls) == 2


async def test_save_memory_async_write_failure_with_real_exception_type_does_not_escape(
    monkeypatch, caplog
):
    """Same A2.6/A2.4 interaction check as update_profile's — the real
    Memory Bank exception type, raised from where the write actually
    executes, must not escape the background task."""
    memories = FakeMemoriesResource(create_effects=[_client_error(), FakeOperation()])
    _install_fake_client(monkeypatch, memories)

    with caplog.at_level(logging.WARNING, logger="day_planner_agent.memory_tools"):
        await memory_tools.save_memory(FakeToolContext(), "a fact")
        await asyncio.gather(*memory_tools._pending_writes)

    assert len(memories.create_calls) == 2


# ---------------------------------------------------------------------------
# A2.6: backend failures return {"status": "error", ...} instead of
# crashing the turn — never a shape that reads as "no profile exists".
# ---------------------------------------------------------------------------


class FakeMemoryServiceWithProfiles(FakeMemoryService):
    def __init__(self, profiles=None, retrieve_effect=None, **kwargs):
        super().__init__(**kwargs)
        self._profiles = profiles or []
        self._retrieve_effect = retrieve_effect

    async def retrieve_profiles(self, **kwargs):
        if self._retrieve_effect is not None:
            raise self._retrieve_effect
        return self._profiles


async def test_get_profile_returns_matching_schema():
    profile = SimpleNamespace(schema_id=memory_tools.PROFILE_SCHEMA_ID, profile={"a": "b"})
    service = FakeMemoryServiceWithProfiles(profiles=[profile])

    result = await memory_tools.get_profile(FakeToolContext(memory_service=service))
    assert result == {"status": "success", "profile": {"a": "b"}}


async def test_get_profile_backend_failure_omits_profile_key():
    service = FakeMemoryServiceWithProfiles(retrieve_effect=_client_error())

    result = await memory_tools.get_profile(FakeToolContext(memory_service=service))
    assert result["status"] == "error"
    # Must never look like {"status": "success", "profile": {}} — that
    # would read as "the user genuinely has no preferences on file".
    assert "profile" not in result


async def test_get_profile_server_error_also_caught():
    """ServerError (5xx) is a sibling of ClientError (4xx) under the
    same APIError base _MEMORY_BANK_ERROR catches — both matter, since a
    real outage is far more likely to surface as a 5xx than a 4xx."""
    service = FakeMemoryServiceWithProfiles(retrieve_effect=_server_error())

    result = await memory_tools.get_profile(FakeToolContext(memory_service=service))
    assert result["status"] == "error"
    assert "profile" not in result


async def test_get_profile_auth_failure_also_caught():
    service = FakeMemoryServiceWithProfiles(
        retrieve_effect=google.auth.exceptions.TransportError("boom")
    )

    result = await memory_tools.get_profile(FakeToolContext(memory_service=service))
    assert result["status"] == "error"


async def test_get_profile_programming_error_still_propagates():
    """A2.6's scope item 3: only backend/auth failure classes are
    caught — a real bug must keep surfacing loudly."""
    service = FakeMemoryServiceWithProfiles(retrieve_effect=TypeError("not a backend failure"))

    with pytest.raises(TypeError):
        await memory_tools.get_profile(FakeToolContext(memory_service=service))


async def test_update_profile_client_construction_failure_returns_error(monkeypatch):
    def raising_client(**kwargs):
        raise google.auth.exceptions.DefaultCredentialsError("boom")

    monkeypatch.setattr(memory_tools.vertexai, "Client", raising_client)

    result = await memory_tools.update_profile(FakeToolContext(), preferences="gym at 6am")
    assert result["status"] == "error"
    assert memory_tools._pending_writes == set()


async def test_save_memory_client_construction_failure_returns_error(monkeypatch):
    def raising_client(**kwargs):
        raise google.auth.exceptions.DefaultCredentialsError("boom")

    monkeypatch.setattr(memory_tools.vertexai, "Client", raising_client)

    result = await memory_tools.save_memory(FakeToolContext(), "a fact")
    assert result["status"] == "error"
    assert memory_tools._pending_writes == set()
