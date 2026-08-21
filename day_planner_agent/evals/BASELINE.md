# A3.1 baseline — recorded 2026-08-24 (9 scenarios), updated 2026-08-19 (27 scenarios)

**This baseline is post-A2.5-reorder.** A2.5 moved `{today}`,
`{profile_section}` and `{zones_section}` from the top of `instruction.md`
to the very end so the instruction+tools prefix stays byte-identical
across requests for Vertex AI's implicit context cache. It shipped with no
behavioural verification, because this suite didn't exist yet — its own
acceptance criterion ("run the A3.1 suite before and after if it exists by
then") could not be met. **This number is not a pre-reorder reference
point and must never be read as one** — see the pre/post comparison
section below for the closest thing to one this suite can produce.

Model: `gemini-2.5-flash` (pinned per A0.5, see `agent.py`). Scenarios:
`day_planner_agent/evals/scenarios/` (30 scenarios: 24 tier `constraint`,
6 tier `decision`, including A3.2's 3 failure-mode scenarios). Run with:

```
day_planner_agent/.venv/bin/python day_planner_agent/evals/runner.py \
    day_planner_agent/evals/scenarios --repeat 3
```

## A3.2 — clean fixtures hide failure modes

Added `day_planner_agent/evals/scenarios/failure_modes/`: zone fetch
failing, calendar `needs_auth`, and a read-only (`not_writable`)
calendar — the three A0.2/A3.2 explicitly ask for.

**Review caught a real gap in the first version of this PR**: two of the
three scenarios asserted less than their names claimed.
`calendar_needs_auth` ("hands over the connect_url and stops") and
`calendar_not_writable` ("reports a read-only calendar") both only
checked `no_events_actually_placed` — the stopping half, never the
reporting half. An agent that silently did nothing, or crashed, would
have passed either one. Fixed by capturing the model's reply text in the
runner and adding two entity-matching invariants that check for a
specific, deterministic string a tool actually returned (not phrasing,
per A3.1's own rule): `connect_url_handed_to_user` (the exact
`connect_url` string from `NeedsAuth`) and `reply_reports_readonly_calendar`
(the calendar's own `summary`, "Personal"). Also added `model_invoked` to
all three scenarios, not just zone-fetch-fails, for the same reason it
was added there.

Current results: `calendar_needs_auth` 100% (3/3). `zone_fetch_fails`
100% (3/3, with the informational re-check crash still present in every
trial — see below). `calendar_not_writable` 67% (2/3) — the one miss is
a genuine, different finding: in that trial the model responded "I don't
see a 'gym' habit in your tracked habits, would you like to create one?"
instead of ever reaching the not-writable calendar path, despite Gym
being in the scenario's `given.habits`. `no_events_actually_placed` still
passed for that trial (nothing was placed, coincidentally, for an
unrelated reason) — exactly the ambiguity `reply_reports_readonly_calendar`
exists to catch, and did.

**A real, unplanned finding surfaced while building the zone-fetch-fails
scenario, unrelated to A0.2 itself**: when the model re-checks zones mid-
conversation (as instruction.md explicitly invites after a preload
failure — "call list_zones yourself to re-check") and that re-check also
fails, the exception isn't caught anywhere in zone_tools.py (or
calendar_tool.py/habit_tools.py/memory_tools.py, by the same pattern) —
it propagates uncaught through ADK's tool-calling machinery and crashes
the whole turn, instead of returning a `{"status": "error"}` the model
could react to. This happened in 2-3 of 3 real trials once the initial
preload was made to fail persistently. Flagged as its own follow-up
task (not fixed here — out of scope for a scenarios-only task) rather
than silently worked around.

This also meant `no_events_actually_placed` alone couldn't distinguish
"the agent correctly declined to place anything" from "the turn crashed
before the agent did anything at all" — both produce zero placed events.
The zone-fetch-fails scenario adds a second check, `model_invoked`
(`expect.model_invoked: true`), which asserts the model actually
produced token usage before any exception — true whether or not a later
re-check crash occurs, false only when the turn dies at the preload
callback itself. **Verified as a genuine A0.2 regression test**: reverting
`_preload_zones` to the pre-A0.2 ordering (flag set before fetch, no
try/except) and re-running this one scenario dropped it from 100% to
0% — `model_invoked` catches it (`input_tokens=0`), confirming the
scenario fails exactly when A0.2's fix is absent, as its acceptance
criterion requires.

## A3.5 — perturbation scenarios (does a single input change move the decision the way it should?)

Added `day_planner_agent/evals/scenarios/perturbations/` (7 scenarios,
21 trials): each one changes exactly one input from what would otherwise
produce an "obvious" placement, and checks the model's decision actually
moves in response — `conflicting_event_at_obvious_slot`,
`new_zone_over_obvious_slot`, `extended_cooldown_over_obvious_slot`,
`no_physical_activity_after_8pm`, `weekend_filled_calendar_wins`,
`raised_target_more_sessions`, `allowed_zones_lets_placement_into_work`.
Built as **static, single-run scenarios** (engineered so the base case is
near-certain by construction) rather than a dynamic base-then-perturb
harness, per explicit direction — each asserts the perturbed run's
*directional property* via existing/new invariants, never an exact
replacement slot.

**Current results: 81.0% (17/21)**. Every miss across all 21 trials was
non-placement (`add_calendar_event` called 0 times) — **zero genuine
constraint violations** (no zone/sleep/existing-event overlap, no bad
`habit_id` tagging) anywhere in this run. This matches the "declines
rather than violates" pattern already noted in the 27-scenario run above
— the recurring failure mode across this whole suite continues to be the
model doing nothing, not doing something unsafe.

### Meta-test: does each perturbation actually fail without its rule?

The acceptance criterion ("each perturbation is verified to fail when its
corresponding rule is removed from `instruction.md`") was run for all 7,
using the same `--instruction <path>` swap-in technique from A2.5's old/
new comparison: surgically remove the exact governing substring from a
scratch copy of `instruction.md` (never the real file), re-run that one
scenario, and check for degradation. Three different outcomes turned up:

**Clean degradation (4/7)** — removing the rule measurably hurt the
scenario:

- `no_physical_activity_after_8pm`: 100% → 33%, both misses non-placement.
- `extended_cooldown_over_obvious_slot`: 100% → 67% (see redesign note
  below), the miss non-placement.
- `new_zone_over_obvious_slot`: needed escalation — a single targeted
  clause removal alone showed no degradation, because instruction.md
  reinforces zone-avoidance with two independent general "hard
  constraint" sentences elsewhere. Removing all three together dropped it
  100% → 33%, the misses again non-placement rather than a genuine
  violation — consistent with the rest of this suite's pattern, and
  notable input for A4.3's eventual instruction-trimming work: this rule
  is currently stated three times over, not once.
- `weekend_filled_calendar_wins`: noisier to read — the scenario's own
  real baseline sits around 80% at n=5 (non-placement is common here even
  with the rule present), so a same-size n=3 comparison isn't clean on
  pass rate alone. The tell was qualitative instead: with the
  weekend-preference sentence removed, 1 of 3 trials produced an actual
  `no_session_overlaps_existing_events` violation (a session placed on
  top of the "Wedding" event) — a failure mode that never once appeared
  across 8 total baseline trials (n=3 + n=5) with the rule present.

**Scenario needed redesigning, not just more removal (1/7)** —
`extended_cooldown_over_obvious_slot`'s original design (Work zone
starting at 09:00, "this week" phrasing) left a free 07:15-09:00 morning
gap and a whole-week fallback, so the model could always dodge the
perturbation entirely regardless of how much cooldown-related instruction
text was removed — even the escalated removal left it at 100%. Fixed by
tightening the fixture itself: Work now starts at 07:15 (exactly when
`wake_up_buffer_minutes` ends, closing the morning gap) and the prompt
asks for "tonight" (closing the whole-week escape). Re-verified at 100%
against the real instruction before re-running the meta-test, which then
showed the expected 100% → 67% drop.

**No degradation found even after maximal removal (2/7)** — genuine
findings about what these two scenarios can and can't test, not
methodology failures:

- `raised_target_more_sessions`: removing the "add up to the period's
  target" clause — and, escalating, the surrounding "your job is to
  actually place the sessions" and "primary job is helping the user
  follow through" framing too — left this at 100% regardless. Read
  together with the scenario's design: the habit's own goal text already
  states the numeric target directly (`"300 min/week, sessions 30-60
  minutes"`), so the model appears to satisfy it as a direct instruction
  from the data itself, largely independent of the system instruction's
  own accumulation language. This rule may simply be restating something
  the model already does from the habit goal text alone.
- `allowed_zones_lets_placement_into_work`: removing the override clause
  showed no degradation, for a structural reason rather than a behavioral
  one — `no_session_overlaps_any_zone` reads `allowed_zones` off the
  fixture's static habit record (set up in `given.habits`, not decided by
  the model at runtime), so it cannot register a violation here
  regardless of whether the model's own reasoning used the documented
  override mechanism. The user's turn also states the exception
  explicitly ("they happen during my work day, that's expected"), which
  the model may simply be honoring on its own per the separate
  "conversational one-off override" rule. This scenario can't currently
  distinguish "correctly applied the documented `allowed_zones`
  mechanism" from "just complied with an explicit in-conversation
  statement" — worth revisiting once A3.6's entity-matching work can
  check what mechanism the model actually invoked rather than only the
  outcome.

## Headline numbers (current — 27 scenarios, 81 trials)

| Tier | Pass rate | Gate | Met? |
|---|---|---|---|
| constraint (tier 1) | 85.7% (69/81) | 100%, blocks release | **No** |
| decision (tier 2) | 66.7% (12/18 — 6 scenarios × 3 trials) | ≥90%, warns | **No** (warn, not a blocker per the tier's own rule) |

Wall time: 1,149.7s (~19.2 min) for 81 trials, ~14.2s/trial. Tokens:
4,444,158 input / 31,169 output / 96,849 thinking — averaging ~55k input
tokens/trial, consistent with the earlier 9-scenario run's ~57k/trial
(the instruction+tools prefix dominates either way; see
`turn_log_queries.sql` query 11 for the deployed cache-hit-rate number
once turns start carrying it).

## The cross-cutting finding: the agent silently does nothing more often than expected

**9 of 81 trials (11%) placed zero events at all**, spread across at
least 7 different scenarios — not concentrated in one prompt's wording.
No trial raised an exception; every one of these is the model completing
normally without calling `add_calendar_event` (or `create_habit`) even
once. This wasn't visible in the first (9-scenario) run at nearly this
rate and is the single most important finding in this update — more
general than any one invariant violation, because it cuts across nearly
every scenario category (habit placement, zone-override scenarios, even
`no_weekend_preference_when_weekend_busy`, which alone had 2/3 trials do
nothing).

This harness doesn't currently capture the model's reply text (only tool
calls and invariants), so it can't distinguish "reasonably asked a
clarifying question instead of guessing" (correct, per instruction.md's
own "if it's ambiguous, ask before assuming") from "silently gave up."
That distinction matters a lot and is worth a follow-up: either extend
the runner to capture and inspect reply text for these trials, or treat
a bare "no tool calls, no explanation-worthy ambiguity in the prompt" as
its own tier-2 (or even tier-1) invariant once A3.6's process-invariant
work exists to lean on.

## Two reproducible, not-noise findings

**1. `late_night_boundary_pressure` fails 0/9 across all three runs to
date** (0/3 in the first 9-scenario run, 0/3 in the pre-A2.5 comparison
run, 0/3 in this 27-scenario run) — always the same failure mode: either
no placement, or a placed session missing `habit_id`. The prompt ("I want
to work out for 45 minutes tonight, as late as possible...") never names
the tracked Gym habit or gives an explicit time. When it *did* place
something, the zone/sleep-window invariants held every time — the
placement itself was never unsafe, only the habit_id tagging (or the
non-placement) was. instruction.md has no explicit rule connecting an
unnamed one-off activity request to a same-kind tracked habit — a real,
now well-evidenced gap, left unfixed here per this task's own
out-of-scope note (no instruction.md changes in A3.1).

**2. Genuine hard-constraint (tier-1) zone violations recur under
scheduling pressure, in different specific ways each time.**
`packed_week_placement` ("still avoids work hours and sleep when the
week already has conflicts") has now failed with a real zone violation
in *two separate runs* — a session at 09:00 Monday in the 9-scenario
run, a session at 10:00 the following Monday in this run — plus a
non-placement in the third trial each time. Every other constraint
scenario *not* under comparable scheduling pressure holds at or near
100%. This is the strongest evidence in this dataset that the tier-1
gate is not actually met today, independent of anything A2.5 touched,
and specifically under load.

## Tier-2 (decision) findings — informational, never gate

Two genuine misses, both plausible and worth watching rather than acting
on from a single instance each:

- **`heavier_load_three_days`**: on the one day with 4 hours of existing
  meetings, the model correctly gave a 60-minute session; on two lighter
  (fully free) days it gave only 45 minutes each — backwards from
  instruction.md's "light day → longer session" rule, in one trial.
- **`two_habits_weekend_split`**: a fully free weekend got 0% of that
  trial's placed minutes, versus the ≥29% (2/7 days) baseline this
  invariant checks for. `weekend_free_gets_loaded` (the more directly
  weekend-focused scenario) held 100%, so this reads as inconsistent
  application of the preference under more complex prompts (two habits
  at once) rather than the preference being absent altogether.

## Pre/post A2.5 comparison (the "optional, worth the hour" check)

Per A3.1's own note, the headline numbers above are a **post-reorder**
baseline only and can't by themselves say whether A2.5 changed
compliance. This section is that comparison, done with the real harness
instead of by hand — run against the original 9 zone/sleep scenarios
(the comparison predates the 18 scenarios added later in this task).

Same 9 scenarios, same repeat=3, run against the pre-A2.5 `instruction.md`
(`git show 43d00f5:day_planner_agent/instruction.md` — the commit
immediately before A2.5 merged):

```
day_planner_agent/.venv/bin/python day_planner_agent/evals/runner.py \
    day_planner_agent/evals/scenarios/zone_sleep --repeat 3 \
    --instruction /path/to/pre-a2.5-instruction.md
```

| Ordering | Tier-1 pass rate | Input tokens (27 trials) |
|---|---|---|
| Pre-A2.5 (volatile content at the top) | 77.8% (21/27) | 1,420,899 |
| Post-A2.5, run 1 | 85.2% (23/27) | not captured |
| Post-A2.5, run 2 | 81.5% (22/27) | 1,537,887 |

All three runs land within an ~8-point band of each other. More tellingly,
the *pattern* of failures doesn't track the ordering:

- `late_night_boundary_pressure` fails identically under both orderings —
  0/3 in every single run, always the same failure mode (missing
  `habit_id`, or no placement at all). This is the strongest evidence in
  this data that finding #1 above is a genuine instruction-content gap,
  independent of A2.5 entirely — it existed before the reorder and
  survived it unchanged.
- The other misses (occasional 0-call trials, the packed-week zone
  violations) land on *different* scenarios in the pre- vs. post-reorder
  runs, not the same ones getting consistently worse. The zone
  violations found across all runs so far have all been in **post**-A2.5
  runs, which if anything cuts against "the reorder made compliance
  worse" — though see the wider 27-scenario data above before reading
  too much into that on its own.

**Conclusion: no material behavioural difference found between orderings.**
This doesn't prove equivalence — 27 trials per side is not a lot, and a
small, real effect could still be hiding under this level of sampling
noise — but it finds nothing suggesting A2.5 traded away compliance for
cache efficiency, which was the open risk that PR shipped with.

## What changed between the two recorded runs

The first recording (9 scenarios, zone/sleep focus only) measured 81.5–
85.2% tier-1. This update (27 scenarios, broader coverage plus the first
tier-2 data) measured 85.7% tier-1 — consistent with the earlier number,
not a regression — and 66.7% tier-2, which has no prior baseline to
compare against since the tier-2 invariants didn't exist yet. The 11%
zero-tool-call rate is new information this update surfaced by testing a
wider variety of prompts (allowed_zones overrides, cool-down overrides,
narrower session ranges, multi-habit asks) than the original 9
zone/sleep-focused scenarios exercised.
