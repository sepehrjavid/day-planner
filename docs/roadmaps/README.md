# Roadmaps — sequenced remediation, ready to execute

Two ordered roadmaps covering every known issue in the system, each task
scoped tightly enough to hand to a coding agent as a single unit of work.

Unlike [todo.md](../todo.md), which deliberately holds ideas that are
*not* being built yet, and [known-issues.md](../known-issues.md), which
tracks deferred problems without committing to fixing them, these are
sequenced work with acceptance criteria. They came out of a full read of
the codebase, the Terraform, and `docs/` in August 2026.

## The two roadmaps, in order

1. **[1-agent.md](1-agent.md)** — everything inside `day_planner_agent`.
   Observability, behavioural and model evaluation, then moving constraint
   arithmetic out of `instruction.md` and into code. Does not change how
   the agent is invoked. 6 phases, 36 tasks, ~16 weeks.
   *Status: A0 and A1.1–A1.3 complete. A1.5 next, then A1.4.*

2. **[2-system.md](2-system.md)** — how the agent gets invoked. Cloud
   Tasks and a background worker, then a second trigger: the user's
   calendar changing. 5 phases, 12 tasks, ~8 weeks.

**Do them in that order.** Roadmap 2 lists four hard prerequisites from
roadmap 1 and will not be safe without them — most sharply, idempotent
calendar writes, since a queue implies retries and a retry without keys
double-books a real user's calendar.

## Why the split falls where it does

Two components are built once and used twice, which is most of the
argument for this particular ordering:

- The **constraint engine** (task A4.1) exists to shrink the prompt, and
  turns out to be exactly the risk checker (B3.2) that filters calendar
  notifications before any model call.
- The **worker** (B1.1) exists to get chat out of the HTTP request, and
  is the same execution path a calendar trigger feeds.

Two tasks were moved into roadmap 1 despite reading like system work.
Idempotency (A2.3) ships together with retries because retrying a
calendar write without a key creates duplicate events. Plan-then-apply
(A4.6) is about how the agent *acts* rather than how it is called, and is
the natural payoff of having the engine.

## Using these

- **One task at a time.** Each has its own acceptance criteria.
- **"Out of scope" sections are not advisory.** They exist because the
  adjacent work is either scheduled elsewhere or deliberately deferred;
  widening scope breaks the dependency chain. Several of them prevent a
  specific, plausible mistake — A4.1 forbids touching `instruction.md`,
  A4.2 forbids removing prompt text during shadow mode, B5.1 forbids
  merging the two backend services.
- **Two specs say "verify, do not assume"** — A0.5 on whether the model
  string is a pinned or moving version, and B1.3 on whether Cloud Tasks
  can reach an internal-ingress Cloud Run service. Both are things an
  agent will assert confidently and wrongly.
- **Two specs in roadmap 2 are provisional.** B3.2 and B4.1 depend on the
  interface of the engine from A4.1, which does not exist yet. Tighten
  them once it does.
- **Phases A1, A3 and A6 are not in numeric order.** Numbering is
  append-only so cross-references stay valid; each phase header gives the
  real execution order. A3.7 comes weeks after the rest, once A1.4 has
  production data to correlate against, and A6 runs after A2 despite its
  number.

## Where the service boundary falls

Phase A6 moves habits, habit sessions, zones and the sleep schedule out
of `day_planner_backend_internal` and into `day_planner_backend_app`.

That service was named for *who calls it*, not *what it protects*, and
two unrelated concerns ended up inside it: credentials, which genuinely
need the isolation [oauth-design.md](../oauth-design.md) argues for, and
ordinary planning data, which landed there only because the agent needed
it first. The visible consequence was that a user could not create a
habit or fix a wrong work-hours zone without asking the model to do it.

After A6, `day_planner_backend_internal` keeps four routes — connect
links, calendar listing, access-token minting, disconnect — plus the KMS
key and refresh tokens, and nothing else. Planning data lives on the app
service, reachable by the UI through session-authenticated `/me` routes
and by the agent through OIDC-authenticated `/agent` routes. The agent
still calls the internal service for credentials.

No Firestore document moves; only the code that owns them does.

## On evaluating decisions, not just compliance

Worth understanding before starting phase A3. Constraint invariants
answer "did the agent break a rule" — binary and cheap, but they will
pass an agent that always picks the same defensible-but-poor slot.
Evaluating *decisions* needs three additional things, and all three are
mechanical rather than subjective:

- **Counterfactuals** (A3.5) — change one input, assert the decision
  moves in the expected direction. This is what distinguishes reasoning
  from pattern-matching to a default.
- **Explanation and process checks** (A3.6) — is the agent's stated
  reason actually true against fixture state, and did it perform the
  lookups its decision should rest on.
- **Correlation with reality** (A3.7) — are the placements the suite
  rewards actually *completed* in production. Without this you are
  optimising a proxy and would never know.

The ground truth for that last one comes from A1.5, which was added to
the roadmap after A1.3 shipped. It replaces the original design's
weakest assumption: that a calendar event still existing at its planned
time meant the user did the thing. It didn't — it meant nobody deleted
it. Habit sessions now carry explicit `pending` / `completed` / `skipped`
state that the user sets from the UI or conversationally through the
agent, so **completion rate** is the primary quality metric and survival
rate is demoted to a diagnostic for explaining why something wasn't
done. `pending` is never counted as failure anywhere.

Rendered overviews of both, with the same content in a more readable
form, were published as private Claude artifacts:
[roadmap 1](https://claude.ai/code/artifact/46227324-966c-4dfd-82ab-c7682ace9a88),
[roadmap 2](https://claude.ai/code/artifact/736fee20-4803-4193-935e-edf677955409),
plus a longer piece on
[the agent feedback loop](https://claude.ai/code/artifact/581ec57a-4f90-42ed-a29a-1a1f5372276e)
covering the reasoning behind roadmap 1's phases A1, A3 and A4. The
markdown here is canonical; the artifacts are the summary view.
