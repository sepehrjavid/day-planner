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
`day_planner_agent/evals/scenarios/` (27 scenarios: 21 tier `constraint`,
6 tier `decision`). Run with:

```
day_planner_agent/.venv/bin/python day_planner_agent/evals/runner.py \
    day_planner_agent/evals/scenarios --repeat 3
```

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
