# Roadmap 1 — The Agent: Executable Task Specs

Companion to the overview at *Instrument, Test, Shrink*. Each task below is
scoped to be handed to a coding agent as a single unit of work.

## Status — start here

| Phase | State | Next |
|---|---|---|
| A0 — live defects | ✅ complete | — |
| A1 — instrument | A1.1–A1.3 ✅ | **A1.5, then A1.4** |
| A2 — cheaper turns | not started | after A1 |
| A3 — test behaviour | not started | parallel with A2 |
| A4 — shrink | not started | blocked by A3 |
| A5 — standing debt | not started | slot between phases |
| A6 — user-owned domain data | not started | after A2, **before A4.2** |

**The next task is A1.5**, not A1.4. See the phase A1 header.

Phase A6 was added last but does not run last — it moves habits, zones, habit
sessions and the sleep schedule out of the internal service so a UI can reach
them, and A4.2 reads that data. Building A4.2 first means pointing it at a
service that is about to stop owning it.

## How to use this document

- **Work one task at a time.** Each has its own acceptance criteria. Do not
  batch tasks from different phases.
- **Respect "Out of scope" literally.** These sections exist because the
  adjacent work is either scheduled elsewhere in this roadmap or deliberately
  deferred. Expanding scope breaks the dependency chain.
- **Phases run in order, but task numbers within a phase do not.** Numbering is
  append-only so cross-references stay valid when tasks are added later. Two
  phases have an execution order that differs from their numbering — A1 and A3 —
  and each states it in its own header. **Read the phase header before picking a
  task.**
- **Phase dependencies:** A3 needs A0.3 and A0.4. A4 needs A3's baseline. A1.4
  needs A1.5. Do not start A4 before A3 produces a baseline.
- **If a task's acceptance criteria appear to require infrastructure from a
  later phase, stop and flag it** rather than building it. That has already
  happened once (A1.4's original rapid-delete criterion needed change detection
  from Roadmap 2 task B2.2) and the spec was wrong, not the reader.
- **Every task ends green.** `pytest` passes across all three suites before
  the task is considered done.

## Repository map

Directories marked `(new)` do not exist yet and are created by the task noted.

```
day_planner_agent/              runs inside Vertex AI Agent Engine
  agent.py                      Agent + AdkApp wiring, preload callbacks
  instruction.md                system prompt (~6,270 tokens)
  calendar_tool.py              Google Calendar tools
  habit_tools.py                habit CRUD + review_habit_week
  zone_tools.py                 zone + sleep schedule tools
  memory_tools.py               Memory Bank profile/facts
  backend_client.py             HTTP client → day_planner_backend_internal
  tests/                        ~192 tests, tool + callback level
  evals/                        (new, A3.1) scenario runner + invariants
  scheduling/                   (new, A4.1) pure-function constraint engine

day_planner_backend_app/app/    public Cloud Run service, user-facing
  services/agent_client.py      the only caller of Agent Engine
  services/turn_log.py          turn records (A1.1)
  services/turn_log_queries.sql documented metric queries (A1.2)
  api/routes/chat.py            /me/chat, /me/chat/reset
  api/routes/me.py              /me/* — pattern to follow for A1.5's route
  db/store.py                   Firestore: users, sessions, quota
  core/config.py                Settings

day_planner_backend_internal/   internal-ingress service, service-to-service
  app/db/models.py              HabitSession, Habit, Zone, SleepSchedule
  app/db/store.py               Firestore: habits, habit_sessions, zones
  app/api/routes/internal.py    /internal/* — all OIDC-gated
  app/schemas/habit_sessions.py request/response models
  app/api/deps.py               require_internal_caller

terraform/
  cloud_run.tf                  both services
  variables.tf                  max_instances
  agent.tf                      Agent Engine + ADK class_methods snapshot
```

**A1.5 spans all three services** — it is the first task in this roadmap that
does. Habit session state lives in `day_planner_backend_internal`, the
user-facing route belongs on `day_planner_backend_app`, and the agent tool lives
in `day_planner_agent`.

---

# Phase A0 — Live defects

Days. No dependencies. Each independently shippable.

## Prerequisite (from Roadmap 2, do first)

Raise `timeout = "60s"` in `terraform/cloud_run.tf` on the **app** service
(`google_cloud_run_v2_service.default`, around line 58). Set it to `900s`.

A planning turn currently takes 60–120s and is killed partway through, often
after some calendar events are already written. You cannot evaluate agent
behaviour when the harness terminates the turn. This is a stopgap — Roadmap 2
task B1.1 removes the request from the critical path properly.

---

## A0.1 — Calendar reads truncate silently

**Type** defect · **Effort** hours · **Blocks** A3.1 fixtures, A4.5

**Files**
- `day_planner_agent/calendar_tool.py` → `_fetch_google_events`
- `day_planner_agent/tests/test_calendar_tool.py`

**Problem**

`_fetch_google_events` calls `service.events().list(...)` and reads
`response.get("items", [])`, ignoring `nextPageToken`. The Google Calendar API
returns at most 250 events per page. A user planning a busy month receives a
partial calendar, and the agent then places habit sessions on top of meetings
it cannot see. Severity scales with how busy the user is — i.e. worst for the
target persona.

**Scope**

1. Wrap the `events().list(...)` call in a pagination loop, accumulating
   `items` across pages until `nextPageToken` is absent.
2. Pass `pageToken` on subsequent requests. Keep `singleEvents=True` and
   `orderBy="startTime"` as they are.
3. Add a safety cap (suggest 20 pages / 5,000 events). On hitting it, return
   what was collected and log a structured warning — do not silently truncate a
   second time.
4. The whole loop stays inside the existing `_list()` closure that runs under
   `asyncio.to_thread`. Do not make the loop async.

**Reference pattern**

`day_planner_backend_internal/app/providers/google.py` lines ~172–195 already
implements exactly this loop shape for the calendar list. Match it.

**Out of scope**

- Do not add pagination to `_fetch_calendar_list_entry` — it fetches one entry.
- Do not change the returned event dict shape. `A4.5` handles capping and
  summarising output; this task only stops data loss.
- Do not add caching here. That is A2.2.

**Acceptance**

- [ ] A fixture returning two pages yields events from both.
- [ ] A fixture returning one page with no `nextPageToken` behaves as before.
- [ ] The page cap emits a warning and returns partial results rather than raising.
- [ ] Existing 30 tests in `test_calendar_tool.py` still pass.

**Verify**

```bash
cd day_planner_agent && python -m pytest tests/test_calendar_tool.py -v
```

---

## A0.2 — Guardrails fail open

**Type** defect · **Effort** hours · **Blocks** A3.2

**Files**
- `day_planner_agent/agent.py` → `_preload_profile`, `_preload_zones`, `_build_instruction`
- `day_planner_agent/zone_tools.py` → `list_zones`, `get_sleep_schedule`
- `day_planner_agent/tests/` → new test file for callbacks

**Problem**

Both preload callbacks set their "already loaded" state flag *before* the
fetch:

```python
callback_context.state[_ZONES_PRELOADED_KEY] = True   # set first
zones_result = await list_zones(callback_context)      # then fetch
```

`zone_tools.list_zones` has no error handling and `backend_client.list_zones`
calls `raise_for_status()`. A transient failure from the internal service
leaves the flag latched `True` for the entire session, so no retry ever
happens. `_build_instruction` then emits *"No day zones or sleep schedule are
on file for this user yet"* and the agent schedules straight through work hours
and sleep for the next six hours.

The guardrails fail **open**. The instruction describes these as "hard
constraints, not soft suggestions."

**Scope**

1. Move the flag assignment to *after* a successful fetch in both
   `_preload_profile` and `_preload_zones`.
2. Catch exceptions from the tool calls inside both callbacks. A preload
   failure must never propagate and kill the invocation, but it must also not
   be mistaken for "no data."
3. Introduce a third state distinct from loaded-and-empty: record a
   `_PRELOAD_FAILED_KEY` when a fetch errors.
4. In `_build_instruction`, emit different text for the three cases:
   - loaded with data → current behaviour
   - loaded, genuinely empty → current "none on file" text
   - **fetch failed** → new text instructing the agent that its constraints
     could not be loaded, that it must not assume none exist, and that it
     should avoid placing habit sessions until it can re-check
5. Expose a `preload_ok` boolean in session state for A1.1 to emit.

**Out of scope**

- Do not add retry logic inside the callbacks. That is A2.3.
- Do not change what `list_zones` / `get_sleep_schedule` return on success.
- Do not restructure the callback registration in `Agent(...)`.

**Acceptance**

- [ ] A failing `list_zones` leaves the preload flag unset so a later turn retries.
- [ ] A failing `list_zones` does not raise out of the callback.
- [ ] `_build_instruction` produces distinguishable text for empty vs. failed.
- [ ] New tests cover all three cases for both callbacks. There are currently zero callback tests.

**Verify**

```bash
cd day_planner_agent && python -m pytest tests/ -v -k preload
```

---

## A0.3 — No CI

**Type** build · **Effort** hours · **Blocks** A3 (all)

**Files**
- `.github/workflows/test.yml` (new)

**Problem**

140 tests exist and run only when someone remembers. Every later phase assumes
a safety net that is not wired up.

**Scope**

1. GitHub Actions workflow triggered on push and pull request.
2. Python 3.12. Install each service's `requirements.txt` + `requirements-dev.txt`.
3. Run all three suites: `day_planner_agent`, `day_planner_backend_app`,
   `day_planner_backend_internal`.
4. Fail the job on any test failure.

**Out of scope**

- No deployment, no Terraform plan/apply, no linting or formatting gates in
  this task. Keep it to tests so it lands today.
- No eval runs — A3.1 adds those to CI once they exist.

**Acceptance**

- [ ] Workflow runs green on the current `main`.
- [ ] An intentionally broken test fails the job.

---

## A0.4 — No injectable clock

**Type** build · **Effort** hours · **Blocks** A3.1

**Files**
- `day_planner_agent/agent.py` → `_build_instruction`

**Problem**

`_build_instruction` calls `datetime.now()` directly to populate `{today}`.
Any behavioural test asserting on "this week" drifts with the wall clock and
silently rots.

**Scope**

1. Introduce a module-level clock function (e.g. `_now()`) that
   `_build_instruction` calls instead of `datetime.now()` directly.
2. Make it overridable for tests — a module attribute is sufficient; do not
   introduce a DI framework.
3. Preserve the existing behaviour exactly: the date must still re-resolve per
   turn, not be captured at import. The comment at `agent.py` explaining why
   `_build_instruction` is a callable rather than an f-string documents this —
   keep that property intact.

**Out of scope**

- Do not change `utcnow()` in the backend `db/models.py`. Different concern.
- Do not thread a clock through the tools. Only the instruction needs it.

**Acceptance**

- [ ] A test can pin `today` to a fixed date and assert the rendered instruction.
- [ ] The date still changes between turns in normal operation.

---

## A0.5 — Model version floats and thinking is unbudgeted

**Type** defect · **Effort** hours · **Blocks** A3.3

**Files**
- `day_planner_agent/agent.py` line ~123

**Problem**

The entire model configuration is `model="gemini-2.5-flash"`. There is no
generation config anywhere in the repository. Two consequences:

1. If that string resolves to a moving pointer rather than a pinned build,
   agent behaviour can change with no commit, no deploy, and no signal —
   underneath 6,270 tokens of carefully tuned prompt.
2. `_visible_text` in `agent_client.py` filters `thought` parts, which means
   thinking output is being produced and billed at output-token rates on every
   call, including trivial ones, with no budget set and no measurement.

**Scope**

1. Determine whether `gemini-2.5-flash` is an alias or a pinned version in the
   deployment region. **Verify against current Google documentation — do not
   assume.**
2. Pin the model to an explicit dated version string.
3. Add an explicit generation config to the `Agent(...)` construction with a
   thinking budget set to a documented, deliberate value.
4. Add a comment recording *why* it is pinned and what the upgrade procedure
   is (run A3.3's comparison matrix, then bump).

**Out of scope**

- Do not change model tier. Flash is the right choice and A4 makes it more so.
- Do not implement model routing. That is a post-A4 decision informed by A3.3.
- Do not tune temperature without a measurement to justify it.

**Acceptance**

- [ ] Model string is explicit and dated.
- [ ] Thinking budget is set explicitly rather than defaulted.
- [ ] A comment states the upgrade procedure.

---

## A0.6 — User data in logs · DONE

Already applied, uncommitted in the working tree. Two `print()` calls and one
`logger.info` payload removed from `day_planner_agent/memory_tools.py`. Keep
the reasoning in view for A1.1, which reintroduces the same temptation at
larger scale.

---

# Phase A1 — Instrument

Week 1–3. Replace estimates with measurements.

**A1.1–A1.3 are complete.** A1.5 was added afterwards and must be built
**before** A1.4, which now depends on it. Numbering stays append-only so
references from A3.4 and A3.7 remain valid — same convention as phase A3.

Execution order: ~~A1.1~~ → ~~A1.2~~ → ~~A1.3~~ → **A1.5** → **A1.4**.

## A1.1 — The tool-call stream is discarded

**Type** build · **Effort** days · **Blocks** A1.2, A1.3, A1.4, A3.4

**Files**
- `day_planner_backend_app/app/services/agent_client.py` → `send_message`, `_visible_text`
- new module for the turn record, e.g. `app/services/turn_log.py`

**Problem**

`_visible_text` keeps only model-authored prose and drops `function_call` and
`function_response` parts from the ADK event stream. That stream is the
complete record of what the agent did, and it is discarded mid-request. When a
user reports "it booked my gym during work hours" there is no way to
reconstruct which tools fired, in what order, or what they returned.

**Scope**

1. In `send_message`, walk the event stream once and accumulate **both** the
   visible reply text (unchanged behaviour) and a structured turn record.
2. Turn record fields:
   - `turn_id`, `session_id`, `user_ref` (hashed user_id, never raw)
   - `tool_calls[]`: ordered, each with `name`, `duration_ms`, returned `status`
   - `model_calls`: count, `input_tokens`, `output_tokens`, `thinking_tokens` if available
   - `preload_ok` (from A0.2)
   - `outcome`: `completed` | `errored` | `timed_out`
   - `wall_ms`
3. Emit as a single structured JSON log line at end of turn.
4. **Redaction is the default.** Log tool *names* and returned *statuses*
   unconditionally. Tool *arguments* carry event titles, times and locations —
   put them behind an explicit environment-flagged diagnostic mode, off by
   default. Never log profile or memory payloads at any level.

**Out of scope**

- Do not build a custom storage backend. Structured logs only; the BigQuery
  sink is A1.2.
- Do not change the reply text returned to the caller.
- Do not add tracing spans. Logs first.

**Acceptance**

- [ ] Every turn emits exactly one structured record.
- [ ] Reply text is byte-identical to the pre-change behaviour for a given stream.
- [ ] With diagnostic mode off, no tool argument value appears in any log line.
- [ ] `test_chat.py` still passes.

---

## A1.2 — No cost or latency signal

**Type** build · **Effort** days · **Blocks** A3.4, scorecard

**Files**
- `day_planner_backend_app/app/services/turn_log.py` (from A1.1)
- Terraform for the BigQuery sink

**Problem**

Token spend, latency distribution and tool error rates are unknown. The
unit-economics case rests on a measured 11,330-token fixed floor and an
*assumed* turn shape. Those assumptions have never been checked.

**Scope**

1. Log sink from Cloud Logging into a BigQuery dataset.
2. Derive and make queryable:
   - tokens per turn (input / output / thinking), and the distribution
   - turn latency p50 / p95 / p99
   - tool calls per turn, as a distribution — the long tail is where loops live
   - tool error rate, split by tool name and by returned status
   - `needs_auth` rate — a product signal disguised as a technical one
3. Document the queries alongside the code so they are reusable, not
   reinvented per question.

**Out of scope**

- No dashboarding product decisions. Queries are enough to start.
- Do not add per-user cost attribution yet; aggregate is sufficient.

**Acceptance**

- [ ] All six metrics are queryable for the trailing 30 days.
- [ ] A "plan my week" turn can be located by its `turn_id` and fully inspected.

---

## A1.3 — Nothing alarms

**Type** build · **Effort** days

**Files**
- Terraform alerting policies

**Problem**

A runaway tool loop is invisible and billed. A preload failure is invisible and
unsafe.

**Scope**

1. **Loop detector**: alert when a single turn issues the same tool name with
   identical arguments three or more times. This is the most expensive class of
   agent bug and it is currently undetectable.
2. **Preload failure rate above zero**: A0.2 makes this failure visible;
   this makes it noticed.
3. Route both somewhere a human actually reads.

**Out of scope**

- Do not add alerts on latency or cost yet — establish baselines in A1.2
  first, or you will only be alerting on your own ignorance.

**Acceptance**

- [ ] A synthetic looping turn fires the alert.
- [ ] A forced preload failure fires the alert.

---

## A1.5 — "Did it happen" is inferred from event existence

**Type** build · **Effort** week · **Blocks** A1.4 · **Do this before A1.4**

> Added after A1.3 shipped. This task did not exist in the original roadmap; it
> replaces the weakest assumption in it.

**Files**
- `day_planner_backend_internal/app/db/models.py` → `HabitSession`
- `day_planner_backend_internal/app/db/store.py` → `upsert_habit_session`, new status method
- `day_planner_backend_internal/app/api/routes/internal.py`
- `day_planner_backend_internal/app/schemas/habit_sessions.py`
- `day_planner_backend_app/app/api/routes/` → new user-facing route
- `day_planner_agent/backend_client.py`, `habit_tools.py`, `instruction.md`

**Problem**

`review_habit_week` currently infers success from whether the calendar event
still exists at its planned time. That measures **plan durability, not
completion.** Two concrete failures follow:

- A session the user moved and then actually did reads as `moved` — a partial
  failure — when it was a success.
- A session sitting untouched on the calendar reads as `kept`, which says only
  that nobody deleted it. It is not evidence the user went to the gym.

Every quality metric downstream (A1.4, A3.7) inherits this weakness. Completion
needs to be first-class state we own, not an inference from a third party's
data.

**Scope**

1. Add to `HabitSession`: `status` ∈ `pending` | `completed` | `skipped`, plus
   `completed_at` and `marked_by` ∈ `user` | `agent`. Default `pending`.
2. **Three states, never two.** `pending` means unknown, not failed. Nothing in
   this task or any later one may impute "not done" from an unmarked session —
   that would silently poison every metric built on it.
3. Internal service route to set status, service-to-service as the rest of
   `/internal/*` is.
4. **New user-facing route on the app service** so the UI can mark completion
   directly. `user_id` must come from `current_user_id`, never from the request
   body — the same rule `chat.py` and every `/me` route follow. The existing
   `/internal/habit-sessions` route is service-to-service only and is not usable
   from a browser.
5. Agent tool to mark a session, so "I did the gym this morning" works
   conversationally. Both paths write the same field.
6. **Idempotent.** Marking complete twice is a no-op, not an error.
7. `review_habit_week` reads status and reports it alongside the existing
   `kept` / `moved` / `gone`. **Keep the calendar diff** — its job changes rather
   than disappearing: status answers *whether* it happened, the diff answers
   *why not*. `gone` + `pending` is a dropped session; `moved` + `completed` is a
   successful reschedule. Distinguishing those two is the point.
8. Instruction changes: the agent may mark a session on the user's say-so, and
   **must not** infer completion from an event still existing. Update
   `review_habit_week`'s docstring — it is the model's contract for reading the
   new field.

**Invariant to protect**

Completion must survive a reschedule. `habit_session_id_for(calendar_id,
event_id)` keys the record, and `update_calendar_event` patches in place so
`event_id` holds. But any path that deletes and recreates an event produces a
new key and silently orphans the completion. Either forbid that path or carry
status forward across it. Document whichever you choose in the model docstring.

**Out of scope**

- No UI. This task exposes the endpoint; the client is separate work.
- No auto-completion from any heuristic — not wearables, not location, not
  "the event still exists." Explicit marks only.
- No reminders or nudges to mark sessions complete.
- Do not remove or weaken the calendar diff in `review_habit_week`.

**Acceptance**

- [ ] Status settable via both the internal route and the user-facing route.
- [ ] User-facing route rejects any attempt to set status on another user's session.
- [ ] Marking twice is idempotent.
- [ ] `review_habit_week` returns status per session, and `moved` + `completed`
      is reported as a success rather than a partial failure.
- [ ] `pending` is never rendered or aggregated as a failure anywhere.
- [ ] Reschedule-survival invariant holds, with a test covering it.

---

## A1.4 — Real-world quality is unmeasured

**Type** build · **Effort** days · **Depends on** A1.5 · **Blocks** A3.4, A3.7

> Rewritten after A1.5 was added. Earlier drafts made session survival the
> primary metric; completion supersedes it. Survival is now a diagnostic.

**Files**
- `day_planner_agent/habit_tools.py` → `review_habit_week`
- `day_planner_backend_app/app/services/turn_log.py`

**Problem**

Everything in A3 is synthetic. Nothing measures whether the agent's placements
work for real people. With A1.5 in place the signal is now genuine rather than
a proxy — it just isn't being recorded anywhere.

**Scope**

1. Emit per-session outcomes as telemetry in addition to returning them to the
   model. Do not change the tool's return shape beyond what A1.5 already
   changed — A4.3 and the instruction depend on it.
2. **Primary metric: completion rate.** Share of placed sessions marked
   `completed`, with `pending` reported as a separate third bucket and never
   folded into failure. Slice by habit, hour of day, day of week, and whether a
   zone constrained the placement.
3. **Secondary, diagnostic only: survival rate** — share still present and
   unmoved. Useful for explaining *why* a session wasn't completed, not for
   claiming it was. Never report survival as a success measure.
4. Two implicit signals from the turn record, requiring nothing from the user:
   - user immediately restates or corrects in the next turn (a negative on
     comprehension rather than on placement)
   - turn abandonment
5. Tag every record with `source: organic | push` so the dataset stays
   consistent when B2.2's change-detection path feeds it later.

**Scope boundary — read this before designing**

Both metrics are **opt-in biased, in different ways**, and the bias must be
documented rather than smoothed over. Completion is observed only for sessions
someone actually marks. Survival is computed only when a review runs. Report
denominators alongside every rate so a reader can see the coverage.

**Do not build a scheduled polling job** to re-check calendar state fleet-wide.
It duplicates work specified in [2-system.md](2-system.md) task B2.2, which
detects deletions properly via `syncToken` incremental sync — deleted events
arrive with `status: "cancelled"` — with B3.1 supplying push notifications so a
poll is unnecessary. Polling is also expensive in the wrong currency: Google
Calendar API quota is **per-project**, so fleet-wide telemetry polling competes
with interactive users for the same limit.

**Rapid-delete detection belongs to B2.2**, not here. A1.5 largely removes the
need for it — an explicitly skipped session is a better signal than a deleted
event ever was.

**Out of scope**

- Do not build user-facing analytics.
- Do not add thumbs up/down. A1.5's explicit marks already provide the signal.
- Do not let any aggregation job call the model. Read-only.
- Do not add Cloud Scheduler, a new internal endpoint, or new service-to-service
  IAM in this task. Phase A1 is instrumentation; standing infrastructure belongs
  to Roadmap 2.

**Acceptance**

- [ ] Completion rate is queryable with the four slices, with `pending` as its
      own bucket and denominators reported alongside every rate.
- [ ] Survival rate is queryable and labelled in the query documentation as
      diagnostic, not a success measure.
- [ ] Restatement and abandonment signals are captured from the turn record.
- [ ] Records carry `source`.
- [ ] The coverage caveat is written into the query documentation, so nobody
      later reads either rate as a fleet-wide figure.

---

# Phase A2 — Cheaper, faster turns

Week 2–4. Runs in parallel with A3.

## A2.1 — Fresh token and TLS handshake on every backend call

**Type** defect · **Effort** days · **Highest payoff in this roadmap**

**Files**
- `day_planner_agent/backend_client.py` → `_mint_id_token`, `_client`

**Problem**

```python
async def _client() -> httpx.AsyncClient:
    token = await _mint_id_token()      # metadata server round trip, every call
    return httpx.AsyncClient(...)       # new TLS handshake, every call
```

A planning turn makes roughly 28 backend calls. That is 28 metadata-server
fetches and 28 TLS handshakes, all serial, all avoidable. OIDC ID tokens are
valid about an hour; `httpx.AsyncClient` is designed to be long-lived.

**Scope**

1. Cache the minted ID token with an expiry margin (refresh at ~55 minutes).
   Guard the refresh with an `asyncio.Lock` so concurrent calls don't stampede
   — mirror the double-checked pattern already used in
   `agent_client.py` → `_get_app`.
2. Hold one module-level `httpx.AsyncClient` with connection pooling, created
   lazily on first use, not at import.
3. Update every call site — all fifteen functions currently do
   `async with await _client() as client`. They must stop closing the shared
   client.
4. Keep the `Authorization` header per-request rather than baked into the
   client, since the token rotates.

**Out of scope**

- Do not add retries here. That is A2.3.
- Do not change any function signature or return shape.
- Do not introduce a new HTTP library.

**Acceptance**

- [ ] Token is minted once across many sequential calls within the window.
- [ ] Token is re-minted after expiry.
- [ ] One client instance is reused; no `RuntimeError: client has been closed`.
- [ ] All existing agent tests pass.

**Verify**

```bash
cd day_planner_agent && python -m pytest tests/ -v
```

---

## A2.2 — Serial fetches and repeated lookups

**Type** defect · **Effort** days

**Files**
- `day_planner_agent/calendar_tool.py` → `get_calendar_events`, `add_calendar_event`

**Problem**

Two separate inefficiencies:

1. `get_calendar_events` builds `tokens_by_account` with a dict comprehension
   that awaits inside the loop — access tokens are fetched one account at a
   time. It then loops calendars fetching events one at a time.
2. `add_calendar_event` calls `backend_client.list_calendars` and
   `_fetch_calendar_list_entry` on **every** invocation. Placing seven habit
   sessions in one turn repeats identical lookups seven times.

**Scope**

1. `asyncio.gather` the per-account token fetches.
2. `asyncio.gather` the per-calendar event fetches. Preserve the existing
   error semantics — an `HttpError` currently aborts with
   `{"status": "error"}`; use `return_exceptions=True` and reproduce that
   behaviour deliberately rather than changing it by accident.
3. Preserve the final `events.sort(key=...)` — gather does not guarantee order.
4. Memoize `list_calendars` and `calendarList` entries per invocation in
   `tool_context.state`, keyed so entries cannot leak across users or
   invocations.

**Out of scope**

- Do not cache across turns or sessions. Per-invocation only — calendar
  selection can change between turns.
- Do not parallelise the *writes* in `add_calendar_event`. Ordering and
  conflict behaviour matter there; A4.6 addresses multi-write integrity.

**Acceptance**

- [ ] Multi-calendar fetch issues requests concurrently.
- [ ] Events remain correctly sorted.
- [ ] A turn placing 5 events performs 1 `list_calendars` call, not 5.
- [ ] Error behaviour on `HttpError` is unchanged.

---

## A2.3 — Idempotency and retries, together

**Type** build · **Effort** week · **Blocks** Roadmap 2 entirely

**Files**
- `day_planner_agent/calendar_tool.py` → `add_calendar_event`, `_insert_google_event`
- `day_planner_agent/backend_client.py`

**Problem**

Two defects that must be fixed as one change.

There is no retry or backoff anywhere in the codebase, so a transient 503
surfaces as `{"status": "error"}` and the model improvises a recovery.

But `add_calendar_event` also has no idempotency key. The only thing preventing
a duplicate gym session is `instruction.md` telling the model to "check first"
— a judgement, not a constraint. **Adding retries without keys creates
duplicate calendar events on real user calendars.**

**Scope**

1. Generate a deterministic idempotency key for calendar writes. Google
   Calendar accepts a caller-supplied event `id` on insert — use a stable hash
   of `(user_id, habit_id, calendar_id, planned_start)` so the same logical
   session insert twice lands on the same event rather than creating two.
   Verify the id-format constraints Google imposes before choosing an encoding.
2. Handle the "already exists" response as success, not error.
3. Only then add retry with jittered exponential backoff for transient classes
   (429, 5xx, connection errors) in the tool layer.
4. Do **not** retry 4xx other than 429.
5. **Add 401 handling for A2.1's cached token.** A2.1 caches the OIDC token for
   55 minutes with no invalidation path, so if the internal service rejects it
   early — clock skew, a service-account change, an audience mismatch after
   A6.2 splits the clients — every subsequent call fails until the TTL elapses.
   On a 401 from the internal backend, clear the cached token, re-mint once, and
   retry the request exactly once. A second 401 is a real failure, not a stale
   token; do not loop.
5. Retries apply to reads freely; writes may only retry once keys are in place.

**Reference**

`day_planner_backend_internal/app/db/store.py` → `upsert_habit_session` already
gets this right, keying on `habit_session_id_for(calendar_id, event_id)`.
Extend the same thinking to the calendar write itself.

**Out of scope**

- Do not add a retry queue or durable retry store. In-process retry only;
  durable retry is Roadmap 2's Cloud Tasks.
- Do not change `update_calendar_event` / `delete_calendar_event` semantics —
  those are already keyed by an existing `event_id`.

**Acceptance**

- [ ] Inserting the same logical session twice yields one calendar event.
- [ ] A simulated 503 retries and eventually succeeds.
- [ ] A 404 does not retry.
- [ ] Retry attempts appear in A1.1's turn record.

---

## A2.4 — Memory writes block the turn

**Type** defect · **Effort** days

**Files**
- `day_planner_agent/memory_tools.py` → `update_profile`, `save_memory`

**Problem**

Both pass `config={"wait_for_completion": True}`, so a server-side LLM
extraction pipeline runs synchronously inside the user's turn. The module
docstring explains why the flag is set — ADK's wrapper silently drops it and
the write no-ops — and that reasoning is correct. The cost is multi-second
stalls on a turn already fighting a latency budget.

**Scope**

1. Move the write off the request path so the tool returns as soon as the write
   is *accepted*, not *completed*.
2. Keep `wait_for_completion: True` where the write actually executes — the
   original bug must not be reintroduced.
3. The tool's response to the model must not claim success it cannot
   guarantee. Prefer "saving" over "saved" if the write is now asynchronous,
   and update the docstring accordingly since the docstring is the model's
   contract.
4. Failures must be logged and retried, not silently dropped.

**Out of scope**

- Do not migrate `vertexai.Client` to `agentplatform.Client`. That is tracked
  separately in `docs/known-issues.md` and has its own verification burden.
- Do not restructure the profile schema.

**Acceptance**

- [ ] `update_profile` returns without waiting for extraction.
- [ ] The write still completes and is observable.
- [ ] A write failure is logged, not swallowed.

---

## A2.5 — 11,330 identical tokens re-sent every model call

**Type** build · **Effort** days · **Largest single cost lever**

**Files**
- `day_planner_agent/agent.py`

**Problem**

The instruction (~6,270 tokens) plus 17 tool schemas (~5,060 tokens) form a
fixed prefix sent on every model call, 5–15 times per user message, before any
conversation history.

**Scope**

1. Enable Vertex AI context caching for the stable prefix.
2. Note the constraint: `_build_instruction` injects `{today}`, the preloaded
   profile, and preloaded zones — all of which vary. Structure the prompt so
   the genuinely static part (the rules and the tool schemas) is cacheable and
   the varying part sits outside the cached region. This may require reordering
   `instruction.md` so the volatile injections move to the end.
3. Measure cache hit rate and confirm the cost reduction in A1.2's data.

**Out of scope**

- Do not shorten the instruction in this task. A4.3 does that, deliberately and
  with evals. Caching and shrinking are independent wins that compound.

**Acceptance**

- [ ] Cache hit rate is observable.
- [ ] Measured input-token cost per turn drops.
- [ ] Behaviour is unchanged — run the A3.1 suite before and after if it exists by then.

---

# Phase A3 — Test behaviour

Week 2–6. **Blocked by A0.3 (CI) and A0.4 (clock).** Parallel with A2.

Evaluation here has three layers, and it is worth being explicit that they
answer different questions:

| Layer | Answers | Speed | Gates |
|---|---|---|---|
| Constraints (A3.1, A3.2) | did it break a rule | minutes | yes, 100% |
| Decisions (A3.5, A3.6) | did it choose sensibly | minutes | direction only, warns |
| Outcomes (A1.4, A3.7) | did it actually work | weeks | never — but it is the truth |

A suite with only the first layer will happily pass an agent that always picks
the same defensible-but-poor slot. That is the gap A3.5 and A3.6 exist to close.

**Execution order within this phase**, which is not the numeric order — the
numbering is append-only so that references from A0.5, A1.1 and A1.4 stay
valid: A3.1 → A3.2 → A3.5 → A3.6 → A3.3 → A3.4 → A3.7. A3.7 comes weeks later,
once A1.4 has accumulated production data.

## A3.1 — No test covers agent behaviour

**Type** build · **Effort** 2 weeks · **Blocks** all of A4

**Files**
- `day_planner_agent/evals/` (new): runner, invariants, scenarios
- `day_planner_agent/tests/conftest.py` for shared stubs

**Problem**

All 140 tests cover tools. None asserts that the agent *places a session in a
legal slot*, which is the product. Every instruction change ships unmeasured,
which is why the prompt can only grow.

**Scope**

1. **Assert on tool calls and arguments, never on wording.** Model output is
   nondeterministic free text; the agent's actions are its contract.
2. Keep the model real — it is the thing under test. Stub `backend_client` and
   the Google Calendar client to serve fixture state deterministically. No test
   touches a real calendar.
3. Scenario format — declarative, with a pinned `today` (A0.4):

```yaml
name: does not place a session during work hours
given:
  today: "2026-08-17"
  zones:
    - {label: Work, start: "09:00", end: "17:30", days: [Mon, Tue, Wed, Thu, Fri]}
  sleep_schedule: {sleep: "23:00", wake: "07:00", cool_down: 30}
  habits:
    - {label: Gym, goal: "180 min/week, sessions 30-60 minutes"}
  calendar_events: []
when:
  user_says: "plan my gym sessions for this week"
expect:
  tool_calls:
    - {name: add_calendar_event, min_count: 3}
  invariants:
    - no_session_overlaps_any_zone
    - no_session_overlaps_sleep_or_cooldown
    - every_habit_session_passes_habit_id
    - placed_minutes_meets_target
```

4. Build **shared named invariants**, not per-scenario assertion code. Twenty
   scenarios sharing a predicate library is maintainable; twenty with bespoke
   assertions is not. Two distinct families:

   **Constraint invariants** — tier 1, "did it break a rule":
   - `no_session_overlaps_any_zone`
   - `no_session_overlaps_sleep_or_cooldown`
   - `every_habit_session_passes_habit_id`
   - `no_habit_id_on_plain_appointment`
   - `placed_minutes_meets_target`
   - `zone_anchored_sessions_match_zone_times`

   **Decision invariants** — tier 2, "did it choose sensibly". These are what
   separate *legal* from *good*:
   - `chosen_slot_ranks_above_median` — needs A4.1's scorer; until then, assert
     the weaker property that the chosen slot is not in the bottom quartile of
     legal candidates by day-load
   - `heavier_load_on_lighter_days`
   - `weekend_preferred_when_weekend_is_free`

   Do not ship A3.1 believing the constraint family alone evaluates decisions.
   It evaluates compliance. A3.5 and A3.6 extend the second family.
5. **Run each scenario 3–5 times and gate on pass rate, not a single pass.**
   This is the main thing separating an eval suite from a unit test suite.
6. Three tiers with different gates:

| Tier | Example | Gate |
|---|---|---|
| Hard constraint | no session inside a zone or sleep window; `habit_id` present on habit sessions and absent on plain appointments | 100%, blocks release |
| Behavioural | calls `review_habit_week` before planning; creates two habits for two stated goals; asks rather than guesses on a real conflict | ≥90%, warns |
| Quality | is the summary clear, is the placement sensible | trend only, never gates |

7. **Build the zone and sleep-window adherence scenarios first**, before any
   other category. Reason in the note below — they cover the one regression
   nothing else in this roadmap can currently detect, and they are also the
   scenarios that matter most in production, since these are the constraints
   `instruction.md` calls "hard constraints, not soft suggestions."
8. Source the first ~30 scenarios from: (a) `instruction.md` walked paragraph
   by paragraph — every rule is an assertion waiting to be written; (b) the
   behavioural requirements in `docs/todo.md` §1, which that document already
   says are literal acceptance criteria.
9. Wire tier 1 into CI on every commit; full suite nightly and on any
   `instruction.md` change.

**The baseline is being taken post-reorder — know what that means**

A2.5 moved `{today}`, `{profile_section}` and `{zones_section}` from the top of
`instruction.md` to the very end, so the instruction-plus-tools prefix stays
byte-identical across requests and the implicit cache can hit. It was verified
as a pure reorder (27,196 → 27,207 bytes, paragraph sets otherwise identical),
but it shipped with **no behavioural verification at all**, because this suite
did not exist yet. Its own acceptance criterion said "run the A3.1 suite before
and after if it exists by then" — it didn't.

So the number recorded here is a *post-reorder* baseline. It becomes the
reference point for everything in A4, and on its own it can never reveal whether
moving the guardrails to the end of a 6,270-token prompt changed how well they
are obeyed. Record that fact alongside the number so nobody later reads the
baseline as validating the state of the world before it.

**Optional, and worth the hour:** once the suite runs, check out the
pre-reorder `instruction.md` from git into a scratch branch and run the zone and
sleep scenarios against both orderings. That retroactively closes the one gap
A2.5 could not close for itself, and it is cheap the moment the harness exists.
If the orderings differ materially, that is a finding about prompt structure
worth having before A4.3 starts cutting rules.

**Out of scope**

- LLM judges belong **only** in tier 3 and must never gate. They are too noisy
  to block on and genuinely useful as a trend line.
- Do not modify `instruction.md` in this task. Establish the baseline against
  the current prompt — that number is the reference point for all of A4.
- Do not build a UI.

**Acceptance**

- [ ] ~30 scenarios run end to end.
- [ ] A recorded baseline pass rate exists per tier, committed, **annotated
      as post-A2.5-reorder** so it is never mistaken for a pre-reorder
      reference point.
- [ ] Tier 1 runs in CI and blocks on violation.
- [ ] Full-suite runtime and token cost are documented.

---

## A3.2 — Clean fixtures hide failure modes

**Type** build · **Effort** days

**Files**
- `day_planner_agent/evals/scenarios/`

**Problem**

Fixtures that always return successfully would never have caught A0.2 — the
agent looks perfectly behaved in every test while failing open in production.

**Scope**

1. At least one scenario where the zone fetch **fails**, asserting the agent
   does not proceed as though no zones exist.
2. One where the calendar fetch returns `needs_auth`, asserting the agent hands
   over the connect URL and stops rather than improvising.
3. One where a calendar is read-only (`not_writable`), asserting the agent
   reports it rather than silently choosing a different calendar.

**Out of scope**

- Do not test transport-level retry behaviour here; that is A2.3's unit tests.
  These scenarios test what the *agent* does when a tool reports failure.

**Acceptance**

- [ ] Reverting A0.2's fix causes the zone-failure scenario to fail.

---

## A3.3 — Model choice is currently unanswerable

**Type** build · **Effort** week · **Depends on** A3.1, A0.5

**Files**
- `day_planner_agent/evals/` runner

**Problem**

"Is Flash the right model?" cannot be answered today. A suite that tests the
agent with one hard-coded model says nothing about the alternatives — including
whether *keeping* Flash is correct.

**Scope**

1. Parameterise the eval runner by model.
2. Emit a comparison matrix rather than a pass/fail: per-tier pass rate ×
   p50/p95 latency × cost per scenario, for each candidate.
3. Make three decisions evidence-based: whether to change tier at all; whether
   routing planning turns to a stronger model raises tier 1 enough to justify
   the complexity; whether an offered model upgrade is safe to take.
4. **Re-run the matrix after A4.3.** The entire argument for moving interval
   arithmetic into code is that it removes what Flash is weakest at — this is
   how you find out whether that actually happened.

**Out of scope**

- Do not implement routing in this task. This produces the evidence; routing is
  a separate decision with its own complexity cost, and ADK sub-agents are the
  mechanism if it is ever justified.

**Acceptance**

- [ ] The suite runs against at least two models and produces a comparison.
- [ ] Cost per scenario is reported, not just pass rate.

---

## A3.4 — Results that don't accumulate

**Type** build · **Effort** days

**Files**
- `day_planner_agent/evals/` reporting
- BigQuery dataset from A1.2

**Problem**

A pass rate compared only against the immediately preceding run catches sudden
breakage and misses slow drift. With a model alias that can move (A0.5), a
silent provider-side change looks identical to a gradual prompt regression.

**Scope**

1. Persist every eval run — commit SHA, model version, per-tier pass rate,
   tokens, latency — into the same BigQuery dataset as A1.2, so agent quality
   and production telemetry sit side by side.
2. A step change with no corresponding commit is the detector for a model that
   moved underneath you. Make that query easy.
3. Define and document the path from a production failure (A1.4 data) to a new
   regression scenario, and use it at least once. Without this the suite freezes
   at whatever thirty scenarios were written in week three while real failures
   keep arriving.

**Out of scope**

- No alerting on eval drift until a few weeks of history exist.

**Acceptance**

- [ ] Every CI eval run is persisted and queryable.
- [ ] At least one scenario in the suite originated from a real production trace.

---

## A3.5 — Static scenarios can't tell reasoning from habit

**Type** build · **Effort** week · **Depends on** A3.1

**Files**
- `day_planner_agent/evals/scenarios/perturbations/`
- `day_planner_agent/evals/` runner — perturbation support

**Problem**

Every scenario in A3.1 is static: one fixture, one assertion set. An agent that
*always* places gym on Tuesday at 6am passes every constraint invariant in any
fixture where Tuesday 6am happens to be legal. Static tests cannot distinguish
an agent reasoning from the inputs from one pattern-matching to a default —
and that distinction is the entire question when evaluating decisions.

**Scope**

1. Extend the scenario format with a `perturb` block: take a base scenario,
   change **exactly one** input, and assert the decision moves in the expected
   direction.
2. **Assert on direction and properties, never on an exact replacement slot.**
   The claim under test is "the agent responded to this input," not "the agent
   produced this specific answer."
3. Each perturbation asserts two things: the base decision is no longer chosen,
   **and** the new decision still satisfies every tier 1 constraint invariant.
   A perturbation that fixes one problem by creating another is a failure.
4. Minimum set, one per constraint type:

| Perturbation | Expected response |
|---|---|
| Add a conflicting event at the chosen slot | session moves or shortens |
| Add a zone covering the chosen slot | session moves outside it |
| Extend sleep or cool-down over the chosen slot | session moves |
| State "no physical activity after 8pm" | evening placements disappear |
| Fill the weekend | weekend-loading preference yields, calendar wins |
| Raise the habit's weekly target | more sessions, or longer ones |
| Add `allowed_zones: ["Work"]` to the habit | placements may now appear in work hours |

5. Run at the same 3–5× repetition and gate at tier 2 (≥90%). Direction should
   be reliable, but one flip is not a release blocker.

**Out of scope**

- Do not perturb two inputs at once. Single-variable change is what makes the
  result attributable.
- Do not assert which specific alternative slot was chosen.
- Do not add perturbations for rules that have no corresponding instruction
  text — you would be testing an expectation the agent was never given.

**Acceptance**

- [ ] At least seven perturbation pairs implemented.
- [ ] **Each perturbation is verified to fail when its corresponding rule is
      removed from `instruction.md`.** A perturbation that still passes with the
      rule deleted is testing nothing — this is the meta-test that keeps the
      suite honest, and it doubles as a dry run for A4.3.

---

## A3.6 — Explanations and process go unchecked

**Type** build · **Effort** days · **Depends on** A3.1, A1.1

**Files**
- `day_planner_agent/evals/invariants/`

**Problem**

Two decision-quality failures that are mechanically checkable and currently
unchecked.

`instruction.md` requires the agent to explain why it placed each session.
Nothing verifies the explanation is *true*. An agent that says "I avoided
Thursday because of your work zone" when no Thursday work zone exists is
confabulating — a real failure, invisible to every current assertion.

Separately, nothing verifies the agent did the lookups its decision should rest
on. A1.1's tool-call trace makes this checkable for the first time.

**Scope**

1. **Explanation consistency.** Cross-check named entities in the agent's final
   reply against fixture state: zone labels, day names, habit labels, times it
   claims to have avoided. If it names a constraint as the reason, that
   constraint must exist and must actually cover the slot in question.
2. Keep this **structural, not semantic** — entity matching against known
   fixture values, not parsing arbitrary reasoning. A cheap deterministic check
   that catches blatant confabulation beats a fragile semantic parser, and it
   costs nothing per run.
3. **Process invariants** over the tool-call trace:
   - `get_calendar_events` covers the full period being planned, before any
     `add_calendar_event`
   - `review_habit_week` is called for the genuinely *preceding* period when the
     habit has prior sessions — assert the date range, not merely that it was
     called
   - `list_habits` is called before placing, rather than relying on the
     preloaded profile
   - no write precedes the reads that should inform it
4. Both families are tier 2.

**Out of scope**

- Do not use an LLM to judge explanations. Entity cross-check only. LLM
  judging stays in tier 3 and never gates.
- Do not assert on phrasing, tone, or length.

**Acceptance**

- [ ] A scenario where the agent cites a non-existent zone fails the check.
- [ ] Process invariants fail when tool-call ordering is scrambled in a fixture.

---

## A3.7 — The suite could score well and mean nothing

**Type** build · **Effort** days · **Depends on** A1.4 (several weeks of data), A4.1

**Files**
- `day_planner_agent/evals/` reporting
- BigQuery dataset from A1.2

**Problem**

Every layer above this one is synthetic. If the properties the suite rewards do
not actually predict whether a user *completes* a session, you are optimising a
proxy — and nothing in the roadmap would tell you. This task is what makes the
synthetic layers trustworthy rather than merely reassuring.

**Scope**

1. Once A1.4 has accumulated several weeks of data, bucket real placements by
   the engine score they would have received (A4.1) and compare **completion
   rate** across buckets. Use completion, not survival — survival measures plan
   durability and would let a high-scoring slot look good simply because nobody
   deleted it.
2. If high-scoring placements are not completed more often, **the scoring
   function encodes the wrong preferences.** Fix the engine, not the model. That
   inversion is the most valuable thing this task can find.
3. Do the same per instruction rule: for each rule with a corresponding eval
   scenario, check whether compliance correlates with completion.
4. Exclude `pending` sessions from both numerator and denominator rather than
   treating them as failures, and state the resulting sample size.
4. Feed results back into A4.3. A rule that does not correlate with real
   outcomes is a much stronger deletion candidate than one that merely "didn't
   move the eval numbers."

**Out of scope**

- Do not gate anything on this. It is a periodic review, not CI.
- Do not act on weak correlations from small samples. Note them and wait.

**Acceptance**

- [ ] A written correlation review exists, with sample sizes stated.
- [ ] At least one engine scoring weight or instruction rule is adjusted, or
      explicitly confirmed, as a result. The loop has to actually close.

---

# Phase A4 — Shrink

Week 5–8. **Blocked by A3** — you cannot safely delete a rule you cannot test.

## A4.1 — Constraint solving happens in prose

**Type** build · **Effort** 2 weeks · **Blocks** A4.2, A4.6, Roadmap 2 B3.2

**Files**
- `day_planner_agent/scheduling/` (new package) — pure functions
- `day_planner_agent/tests/test_scheduling.py` (new)

**Problem**

Paragraph 17 of `instruction.md` is roughly 900 words asking Gemini Flash to
perform interval arithmetic across zones, sleep, cool-down and wake-up windows,
apply per-habit `allowed_zones` exceptions, size sessions by daily load, weight
weekends, and account totals against a target. It is visibly straining — the
zone-anchored case is spliced in with *"skips everything in this paragraph from
here on,"* a branch statement written in English.

**Scope**

Build as **pure functions with no agent involvement in this task**:

1. `free_intervals(date_range, zones, sleep_schedule, events, allowed_zones)`
   — the core. Must handle:
   - zone windows filtered by `days_of_week`
   - the sleep period `[sleep_time, wake_time)` using that day's `day_overrides`
     if present, else the default. **Never overridable by anything.**
   - `cool_down_minutes` before sleep and `wake_up_buffer_minutes` after wake,
     both overridable via the fixed labels `"cool-down"` / `"wake-up"` in
     `allowed_zones`
   - existing busy events
   - midnight-crossing sleep windows
2. `zone_occurrences(zone, date_range)` — enumeration for zone-anchored habits.
3. `target_accounting(habit_goal, placed_sessions)` — parse target, sum placed
   minutes, report remaining.
4. `score_candidates(intervals, day_loads, prior_review)` — day-load sizing,
   weekend preference, repeat-bump penalty.
5. `collisions_with(new_constraint, placed_sessions)` — interval overlap for
   the "you just created a zone that conflicts" case.

Unit-test each exhaustively including DST transitions, midnight crossings,
zero-length windows, and overlapping zones.

**Out of scope**

- **Do not touch `instruction.md` in this task.** No prompt changes at all.
- Do not register a tool yet. That is A4.2.
- Do not implement placement *choice* — this produces candidates and scores;
  choosing among them stays with the model.

**Acceptance**

- [ ] All five functions implemented with unit tests.
- [ ] DST and midnight-crossing cases covered explicitly.
- [ ] Zero changes to `instruction.md` or `agent.py` in this task's diff.

---

## A4.2 — Verify the engine before depending on it

**Type** build · **Effort** week · **Protect this step from schedule pressure**

**Files**
- `day_planner_agent/scheduling_tool.py` (new)
- `day_planner_agent/agent.py` tool registration

**Problem**

Swapping prose for a tool in one step is a rewrite you hope worked.

**Scope**

1. Register `get_available_slots(date_from, date_to, habit_id, min_minutes,
   max_minutes)` returning ranked candidates, each carrying `start`, `end`,
   `score`, `reasons[]` (e.g. `"weekend"`, `"light day"`, `"bumped twice last
   week"`), `constraints_applied[]`, and `remaining_target_minutes`.
2. `reasons` is what preserves the product's explain-your-placement behaviour
   without the model re-deriving it. Do not omit it.
3. **Shadow mode: change no instruction text.** The tool is exposed; the prompt
   still tells the model to derive slots itself.
4. Log where the engine's candidates disagree with what the model actually
   chose. Disagreements are either engine bugs or undocumented prompt rules —
   both must be found before anything depends on the tool.
5. Run for long enough to gather real disagreement data before proceeding.

**Out of scope**

- Do not remove any instruction text in this task. That is A4.3.
- Do not gate placement on the tool's output yet.

**Acceptance**

- [ ] Tool is registered and callable.
- [ ] Disagreement rate is measured and reviewed.
- [ ] A3.1 pass rates are unchanged versus baseline (nothing regressed).

---

## A4.3 — Reduce the instruction

**Type** build · **Effort** ongoing

**Files**
- `day_planner_agent/instruction.md`

**Problem**

~6,270 tokens, growing monotonically, rule interference rising with it.

**Scope**

**Cut one rule at a time, re-running the A3.1 suite after each cut.** A rule
whose removal doesn't move the numbers was doing nothing. One whose removal
drops them was load-bearing and needs its behaviour reproduced in the engine
first.

Moves to code (already built in A4.1):

- deriving free intervals from zones, sleep, cool-down, wake-up, `allowed_zones`
- zone-anchored habit expansion
- accounting placed minutes against the target
- session sizing by day load; weekend loading preference
- repeat-bump detection
- finding sessions a newly created zone collides with

**Stays in prose** — these are genuinely model work, do not remove them:

- classifying an utterance as habit vs. zone vs. profile preference vs. memory
- when a situation is a real conflict worth asking about vs. a routine choice
- resolving "tomorrow", "this Saturday", "next week"
- deriving commute times from "commute is 30 minutes" anchored to work hours
- reading a review outcome as "guardrails worked" vs. a real signal
- recognising "I have work 8 to 17 tomorrow" as a calendar instruction rather
  than thinking aloud
- tone, confirmation, explaining why a placement was made

**Out of scope**

- Do not remove more than one rule per eval cycle. The whole point is knowing
  which change caused which movement.
- Do not touch the tenancy or `user_id` guidance.

**Acceptance**

- [ ] Instruction token count reduced toward ~2,500.
- [ ] Tier 1 pass rate at 100%; tier 2 at or above baseline.
- [ ] Each cut is a separate commit with its eval numbers in the message.

---

## A4.4 — 17 tool schemas on every call

**Type** build · **Effort** week

**Files**
- docstrings across `calendar_tool.py`, `habit_tools.py`, `zone_tools.py`, `memory_tools.py`
- `day_planner_agent/agent.py` tool registration

**Problem**

~5,060 tokens of schema sent unconditionally; most turns need three tools. The
docstrings are excellent documentation and expensive schema simultaneously.

**Scope**

1. Trim model-facing docstring text to what the model needs to choose and call
   correctly. Move the rest into comments — the detail is valuable to humans and
   costly to the model.
2. Gating tool exposure by phase (zone setup vs. daily planning are
   near-disjoint) is **conditional** — read the caching warning below before
   doing it.
3. Re-run A3.1 after trimming. Docstrings *are* behaviour — a trimmed docstring
   can change tool selection.

**Warning: gating tools fights A2.5's context caching**

Tool schemas are part of the cached prefix, not something separate from it.
A2.5 reordered `instruction.md` so the whole instruction-plus-tools prefix is
byte-identical across requests, because an implicit cache hit only covers a run
that matches from the very start of the request. **A tool set that varies by
turn breaks that prefix at the schema boundary**, and every token after the
divergence stops being cached — which for most turns is the entire prefix.

The two halves of this task therefore pull in opposite directions:

- **Trimming docstrings (item 1) is unambiguously good.** It shrinks the prefix
  and it stays byte-identical, so caching and size improvements compound.
- **Gating exposure (item 2) trades a smaller prefix for a colder cache.** With
  cached tokens billed at a large discount, a stable 5,060-token schema block
  can easily cost less than a variable 2,000-token one.

Do item 1 first, ship it, and read the `cached_tokens` hit rate A2.5 added to
`turn_log_queries.sql` (query 11 — use the token-weighted figure, not per-turn).
Only then decide whether gating is worth it, using measured numbers rather than
the intuition that fewer tokens must be cheaper. If you do gate, gate on
something stable for a whole session rather than per-turn, so the prefix
diverges once rather than continuously.

**Out of scope**

- Do not remove the status-contract documentation from return descriptions. The
  model needs those to handle `needs_auth` / `not_writable` / `not_found`
  correctly, and A3.2 tests exactly that.
- Do not gate tool exposure without first measuring the cache hit rate both
  ways. This is the one change in the phase that can make cost *worse* while
  looking like an optimisation.

**Acceptance**

- [ ] Schema token count measurably reduced by trimming alone.
- [ ] Token-weighted cache hit rate measured before and after; it must not
      regress.
- [ ] If tools were gated, the decision is justified by measured total cost —
      cached and uncached tokens together — not by prefix size alone.
- [ ] No tier 1 or tier 2 regression.

---

## A4.5 — Unbounded tool output enters context

**Type** defect · **Effort** days

**Files**
- `day_planner_agent/calendar_tool.py` → `get_calendar_events`

**Problem**

Returns raw event lists with no cap, and each persists in conversation history
for the rest of the turn. Gets worse once A0.1 stops truncating.

**Scope**

1. Cap the number of events returned; summarise the remainder ("plus N further
   events between X and Y").
2. Prefer salient events — those overlapping the period being planned, and
   those carrying `habit_id`.
3. Make the cap configurable.

**Out of scope**

- Do not drop the `habit_id` field. `review_habit_week` and the instruction's
  conflict-detection paragraph depend on its presence.

**Acceptance**

- [ ] A 400-event range returns a bounded payload.
- [ ] All `habit_id`-carrying events survive the cap.
- [ ] No tier 1 regression.

---

## A4.6 — Multi-write turns have no transactional story

**Type** build · **Effort** week · **Depends on** A4.1, A2.3

**Files**
- `day_planner_agent/calendar_tool.py`
- `day_planner_agent/scheduling/`

**Problem**

"Plan my week" is seven independent `add_calendar_event` calls. If the fourth
fails, the user has a half-planned week and the model decides what to do next.

**Scope**

1. Plan-then-apply. With A4.1's engine the full plan is known before any write.
2. Validate the complete plan against all constraints *before* writing anything.
3. Apply with A2.3's idempotency keys so a retry rolls forward rather than
   duplicating.
4. On partial failure, report exactly what was and was not applied — do not
   leave the model to infer it.

**Out of scope**

- Do not implement rollback of already-created events. Roll *forward* with
  idempotency; deleting a user's calendar events to undo a partial failure is
  worse than reporting the partial state.

**Acceptance**

- [ ] A plan failing validation writes nothing.
- [ ] A failure mid-apply is reported precisely.
- [ ] Re-running the same plan creates no duplicates.

---

# Phase A5 — Standing debt

Ongoing. Not blocking; slot between phases.

## A5.1 — Fragile framework coupling

**Type** defect · **Effort** days

**Files**
- `day_planner_agent/memory_tools.py`
- `terraform/agent.tf`

**Problem**

`memory_tools.py` reaches into four ADK/SDK private attributes:
`tool_context._invocation_context`, `service._agent_engine_id`,
`service._project`, `service._location`. Separately, `agent.tf` hand-transcribes
ADK's `_AGENT_ENGINE_CLASS_METHODS` as a static snapshot requiring manual
re-verification on every `google-adk` bump.

**Scope**

1. Wrap all private-attribute access in one adapter module with a single point
   of failure.
2. Add a test that fails loudly if any accessed attribute disappears, pinned to
   the `google-adk` version in `requirements.txt`.
3. Add the same for the `agent.tf` class-methods list if it can be verified
   programmatically.

**Out of scope**

- Do not migrate to `agentplatform.Client` here — `docs/known-issues.md`
  correctly notes that migration must first confirm the replacement honours
  `wait_for_completion`, which is the entire reason these call sites bypass the
  ADK wrapper.

**Acceptance**

- [ ] Private access confined to one module.
- [ ] An ADK upgrade breaking any attribute fails a test rather than production.

---

## A5.2 — Coarse session model

**Type** build · **Effort** week · **Depends on** A1.2 data

**Files**
- `day_planner_backend_app/app/api/routes/chat.py`
- `day_planner_backend_app/app/core/config.py`

**Problem**

One session per user with a 6h idle rollover
(`agent_session_idle_timeout_seconds`). Context grows unboundedly inside the
window, so token cost grows quadratically with turns, and rollover drops
working context abruptly.

**Scope**

1. **Wait for A1.2 data before designing this.** Size the fix to the observed
   distribution of turns per session, not a guess.
2. Then consider compaction or summarisation within a session.

**Out of scope**

- Do not change the archival-to-Memory-Bank behaviour. That path is correct and
  well-reasoned.

**Acceptance**

- [ ] Design decision is justified by measured data, referenced in the commit.

---

## A5.3 — One service account for both the public and internal services

**Type** defect · **Effort** days · **Do before Roadmap 2 B1.1**

> Added after A1.5 shipped. Pre-existing condition, but A1.5 is the first
> change that actively depends on it.

**Files**
- `terraform/cloud_run.tf` → `google_service_account.backend` (line ~30), both
  `service_account =` assignments (lines ~57 and ~225), the invoker binding
  added by A1.5 (~line 355)
- `terraform/kms.tf`, `terraform/secrets.tf`, `terraform/firestore.tf` — all
  three grant to `google_service_account.backend`
- `day_planner_backend_internal` → `INTERNAL_CALLER_SERVICE_ACCOUNTS` env value

**Problem**

`day_planner_backend_app` and `day_planner_backend_internal` run as the **same**
identity, `google_service_account.backend`. That identity holds KMS decrypt on
the refresh-token key (`kms.tf`), Secret Manager access to the OAuth client
secret (`secrets.tf`), and Firestore access (`firestore.tf`).

The app service is the internet-facing one — it takes an `allUsers` invoker
binding the moment `publicly_exposed` flips to true, because Google's OAuth
redirect arrives with no GCP identity. So a compromise of the *public* service
yields the *internal* service's identity, and with it the credentials the
service split exists to protect. `docs/oauth-design.md` argues that split
carefully; a shared service account quietly undoes a good part of it.

This was latent before A1.5. That task added `backend` to
`INTERNAL_CALLER_SERVICE_ACCOUNTS` and granted it `roles/run.invoker` on the
internal service — necessary and correct for the user-facing completion route,
but it means the arrangement is now load-bearing rather than merely untidy.

**Scope**

1. Split `google_service_account.backend` into two: one for the app service,
   one for the internal service.
2. Move each IAM grant to the narrowest holder. KMS decrypt and Secret Manager
   access belong to the **internal** identity only — the app service has no
   business decrypting a refresh token. Firestore access is needed by both, but
   for different collections; grant both and note the asymmetry.
3. Update `INTERNAL_CALLER_SERVICE_ACCOUNTS` to list the app and agent
   identities, not the internal service's own.
4. Keep the app service's invoker binding on internal — A1.5's route depends
   on it. Only the identity changes, not the permission.
5. Verify with `terraform plan` that no grant silently widens. Splitting an SA
   is exactly the change where a copy-paste leaves both identities holding
   everything.

**Why before Roadmap 2**

B1.1 adds a third Cloud Run service (the worker). Doing this first means the
worker gets its own identity from the start rather than inheriting the shared
one and making the problem three-way.

**Out of scope**

- Do not change the service topology. The two-service split is correct and
  well-argued in `docs/oauth-design.md`; this is about identity, not structure.
- Do not change the application-level `require_internal_caller` check. Both the
  IAM binding and the allowlist stay — defence in depth is deliberate here.
- Do not attempt this in the same change as any application code.

**Acceptance**

- [ ] Each service runs as its own service account.
- [ ] The app service's identity has **no** KMS or Secret Manager grant.
- [ ] A1.5's `/me/habit-sessions/status` route still works end to end.
- [ ] `terraform plan` reviewed line by line to confirm no grant widened.

---

# Phase A6 — User-owned domain data

**Execute after A2, in parallel with A3, and before A4.2.** Numbering is
append-only (same convention as A1 and A3); this phase was added last but does
not run last. A4.2 registers `get_available_slots`, which reads zones and the
sleep schedule — building it before this phase means pointing it at a service
that is about to stop owning that data.

The agent is a sidekick, not the only way in. Today a user cannot create a
habit, define a zone, or set their sleep schedule without talking to the model,
because that data lives behind a service only the agent can reach. This phase
moves it to where a UI can reach it too, and shrinks the internal service back
to the credential vault its design document describes.

---

## A6.1 — Domain data sits behind the credential boundary

**Type** build · **Effort** 2 weeks · **Blocks** A6.2, A6.3 · **Affects** B5.1

**Files**
- `day_planner_backend_internal/app/db/store.py` → habit, habit-session, zone,
  sleep-schedule methods (roughly lines 490–730)
- `day_planner_backend_internal/app/db/models.py` → `Habit`, `HabitSession`,
  `Zone`, `SleepSchedule`, `habit_session_id_for`
- `day_planner_backend_internal/app/api/routes/internal.py` → the eleven domain
  routes listed below
- `day_planner_backend_internal/app/schemas/` → `habits.py`,
  `habit_sessions.py`, `zones.py`, `sleep_schedule.py`
- `day_planner_backend_app/app/db/store.py`, `db/models.py`, `schemas/` —
  receiving side

**Problem**

`day_planner_backend_internal` is named for *who calls it*, not *what it
protects*, and two unrelated concerns ended up inside it.

**Credentials** — refresh tokens, the KMS key, access-token minting, connect
links. This genuinely needs isolation, and `docs/oauth-design.md` argues it
carefully: the agent runtime should only ever hold a ~1 hour access token, and
the refresh token plus KMS key should be reachable from exactly one place.

**Planning domain data** — habits, habit sessions, zones, sleep schedule. This
is ordinary user data with no credential exposure whatsoever. It landed in the
internal service only because the agent needed it first and the agent's only
path was internal. That is an accident of ordering, not a security boundary.

The consequence is the one that prompted this phase: **a user cannot manage
their own habits, zones, or sleep schedule at all** except by asking the model
to do it. The data is structurally unreachable from any user-facing surface.
A1.5 already had to work around this, opening the first app→internal call just
so a person could tick "I did the gym."

Left alone, every UI feature repeats that workaround, and the internal service
drifts from "credential vault" to "the backend" — which erodes the blast-radius
argument the split exists for, and makes A5.3's identity separation harder to
justify.

**The move is code-only, not data**

Both services already point at the same Firestore database and the same
collection paths (`users/{uid}/habits/...`, `.../zones/...`, and so on — see
each `store.py`'s module docstring). **No document moves. No migration script.
No backfill.** Only which service's code owns the reads and writes changes.
Verify this before starting by confirming both services' `gcp_project_id` and
`firestore_database` settings resolve to the same database.

**Scope**

1. **Move to `day_planner_backend_app`:** the store methods, models, and schemas
   for habits, habit sessions, zones, and sleep schedule. Move them wholesale —
   do not reimplement, and do not leave a copy behind.
2. **Move these eleven routes off `internal.py`.** They become `/me/*` routes in
   A6.3 and `/agent/*` routes in A6.2:
   - `POST /internal/habits`, `GET /internal/habits`, `POST /internal/habits/update`
   - `POST /internal/habit-sessions`, `GET /internal/habit-sessions`,
     `POST /internal/habit-sessions/status`
   - `POST /internal/zones`, `GET /internal/zones`, `POST /internal/zones/update`
   - `GET /internal/sleep-schedule`, `POST /internal/sleep-schedule`
3. **Keep on `day_planner_backend_internal` — these four routes and nothing
   else:** `/internal/connect-link`, `/internal/calendars`,
   `/internal/access-token`, `/internal/disconnect`. Along with them stay
   `services/crypto.py`, `services/connections.py`, `providers/`, the
   `ConnectedAccount` and `Calendar` models, `oauth_states`, and the KMS grant.
4. Internal keeps `Store.get_user` only for `mint_access_token`'s
   `default_account_id` lookup. Everything else in its user/session/throttle
   code becomes dead once the domain routes leave — delete it rather than
   leaving it to rot.
5. **A1.5's `/me/habit-sessions/status` stops proxying.** It currently calls
   internal via `services/internal_client.py`; after this it writes directly.
   Delete `internal_client.py` if nothing else needs it — check whether the UI's
   connect-calendar flow still requires an app→internal path before removing it.
6. Move the tests with the code. `day_planner_backend_internal/tests/` loses its
   domain coverage and `day_planner_backend_app/tests/` gains it — no net loss
   in assertions.

**Out of scope**

- **Do not change any Firestore path, collection name, or document shape.** The
  moment this becomes a data migration it stops being safe to do in one step.
- Do not merge the two services. The split is correct; this corrects *where the
  line falls*, not whether there is one.
- Do not build user-facing routes here — A6.3 does that. This task ends with the
  domain owned by the app service and the agent still working.
- Do not touch `services/connections.py` or anything credential-adjacent.

**Acceptance**

- [ ] All eleven domain routes are gone from `internal.py`; the four credential
      routes remain and are unchanged.
- [ ] `day_planner_backend_internal` has no import of habit, zone, or
      sleep-schedule code.
- [ ] No Firestore path changed; existing documents are readable with no
      migration.
- [ ] Agent behaviour is unchanged end to end (A6.2 supplies its new path).
- [ ] All three suites pass, with domain tests now living in the app service.

---

## A6.2 — The agent needs a path to domain data on a public service

**Type** build · **Effort** week · **Depends on** A6.1

**Files**
- `day_planner_backend_app/app/api/routes/agent.py` (new)
- `day_planner_backend_app/app/api/deps.py` → service-caller dependency
- `day_planner_backend_app/app/core/config.py` → caller allowlist setting
- `day_planner_agent/backend_client.py` → split into two clients
- `terraform/cloud_run.tf` → agent SA invoker binding on the app service

**Problem**

After A6.1 the agent's domain data lives on `day_planner_backend_app`, which is
the internet-facing service — it takes an `allUsers` invoker binding once
`publicly_exposed` flips, because Google's OAuth redirect arrives with no GCP
identity. The agent needs authenticated service-to-service access to a service
that also serves anonymous browser traffic and user-session traffic.

That is workable, but it introduces the sharpest security question in this
roadmap: **two authentication schemes now live on one service.** `/me/*` routes
trust a session token and derive `user_id` from it. Agent routes trust an OIDC
service identity and take `user_id` in the body, because a trusted service is
acting on a user's behalf. If those ever cross, the consequences are severe in
both directions — a user session reaching an agent route means any signed-in
person can name any `user_id`; an agent identity reaching `/me` means the
tenancy derivation silently changes.

**Scope**

1. New route group on the app service, mounted under a distinct prefix
   (`/agent`), carrying the eleven domain operations from A6.1.
2. **Router-level dependency, not per-route.** Mirror
   `day_planner_backend_internal/app/api/deps.py`'s `require_internal_caller`:
   verify the OIDC token, check the caller's service account against an explicit
   allowlist, and attach it as `dependencies=[...]` on the router itself so a new
   route cannot be added unprotected by omission. The existing `/internal`
   router already does exactly this — copy the pattern.
3. **`/me` and `/agent` must never share a dependency, a router, or a
   `user_id` derivation.** Write a test asserting a valid session token is
   rejected on an `/agent` route, and a valid agent OIDC token is rejected on a
   `/me` route. Both directions.
4. Terraform: grant the agent's service account `roles/run.invoker` on the app
   service. Note this is separate from the `allUsers` binding — a public service
   still evaluates IAM for identified callers.
5. **Split `day_planner_agent/backend_client.py` in two**, since the agent now
   talks to two services with two different base URLs and audiences:
   - credentials client → internal: `connect_link`, `list_calendars`,
     `access_token`
   - domain client → app: habits, habit sessions, zones, sleep schedule
6. Both clients mint OIDC tokens for their **own** audience. A token minted for
   the internal service is not valid at the app service; getting this wrong
   produces a confusing 401 rather than an obvious error.
7. Carry A2.1's token caching and pooled client into both — do not reintroduce
   per-call minting in the new client.

**Out of scope**

- Do not expose domain operations on the app service without service auth,
  however convenient it looks during development.
- Do not reuse `/me` schemas for agent routes if they differ in `user_id`
  handling. A shared schema is how the two auth models leak into each other.

**Acceptance**

- [ ] Agent reaches all domain operations through the app service.
- [ ] A session token is rejected on `/agent/*`; an agent token is rejected on
      `/me/*`. Tested in both directions.
- [ ] The agent still reaches `/internal/*` for credentials, unchanged.
- [ ] Neither client mints a token per call.

---

## A6.3 — Users cannot manage their own habits, zones, or sleep schedule

**Type** build · **Effort** 2 weeks · **Depends on** A6.1

**Files**
- `day_planner_backend_app/app/api/routes/me.py` or new per-entity route modules
- `day_planner_backend_app/app/schemas/`

**Problem**

Every planning entity is currently agent-only. A user who wants to fix a wrong
work-hours zone, correct a sleep time, pause a habit, or tick off a session has
exactly one route: ask the model, in prose, and hope it maps the request onto
the right tool. That is a poor fit for editing structured data the user can
already see, and it makes the agent a bottleneck on its own configuration.

**Scope**

1. Full `/me` CRUD, session-authenticated, `user_id` from `current_user_id`
   only — the rule every existing `/me` route follows:
   - **Habits** — list, create, update. **No delete**: `update_habit`'s
     `status` field (`active`/`paused`/`archived`) is the retirement mechanism
     and exists so anything referencing a `habit_id` still resolves. Do not add
     a hard delete.
   - **Zones** — list, create, update, and delete. Zones have no referential
     obligation the way habits do, but see the warning below.
   - **Sleep schedule** — get and set. Singleton per user, create-or-update, no
     delete.
   - **Habit sessions** — list, and set status (A1.5's route, now direct).
2. **Validation must be shared with the agent path, not reimplemented.** Both
   `/me` and `/agent` routes call the same store methods and the same Pydantic
   models. Two implementations of "is this a valid `days_of_week`" is how the
   agent and the UI start disagreeing about what a zone is.
3. Preserve the existing partial-update semantics exactly: `update_habit` and
   `update_zone` return `None` for an unknown id so the route can 404 rather
   than silently creating a document under a caller-chosen id, and
   `set_sleep_schedule` replaces `day_overrides` wholesale rather than merging
   per-day. These are documented decisions in `store.py` — carry them over.
4. Zone changes have consequences the agent currently handles conversationally
   (see `instruction.md`'s paragraph on checking a new or changed zone against
   already-placed sessions). **A UI edit bypasses that check entirely.** Either
   surface the resulting conflicts in the response so the UI can show them, or
   record that the check is now missing on this path — do not leave it
   silently unhandled.

**Out of scope**

- Do not build the UI. This exposes the API.
- Do not add hard-delete for habits.
- Do not attempt to make the agent aware of UI edits mid-conversation. Session
  state staleness after an out-of-band edit is a real problem and belongs with
  A5.2's session work, not here.

**Acceptance**

- [ ] Every planning entity is creatable and editable without the agent.
- [ ] `/me` and `/agent` paths share store methods and validation.
- [ ] Unknown ids 404 rather than creating documents.
- [ ] Zone-edit conflict behaviour is either surfaced or explicitly recorded as
      a known gap.

---

## A6.4 — No password change, no password reset

**Type** build · **Effort** week · **Independent of A6.1–A6.3**

> Account management rather than domain data, grouped into this phase because
> it is the same theme: things a user must be able to do for themselves.

**Files**
- `day_planner_backend_app/app/api/routes/auth.py`
- `day_planner_backend_app/app/db/store.py` → reset-token methods
- `day_planner_backend_app/app/core/config.py` → TTLs
- `day_planner_backend_app/app/schemas/auth.py`

**Problem**

`store.py` has `update_password_hash`, but **nothing calls it.** There is no
route to change a password and no way to recover a forgotten one. A user who
forgets their password loses their account and every connected calendar with
it. For a product holding 30-day sessions (`session_ttl_seconds`), that is not
an edge case — it is the normal path back in after switching devices.

Related and worth noting while in this code: `email_verified` is written as
`False` at signup and never read anywhere. Password reset by email is only as
trustworthy as the address it sends to, so the two are connected — but
verification is a larger piece of work and is **not** in this task's scope.

**Scope**

**Password change** — authenticated:

1. `POST /me/password`, session-authenticated, requiring the **current**
   password in the body. Never allow a change on session possession alone; a
   borrowed unlocked laptop should not be an account takeover.
2. Verify the current password with the same comparison used at login, then
   call the existing `update_password_hash`.
3. **Invalidate the user's other sessions** on success, keeping the caller's
   own. This currently requires a query — `sessions/{token_hash}` is keyed by
   hash with no user index, so add a `user_id` index or an equivalent lookup.
   Changing a password without evicting other sessions provides much less than
   it appears to.

**Password reset** — unauthenticated:

4. `POST /auth/password-reset/request` taking an email. Generate a token,
   store **only its hash** in a new TTL'd collection with the `user_id` and an
   expiry — the exact pattern `create_session` already uses for session tokens.
   Email the raw token as a link.
5. `POST /auth/password-reset/confirm` taking token plus new password. Consume
   it in a transaction so it is genuinely single-use — mirror
   `consume_oauth_state`, which already solves this.
6. **Always return the same response** whether or not the address exists. A
   differing response or timing turns this endpoint into an account-enumeration
   oracle.
7. Short TTL — 30 to 60 minutes, far shorter than a session.
8. Rate-limit per email and per IP. `check_login_throttle` and
   `record_login_failure` are the existing pattern; reuse rather than invent.
9. Invalidate **all** sessions on a successful reset, including any caller's.
   A reset means "I lost control of this account."
10. **New dependency: outbound email.** Nothing in this codebase sends email
    today. This needs a provider decision, credentials in Secret Manager
    alongside the OAuth client secret, and Terraform wiring. Treat it as its
    own decision rather than a detail of this task, and flag it early if it
    needs approval.

**Out of scope**

- Email verification at signup. Related, larger, separate.
- Magic-link or passwordless login.
- MFA.
- Any UI.

**Acceptance**

- [ ] Password change requires the current password and evicts other sessions.
- [ ] Reset tokens are stored hashed, single-use, and TTL'd.
- [ ] Request endpoint responds identically for existing and non-existing
      addresses.
- [ ] Successful reset invalidates every session for that user.
- [ ] Both endpoints are rate-limited.
- [ ] The email provider choice is documented, with its secret in Secret
      Manager rather than an env var.
