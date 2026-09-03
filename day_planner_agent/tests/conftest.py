import os
from pathlib import Path

os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-proj")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")
os.environ.setdefault("INTERNAL_BACKEND_URL", "https://internal.example.invalid")
os.environ.setdefault("APP_BACKEND_URL", "https://app.example.invalid")

# agent.py builds AdkApp(agent=_llm_agent) at import time, which eagerly
# resolves a GCP project via google.auth.default() — with no credentials of
# any kind on the environment, that raises before a single test even runs.
# Nothing in this suite makes a real Google Cloud call with these
# credentials (backend_client and the Calendar client are always
# monkeypatched), so a fake, non-functional key that only needs to parse is
# enough. Without this, the suite silently depended on whoever ran it
# having their own gcloud application-default login configured — it passed
# on a real dev machine and failed on a clean CI runner (see A0.3).
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    str(Path(__file__).parent / "fixtures" / "fake_service_account.json"),
)

import httpx  # noqa: E402
import pytest  # noqa: E402

# ---------------------------------------------------------------------------
# Shared scenario fixtures (A3.1) — everything below this point has no
# pytest-specific dependency and no module-level side effect, unlike the
# env var setup above. day_planner_agent/evals/runner.py imports
# ScenarioFixture directly from this module (not through pytest) to build
# the same fixture calendar/backend state a scenario's `given` block
# describes, real model calls included — the eval runner explicitly sets
# its own GOOGLE_APPLICATION_CREDENTIALS/GOOGLE_CLOUD_PROJECT before ever
# importing this module, so this file's own setdefault calls above become
# harmless no-ops for that path (setdefault never overrides an
# already-set variable) rather than silently swapping in the fake
# unit-test credentials for a run that needs to hit the real model.
#
# Deliberately instance-scoped, not the module-global-dict style used by
# individual test files in this suite — a scenario runs 3-5 times (A3.1's
# repeat-and-gate requirement) and each trial needs its own isolated
# calendar/habit state, not one shared mutable dict that a habit created
# in trial 1 leaks into trial 2.


class FakeEventsResource:
    """`existing` seeds events(), list() returns; insert()/patch() record
    what was submitted and echo it back with a synthetic id, so a caller
    can assert on exactly what the model chose to place. get() serves
    both existing and newly-inserted events, since A2.3's insert retry
    logic fetches by id on a 409."""

    def __init__(self, existing: list[dict] | None = None) -> None:
        self.existing = list(existing or [])
        self.inserted: list[dict] = []
        self.patched: list[dict] = []
        self.deleted: list[str] = []
        self._op: str | None = None
        self._pending: dict = {}

    def list(self, **kwargs):
        self._op = "list"
        return self

    def insert(self, **kwargs):
        self._op = "insert"
        self._pending = kwargs
        return self

    def patch(self, **kwargs):
        self._op = "patch"
        self._pending = kwargs
        return self

    def get(self, **kwargs):
        self._op = "get"
        self._pending = kwargs
        return self

    def delete(self, **kwargs):
        self._op = "delete"
        self._pending = kwargs
        return self

    def execute(self):
        if self._op == "list":
            return {"items": self.existing + self.inserted}
        if self._op == "insert":
            item = dict(self._pending["body"])
            item.setdefault("id", f"evt-{len(self.inserted) + 1}")
            item.setdefault("htmlLink", "https://calendar.example/e")
            self.inserted.append(item)
            return item
        if self._op == "patch":
            event_id = self._pending["eventId"]
            for item in self.existing + self.inserted:
                if item.get("id") == event_id:
                    item.update(self._pending["body"])
                    self.patched.append(item)
                    return item
            raise LookupError(f"no fixture event with id {event_id!r}")
        if self._op == "get":
            event_id = self._pending["eventId"]
            for item in self.existing + self.inserted:
                if item.get("id") == event_id:
                    return item
            from googleapiclient.errors import HttpError
            from types import SimpleNamespace as _SimpleNamespace

            raise HttpError(_SimpleNamespace(status=404, reason="not found"), b"{}")
        if self._op == "delete":
            self.deleted.append(self._pending["eventId"])
            return None
        raise AssertionError(f"unexpected op {self._op!r}")


class FakeCalendarListResource:
    def __init__(
        self,
        access_role: str = "owner",
        time_zone: str = "America/Los_Angeles",
        summary: str | None = None,
    ) -> None:
        if summary is None:
            # Deferred to call time, not a plain default value, so this
            # can import from evals/invariants.py without triggering
            # day_planner_agent/__init__.py's eager agent.py import at
            # conftest.py's own module-load time (see this file's env
            # var setup at the top, and evals/runner.py's own docstring
            # on the same ordering hazard).
            from day_planner_agent.evals.invariants import READ_ONLY_CALENDAR_SUMMARY

            summary = READ_ONLY_CALENDAR_SUMMARY
        self._access_role = access_role
        self._time_zone = time_zone
        self._summary = summary

    def get(self, **kwargs):
        return self

    def execute(self):
        return {"accessRole": self._access_role, "timeZone": self._time_zone, "summary": self._summary}


class FakeCalendarService:
    def __init__(self, existing: list[dict] | None = None, access_role: str = "owner") -> None:
        self._events = FakeEventsResource(existing)
        self._calendar_list = FakeCalendarListResource(access_role=access_role)

    def events(self):
        return self._events

    def calendarList(self):
        return self._calendar_list

    def placed_events(self) -> list[dict]:
        """What the agent itself created this run — evals/runner.py's
        invariant checks care about this, not the pre-seeded `existing`
        events a scenario's fixture started with."""
        return list(self._events.inserted)


class _FakeProfileRecord:
    """Matches the shape memory_tools.get_profile reads off whatever
    service.retrieve_profiles(...) returns: .schema_id and .profile."""

    def __init__(self, profile: dict) -> None:
        self.schema_id = "day-planner-profile"
        self.profile = profile


class _FakeMemoryService:
    """Stands in for the VertexAiMemoryBankService memory_tools.py's
    _memory_service() looks for — real code gates on
    hasattr(service, "_agent_engine_id"), which InMemoryRunner's own
    default memory service doesn't have, so every get_profile/
    update_profile/save_memory call errored with "Memory Bank is not
    configured" in every eval trial until this fixture existed (A4.3
    follow-up investigation, see evals/BASELINE.md). _project/_location/
    _agent_engine_id are read by update_profile/save_memory to construct
    a (also faked, see _FakeVertexaiModule below) write client; values
    here are arbitrary, never used to reach a real service."""

    def __init__(self, profile: dict) -> None:
        self._project = "test-proj"
        self._location = "us-central1"
        self._agent_engine_id = "fake-engine-id"
        self._profile = profile

    async def retrieve_profiles(self, *, app_name, user_id):
        if not self._profile:
            return []
        return [_FakeProfileRecord(dict(self._profile))]


class _FakeMemoryOperation:
    """Matches the .error attribute memory_tools._write_with_retry checks
    on whatever agent_engines.memories.generate/create returns."""

    error = None


class _FakeAgentEngineMemories:
    async def generate(self, **kwargs):
        return _FakeMemoryOperation()

    async def create(self, **kwargs):
        return _FakeMemoryOperation()


class _FakeVertexaiAio:
    def __init__(self) -> None:
        self.agent_engines = type("_", (), {"memories": _FakeAgentEngineMemories()})()


class _FakeVertexaiClient:
    def __init__(self, project=None, location=None) -> None:
        self.aio = _FakeVertexaiAio()


class _FakePermissiveConstruct:
    """Accepts and discards any kwargs — stands in for
    vertexai.types.GenerateMemoriesRequestDirectContentsSource(Event) in
    update_profile's write path. The fake generate() below never
    inspects direct_contents_source's contents, so there's nothing to
    reproduce here beyond "doesn't raise."""

    def __init__(self, **kwargs) -> None:
        pass


class _FakeVertexaiTypes:
    GenerateMemoriesRequestDirectContentsSource = _FakePermissiveConstruct
    GenerateMemoriesRequestDirectContentsSourceEvent = _FakePermissiveConstruct


class _FakeVertexaiModule:
    """Replaces memory_tools' own `vertexai` import wholesale — only
    .Client(...) and .types.* are ever used by update_profile/
    save_memory, and never with real credentials in a test/eval run."""

    Client = _FakeVertexaiClient
    types = _FakeVertexaiTypes()


class ScenarioFixture:
    """Builds the fake backend_client/calendar_tool state one eval
    scenario's `given` block describes, and installs it via monkeypatch —
    the same fixture-stubbing shape every unit test in this suite already
    uses (see test_calendar_tool.py's FakeCalendarService and friends),
    generalized so day_planner_agent/evals/runner.py can build it from
    declarative scenario data instead of hand-written per-test fakes.

    zones/sleep_schedule/habits/calendar_events use the same shapes
    backend_client's real functions return — see zone_tools.py,
    habit_tools.py, and calendar_tool.py's own docstrings for the exact
    dict fields expected.

    zones_fetch_fails/needs_auth/calendar_access_role (A3.2) inject the
    three failure modes clean fixtures would otherwise hide — see
    evals/scenarios/failure_modes/. A clean fixture that always succeeds
    would never have caught A0.2 (the preload-fail-open bug): the agent
    looks perfectly behaved in every scenario while failing open in
    production, because nothing ever exercises the failure path at all.

    habits_fetch_fails (A2.6) is the same idea for a call that's never
    part of preload at all — habits aren't preloaded the way zones and
    the profile are (see habit_tools.py's own list_habits docstring), so
    every list_habits call is inherently a live, mid-conversation one.
    This is what lets a failure-mode scenario exercise "a backend call
    fails mid-conversation" distinctly from A3.2's zone_fetch_fails,
    which is about the preload path specifically.

    create_zone/update_zone (A4.3) mutate self.zones the same way
    create_habit/update_habit already mutate self.habits — needed so a
    scenario whose user_says causes the model to call create_zone or
    update_zone mid-conversation actually has that zone reflected in a
    later list_zones or find_zone_collisions call within the same trial,
    rather than the fixture silently staying on its `given.zones`
    snapshot. Not needed before A4.3: no prior scenario relied on the
    model's own zone edits taking effect within the same run.

    get_profile/update_profile/save_memory (memory_tools.py) are faked
    via _memory_service/vertexai below, not a method on this class the
    way zones/habits are — a follow-up to A4.3's paragraph-13 work found
    every eval trial's get_profile call had silently been returning
    "Memory Bank is not configured" (InMemoryRunner's default memory
    service lacks the `_agent_engine_id` attribute memory_tools.py's own
    `_memory_service()` checks for), and any scenario whose utterance
    triggered update_profile mid-conversation got a hard, visible error.
    load_memory (ADK's own built-in tool) needed no fix — it goes through
    ToolContext.search_memory/InMemoryMemoryService instead, which
    InMemoryRunner already backs with a working, empty-by-default
    implementation.

    habit_sessions accepts pre-seeded, previously-placed sessions (A4.3's
    repeat-bump cut) — distinct from calendar_events, the calendar's
    *current* state. list_habit_sessions returns them (filtered by date),
    so compute_habit_review (habit_tools.py) can genuinely diff "what was
    planned" against "what's on the calendar now" and derive a real
    "moved"/"gone" outcome with a real bumped_by, instead of the review
    always coming back empty. Nothing before A4.3 needed a scenario where
    that diff itself was the thing under test.
    """

    def __init__(
        self,
        *,
        zones: list[dict] | None = None,
        sleep_schedule: dict | None = None,
        habits: list[dict] | None = None,
        calendar_events: list[dict] | None = None,
        habit_sessions: list[dict] | None = None,
        profile: dict | None = None,
        zones_fetch_fails: bool = False,
        habits_fetch_fails: bool = False,
        needs_auth: bool = False,
        calendar_access_role: str = "owner",
    ) -> None:
        self.zones = list(zones or [])
        self.sleep_schedule = sleep_schedule
        self.habits = list(habits or [])
        self.calendar_service = FakeCalendarService(calendar_events, access_role=calendar_access_role)
        # Pre-seeded ("given") sessions plus anything upsert_habit_session
        # appends live during the trial — one list, matching how a real
        # list_habit_sessions call would see both (A4.3, repeat-bump cut).
        self.habit_sessions: list[dict] = list(habit_sessions or [])
        self.zones_fetch_fails = zones_fetch_fails
        self.habits_fetch_fails = habits_fetch_fails
        self.needs_auth = needs_auth
        self._memory_service = _FakeMemoryService(dict(profile or {}))

    # -- backend_client fakes --------------------------------------------

    async def list_calendars(self, user_id):
        if self.needs_auth:
            from day_planner_agent import backend_client
            from day_planner_agent.evals.invariants import NEEDS_AUTH_CONNECT_URL

            raise backend_client.NeedsAuth(
                NEEDS_AUTH_CONNECT_URL, "That calendar's account needs reconnecting."
            )
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [
                {
                    "account_id": "acct-1",
                    "calendar_id": "me@gmail.com",
                    "summary": "Personal",
                    "is_primary": True,
                }
            ],
        }

    async def access_token(self, user_id, account_id):
        return "FAKE-TOKEN"

    async def list_zones(self, user_id):
        if self.zones_fetch_fails:
            # A2.6: must be something backend_client.BACKEND_ERROR actually
            # catches (httpx.HTTPError / GoogleAuthError) — a real backend
            # outage surfaces as one of these, never a bare RuntimeError, so
            # simulating anything else would let a scenario "pass" the
            # no_exception check for the wrong reason (the fixture's own
            # exception type happening not to match what production code
            # would ever actually see, not because the fix genuinely works).
            raise httpx.ConnectError("simulated backend failure fetching zones")
        return list(self.zones)

    async def get_sleep_schedule(self, user_id):
        return dict(self.sleep_schedule) if self.sleep_schedule else None

    async def list_habits(self, user_id, status=None):
        if self.habits_fetch_fails:
            raise httpx.ConnectError("simulated backend failure fetching habits")
        if status is None:
            return list(self.habits)
        return [h for h in self.habits if h.get("status") == status]

    async def create_habit(self, user_id, *, label, goal):
        habit = {
            "habit_id": f"h-{len(self.habits) + 1}",
            "label": label,
            "goal": goal,
            "status": "active",
            "allowed_zones": [],
        }
        self.habits.append(habit)
        return habit

    async def update_habit(self, user_id, habit_id, **kwargs):
        for h in self.habits:
            if h["habit_id"] == habit_id:
                h.update({k: v for k, v in kwargs.items() if v is not None})
                return h
        return None

    async def create_zone(self, user_id, *, label, start_time, end_time, days_of_week):
        zone = {
            "zone_id": f"z-{len(self.zones) + 1}",
            "label": label,
            "start_time": start_time,
            "end_time": end_time,
            "days_of_week": days_of_week,
        }
        self.zones.append(zone)
        return zone

    async def update_zone(self, user_id, zone_id, **kwargs):
        for z in self.zones:
            if z["zone_id"] == zone_id:
                z.update({k: v for k, v in kwargs.items() if v is not None})
                return z
        return None

    async def upsert_habit_session(self, user_id, **kwargs):
        self.habit_sessions.append(kwargs)
        return {"habit_session_id": f"hs-{len(self.habit_sessions)}"}

    async def set_habit_session_status(self, user_id, **kwargs):
        return {"status": kwargs.get("status")}

    async def list_habit_sessions(self, user_id, *, planned_from, planned_to):
        # Date-portion-only comparison, matching the "known limitation"
        # this whole call chain already documents (scheduling_tool.py:
        # planned_from/planned_to arrive as UTC-calendar-day strings, not
        # the user's own local calendar days) — good enough for a test
        # fixture, not meant to be a precise timezone-aware filter.
        from_date, to_date = planned_from[:10], planned_to[:10]
        return [
            s for s in self.habit_sessions
            if from_date <= s["planned_start"][:10] < to_date
        ]

    # -- calendar_tool.build fake -----------------------------------------

    def build(self, service_name, version, credentials):
        return self.calendar_service

    # -- install ------------------------------------------------------------

    def install(self, monkeypatch) -> None:
        # A6.2: credentials (backend_client, audienced to
        # day_planner_backend_internal) and domain data (domain_client,
        # audienced to day_planner_backend_app's /agent/*) are two
        # separate clients now — see backend_client.py's own module
        # docstring for why they must never be confused.
        from day_planner_agent import backend_client, calendar_tool, domain_client, memory_tools

        monkeypatch.setattr(backend_client, "list_calendars", self.list_calendars)
        monkeypatch.setattr(backend_client, "access_token", self.access_token)
        monkeypatch.setattr(domain_client, "list_zones", self.list_zones)
        monkeypatch.setattr(domain_client, "get_sleep_schedule", self.get_sleep_schedule)
        monkeypatch.setattr(domain_client, "list_habits", self.list_habits)
        monkeypatch.setattr(domain_client, "create_habit", self.create_habit)
        monkeypatch.setattr(domain_client, "update_habit", self.update_habit)
        monkeypatch.setattr(domain_client, "create_zone", self.create_zone)
        monkeypatch.setattr(domain_client, "update_zone", self.update_zone)
        monkeypatch.setattr(domain_client, "upsert_habit_session", self.upsert_habit_session)
        monkeypatch.setattr(
            domain_client, "set_habit_session_status", self.set_habit_session_status
        )
        monkeypatch.setattr(domain_client, "list_habit_sessions", self.list_habit_sessions)
        monkeypatch.setattr(calendar_tool, "build", self.build)
        monkeypatch.setattr(
            memory_tools, "_memory_service", lambda tool_context: self._memory_service
        )
        monkeypatch.setattr(memory_tools, "vertexai", _FakeVertexaiModule())


class FakeSession:
    def __init__(self, user_id: str) -> None:
        self.user_id = user_id


class FakeToolContext:
    """The surface calendar_tool.py and habit_tools.py touch on a real
    ToolContext — .state added for A1.4's telemetry, which reads the
    zones agent.py's _preload_zones would have cached there and writes
    its own per-session outcomes back into it. .invocation_id added for
    A2.2's per-invocation memoization in calendar_tool.py — real ADK
    shares one invocation_id across every tool call within a single
    turn, which is what scopes that cache to "this turn only"."""

    def __init__(self, user_id: str, invocation_id: str = "inv-1") -> None:
        self.session = FakeSession(user_id)
        self.state: dict = {}
        self.invocation_id = invocation_id


@pytest.fixture
def tool_context():
    return FakeToolContext(user_id="user-1")
