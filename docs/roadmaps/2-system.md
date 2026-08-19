# Roadmap 2 — The System: Executable Task Specs

Companion to the overview at *Queue, Worker, Trigger*. How the agent gets
invoked: out of the HTTP request, then fed from a second trigger.

> **Read this before starting.** Two specs here — B3.2 and B4.1 — depend on the
> interface of the constraint engine built in Roadmap 1 task A4.1. That
> interface does not exist yet. Revisit and tighten those two specs once A4.1
> has landed rather than treating them as final. Everything else is stable.

## Hard prerequisites from Roadmap 1

Do not start this roadmap until all four are true. These are not
nice-to-haves; each one turns a specific failure mode from "impossible" into
"likely."

| From Roadmap 1 | Why it gates this work |
|---|---|
| **A2.3** idempotent calendar writes | A queue implies retries. A retry against `add_calendar_event` without keys double-books a real user's calendar. |
| **A4.1** constraint engine | Becomes B3.2's risk checker. Without it, every calendar notification costs a model call. |
| **A3.1** behavioural evals | Nobody is watching when the agent runs unprompted. |
| **A1** traces and alarms | A silent fleet-wide failure at 3am is exactly what `docs/known-issues.md` argues against shipping into. |

## The shape

One asynchronous execution path, fed by two triggers. Chat and calendar-change
each get their own entry point and queue, converging on a single worker. Build
the path once; proactive becomes wiring rather than a parallel system.

---

# Phase B1 — Turn-as-a-job

Week 1–3. Builds the worker that B3 reuses.

## B1.1 — The agentic loop runs inside the request

**Type** infra · **Effort** 2 weeks · **Blocks** everything else here

**Files**
- `day_planner_backend_app/app/api/routes/chat.py` → `chat`
- `day_planner_backend_app/app/db/store.py` → new `turns` collection methods
- new service: `day_planner_worker/` (or equivalent)
- `terraform/cloud_run.tf`, new Cloud Tasks queue resources

**Problem**

`/me/chat` awaits `agent_client.send_message` to completion before returning. A
client disconnect — phone backgrounds, network flap — kills the run mid-way
through calendar writes, leaving a half-planned week with no record. The
container slot is held for the whole turn. Roadmap 1's raised timeout is a
stopgap, not a fix.

**Scope**

1. `/me/chat` becomes: authenticate → `check_and_consume_quota` (unchanged) →
   write a `turns/{turn_id}` document with `status=queued` → enqueue a Cloud
   Task → return `202 {turn_id}`. Target under 100ms.
2. New Cloud Run worker service with a long timeout that receives the task and
   runs the agentic loop via the existing `AgentClient`.
3. Worker authenticates the Cloud Tasks caller via OIDC. Mirror the existing
   `require_internal_caller` pattern in
   `day_planner_backend_internal/app/api/deps.py`.
4. Preserve the session lifecycle exactly as it is today: idle rollover via
   `_is_idle`, `archive_session` before dropping, `set_agent_session` after.
   Move it into the worker unchanged.
5. Quota is consumed at enqueue, not in the worker — a queued turn has already
   spent its allowance, and a retry must not double-charge.

**Critical detail**

Set the Cloud Task's `dispatchDeadline` to **at least** the worker's Cloud Run
timeout. These are two independent limits. A shorter deadline retries a turn
that is still running — which produces duplicate work even with A2.3 in place,
because it is the same *turn* twice rather than the same *write* twice.

**Out of scope**

- Do not change `AgentClient` internals. It moves; it does not change.
- Do not implement the calendar trigger. That is B3.
- Do not remove `/me/chat/reset` — it stays synchronous, it is cheap.

**Acceptance**

- [ ] `/me/chat` returns 202 in under 100ms.
- [ ] A killed client connection does not affect turn completion.
- [ ] Quota is consumed exactly once per turn including on retry.
- [ ] `test_chat.py` updated and passing.

---

## B1.2 — No way to deliver results or show history

**Type** infra · **Effort** week · **Depends on** B1.1

**Files**
- worker service → turn document writer
- `day_planner_backend_app/app/db/store.py`
- Firestore security rules (new — currently there are none for client access)

**Problem**

An async turn needs a delivery channel. Separately, sessions are deleted after
archival in `agent_client.archive_session`, so users cannot see their own chat
history at all.

**Scope**

1. Worker appends reply deltas to `turns/{turn_id}` as the stream produces
   them, and sets a terminal `status`.
2. Client subscribes with a Firestore realtime listener.
3. **Write Firestore security rules.** The client now reads Firestore directly
   for the first time — a user must be able to read only their own turns.
   This is a new trust boundary and the most security-sensitive part of this
   phase.
4. Persisted turns give chat history as a side effect. Expose a list endpoint.

**Why Firestore rather than SSE**

Firestore is already in the stack, the client SDKs have native realtime across
web and mobile, and reconnection and resumption are handled for you. With SSE
you own cursor state and resumption yourself, and it does not solve history.

**Out of scope**

- Do not build the client UI.
- Do not add turn deletion or retention policy yet — decide it with real data.

**Acceptance**

- [ ] Reply text appears incrementally in the turn document.
- [ ] Security rules deny cross-user turn reads. Test this explicitly.
- [ ] History endpoint returns a user's prior turns.

---

## B1.3 — The worker has no path to the internal service

**Type** infra · **Effort** days · **Settle in week 1**

**Files**
- `terraform/cloud_run.tf`, `terraform/network.tf`

**Problem**

B3.2's risk checker runs in the worker and needs zones, sleep schedule and
habits **directly** — not only through the agent. The internal service is
`INGRESS_TRAFFIC_INTERNAL_ONLY`, reachable today only over the PSC interface
Agent Engine uses.

**Scope**

1. Decide: give the worker the same VPC path, or move internal to IAM-gated
   ingress with OIDC verification (which `require_internal_caller` already
   implements).
2. **Verify, do not assume**, whether Cloud Tasks can reach an
   internal-only Cloud Run service in your configuration. The simpler and more
   likely-correct path is ingress open with the invoker role granted solely to
   the Cloud Tasks service account.
3. Document the decision in `docs/oauth-design.md` alongside the existing
   trust-boundary reasoning.

**Out of scope**

- Do not weaken the internal service's authentication. Ingress is
  defence-in-depth; IAM plus OIDC is the actual gate and must stay.

**Acceptance**

- [ ] Worker reaches internal successfully.
- [ ] An unauthenticated caller is still rejected.
- [ ] Decision recorded in docs.

---

## B1.4 — Capacity settings never load-tested

**Type** infra · **Effort** days

**Files**
- `terraform/cloud_run.tf`, `terraform/variables.tf`

**Problem**

`max_instances` defaults to 2 (a documented dev cost bound) and
`min_instance_count = 0` means cold starts include `agent_engines.get()`'s
describe call. At Cloud Run's default concurrency that caps in-flight chats
around 160. The worker cannot inherit this template.

**Scope**

1. Separate scaling settings for the worker: long timeout, its own instance
   ceiling, concurrency tuned for a long-running I/O-bound workload (much lower
   than the default 80).
2. Consider `min_instance_count = 1` on the app service to remove cold starts
   from the enqueue path.
3. Synthetic load test at target concurrency. Watch **Google Calendar API quota
   consumption** — it is per-project and is the sharp edge — plus Firestore
   contention and Agent Engine limits.

**Out of scope**

- Do not raise `max_instances` without the load test. Discovering the ceiling
  in production is the thing this task exists to prevent.

**Acceptance**

- [ ] Worker has independent scaling config.
- [ ] Load test results documented, including the Calendar API quota headroom.

---

# Phase B2 — Know what changed

Week 3–4. State and fields proactive needs. Neither exists today.

## B2.1 — No user timezone

**Type** infra · **Effort** days · **Blocks** B3, B4

**Files**
- `day_planner_backend_internal/app/db/models.py`, `db/store.py`
- `day_planner_backend_internal/app/api/routes/internal.py`

**Problem**

Timezone is derived per-calendar from Google at call time in
`calendar_tool.py` (`entry.get("timeZone")`). There is no field on the user
record to schedule anything time-of-day against.

**Scope**

1. Add `timezone` (IANA identifier) to the user record.
2. Populate it on OAuth connect from the primary calendar's `timeZone`, which
   is already fetched.
3. Allow the agent to correct it conversationally.
4. Handle absence gracefully — do not assume UTC silently.

**Out of scope**

- Do not change how `calendar_tool` resolves event times. The per-calendar
  timezone resolution is correct and deliberate; this field is for *scheduling
  jobs*, not for writing events.

**Acceptance**

- [ ] Existing users are backfilled.
- [ ] New connections populate it automatically.

---

## B2.2 — No change detection

**Type** infra · **Effort** week · **Blocks** B3.1

**Files**
- `day_planner_agent/calendar_tool.py` or a new sync module in the worker
- `day_planner_backend_internal/app/db/store.py` → new `calendar_sync` collection

**Problem**

Calendar reads are range queries only. A push notification carries an **empty
body** — it says something changed on this calendar, never what.

**Scope**

1. New state at `users/{uid}/calendar_sync/{calendar_id}` holding `sync_token`,
   `channel_id`, `resource_id`, `channel_expiration`.
2. Initial full sync stores `nextSyncToken`.
3. Incremental sync calls `events.list(calendarId, syncToken=...)` returning
   only changed events, then updates the token.
4. **Handle `410 GONE`** — the token expired; fall back to a full resync and
   store a fresh token.
5. Deleted events come back with `status: "cancelled"`. Handle them; they are
   the most important signal for "a habit session disappeared."
6. **Close out A1.4's deferred diagnostic here.** A1.5 made completion explicit
   user-set state, so completion rate — the primary quality metric — already
   works without this task. What still depends on change detection is the
   *diagnostic* half: survival rate for users who never trigger a review, and
   rapid-delete detection (a tagged event deleted shortly after creation). Emit
   into the same BigQuery dataset with `source: push`. Scope is narrower than
   earlier drafts implied — this improves explanations of *why* a session was not
   completed; it is not the completion signal itself.

**Out of scope**

- Do not register watch channels yet. That is B3.1.
- Do not change the existing range-query path — the agent still needs it for
  ordinary planning.

**Acceptance**

- [ ] Incremental sync returns only changes.
- [ ] A 410 triggers full resync without data loss.
- [ ] Cancelled events are surfaced, not dropped.

---

# Phase B3 — Trigger, gated

Week 4–6. **B3.1 ships log-only for a week before B3.2 may spend a token.**

## B3.1 — Notifications are bursty and self-inflicted

**Type** infra · **Effort** week

**Files**
- `day_planner_backend_app/app/api/routes/webhooks.py` (new)
- Cloud Tasks queue for replan jobs

**Problem**

Two failure modes arrive together:

1. **Feedback loop.** The agent's own writes trigger notifications, which would
   trigger the agent, which writes. Infinite and billed.
2. **Bursts.** A user rearranging their week produces dozens of notifications
   in a minute.

**Scope**

1. Webhook on the **existing public app service** — it already handles the hard
   case of Google's servers arriving with no GCP identity, on the OAuth
   callback.
2. Validate `X-Goog-Channel-Token` **constant-time** against the secret set at
   watch time. Check `X-Goog-Resource-ID` matches stored state.
3. Ignore `X-Goog-Resource-State: sync` — that is the creation handshake, not a
   change.
4. Return 200 **fast**, under a second. Google retries a slow endpoint, which
   compounds a burst.
5. **Debounce via Cloud Tasks name dedup**: task name
   `replan-{user_id}-{5min bucket}`, `scheduleTime` now + 5 minutes. Creating a
   task with an existing name returns `ALREADY_EXISTS`, collapsing a burst into
   one job with no debounce logic of your own. Note names stay deduped for
   about an hour after completion — harmless here.
6. **Break the loop**: filter out changes to events carrying the
   `day_planner_habit_id` extended property, plus a short-lived write log of
   event IDs the worker just created. The tag alone will not tell you who made
   the most recent edit; you need both.
7. **Ship log-only.** Record notification volume, self-trigger ratio, and
   dedup effectiveness for a week. No agent invocation.

**Out of scope**

- Do not invoke the agent from this task under any circumstances.
- Do not register watch channels for all users at once — start with a small
  cohort.

**Acceptance**

- [ ] Invalid channel token is rejected.
- [ ] `sync` state is ignored.
- [ ] A burst of 40 notifications produces one queued job.
- [ ] Self-triggered changes are correctly identified and dropped.
- [ ] One week of volume data collected before B3.2 starts.

---

## B3.2 — Deciding whether anything actually matters

**Type** gate · **Effort** week · **Depends on** A4.1, B1.3, B2.2

> Revisit this spec once A4.1's engine interface is final.

**Problem**

Firing a model call per notification is ruinous, and most notifications are
irrelevant — a colleague moved a meeting that collides with nothing.

**Scope**

1. In the worker, run Roadmap 1's constraint engine (A4.1) as a pure-Python
   gate over the changed events from B2.2.
2. Two questions, both answerable without a model: does any placed habit
   session now conflict with a zone, sleep window, or new event? Is any habit's
   period target now at risk?
3. Escalate to the agent **only** when the answer is yes *and* the resolution is
   ambiguous. A session displaced with an obvious legal alternative can be
   proposed without a model call at all.
4. Instrument the gate ratio — this is the number that determines whether
   proactive is affordable. Target under 0.05 model calls per notification.

**Out of scope**

- Do not let the gate write to calendars. It decides; B4.1 proposes.
- Do not duplicate engine logic here. Import it. If something is missing from
  A4.1, add it there with unit tests, not here.

**Acceptance**

- [ ] Gate ratio measured and under target.
- [ ] Zero model calls for notifications with no conflict.
- [ ] Gate decisions logged with reasons.

---

## B3.3 — Proactive runs bypass the quota

**Type** gate · **Effort** days

**Files**
- `day_planner_backend_internal/app/db/store.py` → new budget method
- worker

**Problem**

`check_and_consume_quota` lives in the `/me/chat` route
(`day_planner_backend_app/app/api/routes/chat.py`), so a background path spends
nothing against it. An unbounded background path is how a bug becomes a
five-figure bill overnight.

**Scope**

1. A separate proactive budget per user per day, atomically consumed — mirror
   the transaction pattern in `check_and_consume_quota`, which is already
   correct.
2. It must **not** consume the user's chat allowance.
3. Hard global ceiling as well as per-user, so a systemic bug cannot escalate
   fleet-wide.
4. Exhausted budget is not an error — skip silently and log.

**Out of scope**

- Do not add billing or tiers. `docs/pricing-ideas.md` owns that.

**Acceptance**

- [ ] Proactive runs consume only the proactive budget.
- [ ] Global ceiling stops runaway fleet-wide spend.

---

# Phase B4 — Act on it

Week 6–8.

## B4.1 — Nobody is there to answer

**Type** gate · **Effort** 2 weeks · **Depends on** B3.2, A2.3

> Revisit this spec once A4.1's engine interface is final.

**Problem**

`instruction.md` says "ask the user rather than guess" in six places, and there
is a commit named "ask-before-replanning." A background run has nobody to ask,
so it either stalls and does nothing or silently mutates a real calendar —
contradicting the product's core trust posture.

**Scope**

1. New `proposals/{proposal_id}` Firestore collection: what changed, what is
   suggested, why (using the engine's `reasons`), and which events it touches.
2. Push notification to the user.
3. One-tap approve applies the change **deterministically** — plain code using
   A2.3's idempotency keys, not a second agent invocation.
4. Proposals expire. A stale proposal referencing a since-deleted event must
   fail safe, not write garbage.
5. Reuse the security rules pattern from B1.2 so a user reads only their own.
6. Track approval rate — it is your best proxy for whether proactive is
   actually useful, and the signal that tells you to stop if it is not.

**Out of scope**

- Do not let a proposal apply automatically, under any confidence threshold.
  The whole design rests on a human tapping approve.
- Do not build partial approval in v1 — accept or dismiss whole.

**Acceptance**

- [ ] A conflict produces a proposal, never a direct write.
- [ ] Approval applies idempotently.
- [ ] A stale proposal fails safe.
- [ ] Approval rate is tracked.

---

## B4.2 — Watch channels expire

**Type** infra · **Effort** days

**Files**
- Cloud Scheduler + worker route

**Problem**

Calendar watch channels last roughly seven days at most, and Google may return
a **shorter** expiration than requested. An expired channel fails silently —
notifications simply stop, and nothing surfaces it.

**Scope**

1. Cloud Scheduler sweeps daily for channels expiring within 48h and re-watches.
2. **Always store the returned `expiration`, never the requested one.**
3. Batch and rate-limit. At 100k users × ~3 calendars renewing weekly that is
   roughly 43k renewals/day.
4. Alert if renewal failures exceed a threshold, and if any active user has had
   zero notifications for longer than the channel lifetime — that is how you
   detect silent breakage.

**Out of scope**

- Do not invoke the agent from this job. It is deterministic maintenance.

**Acceptance**

- [ ] Channels renew before expiry.
- [ ] Renewal failures alert.
- [ ] Silent-stop detection works.

---

# Phase B5 — Standing debt

## B5.1 — Two backends share copy-pasted infrastructure code

**Type** infra · **Effort** days · **Re-scope after Roadmap 1's A6.1**

> **Rewritten.** Roadmap 1 phase A6 changes this task substantially. Read that
> phase before starting, and re-measure the duplication rather than trusting the
> figures below.

**Files**
- new `day_planner_common/` package
- every service's `Dockerfile` and `requirements.txt`

**Problem**

`providers/google.py`, `providers/base.py`, `services/crypto.py` and
`core/pkce.py` are byte-identical across both services. Security-relevant code
existing twice means a fix to one can miss the other.

The larger divergences originally listed here — `db/store.py` by 400 lines,
`db/models.py` by 222 — were mostly **domain code that only the internal service
had**. Roadmap 1 task A6.1 moves habits, habit sessions, zones and the sleep
schedule to the app service outright, which removes that asymmetry rather than
sharing it. So most of what this task was going to reconcile stops existing.

What remains after A6.1 is genuinely shared **credential-adjacent
infrastructure**, and it is a much smaller and better-defined surface:

- `providers/google.py`, `providers/base.py`, `providers/__init__.py`
- `services/crypto.py`, `core/pkce.py`, `core/security.py`
- the `ConnectedAccount` and `Calendar` models
- whatever `Store` code both services still need for connected accounts

Note also that A6.1 deletes internal's now-dead user, session and login-throttle
code, which removes another slice of the apparent duplication without any
extraction work at all.

**Scope**

1. **Re-measure first.** Diff the two services after A6.1 lands and work from
   the real numbers. The list above is a prediction, not a finding.
2. Extract what remains into `day_planner_common`, installed by every service
   Dockerfile — including B1.1's worker.
3. Start with the byte-identical files; those are risk-free.
4. Reconcile any remaining drift deliberately, file by file, checking whether
   each difference is intentional. Some are real: `connections.py` genuinely
   differs because only the app service performs the OAuth code exchange.

**Out of scope**

- **Do not merge the services.** The split is well-argued in
  `docs/oauth-design.md` and A6.1 corrects where the line falls, not whether
  there is one.
- Do not extract domain code. After A6.1 it has a single owner and belongs to
  the app service; putting it in a shared package would re-create the ambiguity
  A6.1 exists to remove.
- Do not start before A6.1. Extracting code that is about to move is wasted work
  and makes A6.1's diff much harder to review.

**Acceptance**

- [ ] Duplication re-measured post-A6.1 and recorded.
- [ ] Byte-identical credential-adjacent files exist once.
- [ ] Every intentional divergence is documented as such.
- [ ] No domain code in the shared package.
- [ ] All service test suites pass unchanged.
