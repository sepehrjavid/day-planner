# A3.3 — model comparison matrix

Recorded 2026-08-22. Full suite (`day_planner_agent/evals/scenarios`, 39
scenarios: 24 tier `constraint`, 15 tier `decision`), `--repeat 3`, run
once against each candidate model via the new `--compare-models` flag:

```
day_planner_agent/.venv/bin/python day_planner_agent/evals/runner.py \
    day_planner_agent/evals/scenarios \
    --compare-models gemini-2.5-flash,gemini-2.5-pro --repeat 3
```

## Results (117 trials per model)

| Model | Tier-1 (constraint) | Tier-2 (decision) | p50 wall_s | p95 wall_s | $/scenario | total $ (117 trials) |
|---|---|---|---|---|---|---|
| `gemini-2.5-flash` (currently pinned) | 86.1% | 51.1% | 13.2s | 20.6s | $0.0576 | $2.2448 |
| `gemini-2.5-pro` | 80.6% | 64.4% | 23.3s | 38.9s | $0.2358 | $9.1945 |

Pricing: standard pay-as-you-go USD/1M tokens from
[ai.google.dev/gemini-api/docs/pricing](https://ai.google.dev/gemini-api/docs/pricing),
fetched 2026-08-21 — flash $0.30 in / $2.50 out (thinking billed at the
output rate), pro $1.25 in / $10.00 out at the ≤200k-token tier (this
deployment's prompts are tens of thousands of tokens, well under that
boundary — see `BASELINE.md`'s own token counts). This is the Gemini
Developer API's page, not Vertex AI's own pricing page directly — list
pricing is normally unified across both surfaces for standard-tier
usage, but **verify against Vertex AI's current pricing page before
using this for a real budget decision**, the same "verify, don't assume"
discipline A0.5 already applied to the model-version string itself.

## The one finding that matters most: neither model actually violates a hard constraint

Across all 234 trials (both models combined), **zero** genuine
zone/sleep/existing-event-overlap violations — `no_session_overlaps_any_
zone`, `no_session_overlaps_sleep_or_cooldown`, and `no_session_overlaps_
existing_events` all held 100% of the time, on both models. Every tier-1
miss on both sides is the same "declines rather than violates" pattern
already the dominant finding throughout `BASELINE.md`: non-placement
(`add_calendar_event` called fewer times than expected — ~21-22
instances on each model, the largest single failure category by far),
plus a handful of `every_habit_session_passes_habit_id`,
`placed_minutes_meets_target`, and process-invariant misses.

This matters for reading the tier-1 gap between the two models
correctly: **Pro's lower tier-1 number is not a safety regression.**
Neither model ever placed a session inside a zone, inside sleep, or on
top of an existing event. The gap is entirely about placement
reliability (how often it acts at all) and a few decision-adjacent
misses, not about whether the guardrails hold when it does act.

## Answering the roadmap's three questions

**Whether to change tier at all.** No case for it from this data. Pro
costs ~4.1x more per trial and runs ~1.8x slower at p50 (nearly 2x at
p95) than Flash, and does not improve the number that actually gates a
release — tier-1 is *lower* on Pro in this run, not higher.

**Whether routing planning turns to a stronger model raises tier 1
enough to justify the complexity.** No, per this data — tier-1 moved
the wrong direction on Pro. Where Pro is genuinely better is tier-2
(64.4% vs 51.1%), which is real and worth noting, but tier-2 is
warn-only per A3.1's own gate table; it doesn't need routing complexity
to address a number that isn't blocking anything today.

**Whether an offered model upgrade is safe to take.** This task's actual
contribution to that question is the mechanism, not a one-time verdict:
`runner.py --model <name>` (single override) and `--compare-models
<a>,<b>` (matrix) now exist precisely so a future offered upgrade —
including the mandatory one before `gemini-2.5-flash`'s 2026-10-16
retirement, per `agent.py`'s own comment — can be checked against this
same matrix before being taken, rather than assumed safe.

## Recommendation

**Keep `gemini-2.5-flash` pinned.** Pro's decision-quality edge is real
but doesn't move a gating number, and its cost/latency tax is
substantial for a suite already showing that non-placement — not
constraint violation — is the dominant failure mode on either model.
That points at A4.3 (moving interval arithmetic out of `instruction.md`
and into code) as the more promising lever for tier-1 specifically, not
a blanket model upgrade. Per this task's own scope item 4, **re-run this
matrix after A4.3 ships** — the argument for that work is that it removes
exactly what Flash is weakest at, and this is how to find out whether it
actually did.

## Caveats

- n=3 per scenario (234 trials total) is the same repeat count used
  throughout this suite for cost reasons — not enough to treat any
  single scenario's per-model delta as statistically solid, only the
  aggregate tier-level and cost/latency numbers above.
- Cost figures are a relative comparison, not a production budget
  estimate — real traffic's prompt-caching hit rate (A2.5) isn't
  reproduced by this harness, which builds a fresh runner and session
  per trial.
- This is one snapshot in time. Both model identifiers can change
  underneath this comparison (see `agent.py`'s own note on `gemini-2.5-
  flash`'s retirement date) — re-run rather than trusting an old number
  once either candidate's pricing, version, or behavior has had reason
  to move.
