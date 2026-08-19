# A3.1 baseline — recorded 2026-08-24

**This baseline is post-A2.5-reorder.** A2.5 moved `{today}`,
`{profile_section}` and `{zones_section}` from the top of `instruction.md`
to the very end so the instruction+tools prefix stays byte-identical
across requests for Vertex AI's implicit context cache. It shipped with no
behavioural verification, because this suite didn't exist yet — its own
acceptance criterion ("run the A3.1 suite before and after if it exists by
then") could not be met. **This number is not a pre-reorder reference
point and must never be read as one.**

Model: `gemini-2.5-flash` (pinned per A0.5, see `agent.py`). Scenarios:
`day_planner_agent/evals/scenarios/zone_sleep/` (9 scenarios, all tier
`constraint`). Run with:

```
day_planner_agent/.venv/bin/python day_planner_agent/evals/runner.py \
    day_planner_agent/evals/scenarios --repeat 3
```

## Headline numbers

Two independent full-suite runs (9 scenarios × 3 trials = 27 trials each),
back to back, same day, same scenarios, same instruction.md:

| Run | Tier-1 (constraint) pass rate | Wall time | Input tokens | Output tokens | Thinking tokens |
|---|---|---|---|---|---|
| 1 | 85.2% (23/27) | not captured (added after) | not captured | not captured | not captured |
| 2 | 81.5% (22/27) | 340.4s (~5.7 min for 27 trials, ~12.6s/trial) | 1,537,887 | 9,330 | 31,939 |

**Tier-1's stated gate is 100%.** The measured rate is not that, and the
gap is not noise across the board — see below. Full-suite token cost
(run 2, the one with tracking): ~57k input tokens per trial on average,
dominated by the instruction+tools prefix; this is the number A2.5's
context caching is meant to discount, not eliminate — see
`turn_log_queries.sql` query 11 once this runs against the deployed
agent and turns start carrying `cached_tokens`.

## Per-scenario results, both runs combined (6 trials per scenario)

| Scenario | Pass rate | Failure mode |
|---|---|---|
| `basic_week_placement` | 6/6 | — |
| `custom_zone_not_named_work` | 6/6 | — |
| `evening_preference_plus_zone` | 6/6 | — |
| `multiple_habits_independent` | 6/6 | — |
| `two_week_placement` | 6/6 | — |
| `zone_anchored_commute` | 6/6 | — |
| `plain_appointment_not_tagged` | 5/6 | 1 trial: 0 `add_calendar_event` calls (didn't act at all on an unambiguous appointment) |
| `packed_week_placement` | 4/6 | 1 trial: genuine zone violation (`Gym` placed 09:00 Monday, inside the 09:00–17:30 Work zone); 1 trial: 0 `add_calendar_event` calls |
| `late_night_boundary_pressure` | 0/6 | Every trial: either 0 `add_calendar_event` calls, or a placed session missing `habit_id` |

## Two findings worth reading closely

**1. `late_night_boundary_pressure` fails 100% of the time, reproducibly —
this is signal, not noise.** The prompt ("I want to work out for 45
minutes tonight, as late as possible before I need to start winding down
for bed") never names the tracked Gym habit or gives an explicit time.
Across 6 trials the agent either asked a clarifying question instead of
acting (0 calls — itself arguably correct per instruction.md's "if it's
ambiguous, ask before assuming"), or placed a session tagged as a plain
appointment, not a habit session. When it *did* place something, the
zone/sleep-window invariants held every time — the placement itself was
never unsafe, only the habit_id tagging was. instruction.md has no
explicit rule connecting an unnamed one-off activity request to a
tracked habit of the same kind; this looks like a real gap worth a
targeted instruction addition, not a scenario bug. Left as-is here
per A3.1's own out-of-scope note (no instruction.md changes in this
task) — flagged for a follow-up.

**2. `packed_week_placement` shows a real, if infrequent, hard-constraint
violation.** One trial (of 6) placed a habit session inside the Work zone
while the calendar already had two conflicting events that week. This is
exactly the class of regression this suite exists to catch, and it means
the tier-1 gate is not actually met today, independent of anything A2.5
touched — every other zone/sleep scenario, including the two under
comparable or greater scheduling pressure (`evening_preference_plus_zone`,
`two_week_placement`), passed 6/6. Worth another few dozen trials once
CI wiring lands to see whether this rate holds, before treating it as
more than a single data point.

## Pre/post A2.5 comparison (the "optional, worth the hour" check)

Per A3.1's own note, the headline numbers above are a **post-reorder**
baseline only and can't by themselves say whether A2.5 changed
compliance. This section is that comparison, done with the real harness
instead of by hand.

Same 9 scenarios, same repeat=3, run against the pre-A2.5 `instruction.md`
(`git show 43d00f5:day_planner_agent/instruction.md` — the commit
immediately before A2.5 merged):

```
day_planner_agent/.venv/bin/python day_planner_agent/evals/runner.py \
    day_planner_agent/evals/scenarios --repeat 3 \
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
- The other misses (occasional 0-call trials, the one packed-week zone
  violation) land on *different* scenarios in the pre- vs. post-reorder
  runs, not the same ones getting consistently worse. The one real zone
  violation found across all three runs was in a **post**-A2.5 run, which
  if anything cuts against "the reorder made compliance worse."

**Conclusion: no material behavioural difference found between orderings.**
This doesn't prove equivalence — 27 trials per side is not a lot, and a
small, real effect could still be hiding under this level of sampling
noise — but it finds nothing suggesting A2.5 traded away compliance for
cache efficiency, which was the open risk that PR shipped with.
