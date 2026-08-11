# Todo — brainstormed, not yet built

Ideas discussed and scoped but deliberately not implemented — a place to
land them so they don't get lost or half-built prematurely. Not a
committed roadmap with dates; a set of design notes to pick back up
cold, in the same spirit as [feature-ideas.md](feature-ideas.md).

One entry here (§4) was actually implemented directly in
`day_planner_agent/instruction.md` for a moment and then deliberately
reverted, specifically so it could be scoped properly alongside §1
first rather than bolted on ad hoc — see that entry for why.

## Suggested build order

There's a real dependency structure here, not an arbitrary list — follow
it in this order:

1. **§3 (learning from past outcomes) first.** Fully independent of
   everything else below — no new schema, no new tools, just an
   unconditional call to an existing tool (`review_habit_week`) plus an
   `instruction.md` rewrite. Lowest risk, smallest surface area, and a
   reasonable way to get oriented in this codebase's conventions
   (`instruction.md` editing style, the test patterns in
   `day_planner_agent/tests`, the commit → Terraform/`gcloud` deploy
   pipeline) before taking on something bigger.
2. **§1 (day zones) next.** The foundational piece everything else here
   depends on. Biggest chunk of work — new Firestore collections/routes/
   tools mirroring the `Habit` pattern already in this codebase (same
   shape of work, just a new entity), plus a real rewrite of the
   placement/conflict-ask logic in `instruction.md` to be zone-general
   instead of work-hours-specific. Treat the three "Behavioral
   requirements" bullets inside §1 as literal acceptance criteria, not
   optional follow-up — they came from auditing the plan against concrete
   scenarios, not from speculating.
3. **§2's placement-policy half can happen anytime, in parallel with §1
   or §3** — "load more of a habit's target onto lighter weekend days"
   doesn't itself need zone data. Only "a weekday-only zone doesn't
   restrict weekends" is a side effect of §1 that needs nothing further
   once §1 lands. Don't treat all of §2 as blocked on §1.
4. **§4 last, and only after §1 is done.** Its own status note explains
   why: built against `update_profile` alone, it would need to be
   immediately redone the moment zones ship — building it first is net
   extra work, not just a sequencing preference.

`feature-ideas.md` has two older, separately-tracked, still-unbuilt items
(capacity forecasting, habit protection) that are out of scope for this
list and aren't repeated here — worth knowing about when weighing overall
priority beyond this document alone.

---

## 1. Day zones — structured scheduling constraints, replacing prose-parsed ones

**Priority: 2 of 4 — build after §3, before §2/§4.** Foundational; §4
depends on it directly, §2 partially does. See "Suggested build order"
above.

**Problem**: scheduling constraints that "almost everyone has" — work
hours, sleep, the wind-down before sleep, the grace period after waking
— currently only exist as free-text sentences in the Memory Bank profile
(`update_profile`), which the model has to notice and correctly apply
every single time it places a habit session. This already caused one real
bug: the agent had "work 9am-5pm" in the profile and scheduled a habit
during it anyway, because a gap between meetings during the day still
*looked* calendar-free to the model even though it wasn't schedule-free.
Patching that in prose (treat work hours as an implicit blackout window)
worked, but doesn't scale — every new "everyone has this" life-structure
category would need its own hand-written paragraph.

**Proposed shape**: a new structured `Zone` entity, sibling to `Habit`,
stored the same way (`day_planner_backend_internal`'s Firestore, not the
free-text profile) and for the same reason habits were pulled out of the
profile — stable ids, deterministic CRUD, and a real many-to-many
relationship to habits, none of which a Memory Bank free-text merge can
guarantee.

**Design principle this rests on**: zones are mostly DB operations, not
agent operations — plain CRUD plus a straightforward containment check the
agent reads off structured data, not new agent-side reasoning. The
temptation to build "smart" zone inference (the model guessing zone
boundaries or eligibility from conversation) should be resisted; that's
exactly the failure mode the work-hours bug already demonstrated; the
agent's job stays "check the data," not "remember and correctly apply a
nuance."

- **`Zone`** (`users/{user_id}/zones/{zone_id}`): `label`, `start_time`,
  `end_time`, `days_of_week` (e.g. `[Mon..Fri]`). Covers work, commute,
  and any custom zone the user names. No row at all = doesn't exist for
  that user (covers "empty if they don't work"). `days_of_week` is also
  how weekend-specific behavior falls out for free — a work zone scoped
  to weekdays just doesn't apply Saturday/Sunday.
- **Sleep is a special, singular zone**, not a generic one, because
  cool-down and wake-up aren't independent zones — they're offsets *from*
  sleep's own boundaries: a `sleep_time`/`wake_time` pair **per day of the
  week** (not just weekday/weekend — a real, low-cost improvement that
  covers any *systematic* weekly variation, e.g. "I always sleep in on
  Sundays"; defaults to one value for all 7 days so the common case needs
  no extra input, with per-day overrides only where they actually differ),
  plus `cool_down_minutes` (derives `[sleep_time − cool_down_minutes,
  sleep_time)`) and `wake_up_buffer_minutes` (derives `[wake_time, wake_time
  + wake_up_buffer_minutes)`). **Out of scope, deliberately**: alternating
  or rotating patterns (e.g. night-shift work every other week) — a
  day-of-week field can't represent "which week," and a real fix needs a
  recurrence-rule engine, which is a meaningfully bigger piece of scope
  than the rest of this section and not justified without evidence anyone
  actually needs it. For now, that case is handled the same way any other
  one-off exception is (see "Behavioral requirements" below) — the user
  states it conversationally when it's relevant that occurrence, rather
  than the standing schedule trying to encode a rotation. See
  [known-issues.md](known-issues.md) for the tracked version of this
  limitation. Open design question, unrelated to the above: model sleep as
  its own `set_sleep_schedule` tool/shape, or as a `Zone` row with extra
  fields and a fixed id like `"sleep"`? Leaning towards a separate shape —
  stretching the generic `Zone` to fit two boundary-relative windows that
  only ever have one instance each adds a generic
  "relative-to-another-zone" abstraction for a case that doesn't need one.
- **`Habit.allowed_zones`**: a new field on the existing `Habit` record —
  a list of zone labels/ids a specific habit may run in, *in addition to*
  the default of any unzoned (open) time. A zone is a restriction unless a
  specific habit is explicitly allowed into it: reading → cool-down,
  checking email → wake-up, a specific exercise habit → work, if the user
  says so. This is what implements "habits are allowed during work hours
  if the habit is defined to allow it" as a real field instead of the
  model having to notice wording buried in a goal string — and should
  *replace* the "the habit's own goal text says so" override currently in
  `instruction.md`, which is real but strictly weaker (relies on the model
  noticing prose rather than checking a field).
- **New tools**: `create_zone`/`update_zone`/`list_zones`,
  `set_sleep_schedule` (or equivalent), plus `allowed_zones` added to
  `update_habit`'s parameters.
- Zones are low-cardinality and change rarely, much like the profile —
  worth preloading alongside it at session start (`_preload_profile` in
  `day_planner_agent/agent.py`) rather than making the agent fetch them on
  every placement decision.

**Behavioral requirements this must preserve** — checked against
`instruction.md`'s current work-hours handling; treat these as acceptance
criteria for §1, not follow-on cleanup:

- **A one-off conversational override must stay one-off.** `allowed_zones`
  is a *standing* override, persisted on the habit. The other override
  that exists today — the user saying "just today, squeeze it into work
  hours" — must not get written to `allowed_zones`; that would silently
  turn a single-day exception into a permanent rule. The instruction layer
  on top of zones still needs a purely conversational override path for a
  single instance, kept entirely separate from the structured field.
- **The "ask on genuine conflict" placement logic must become
  zone-general, not stay work-hours-specific.** Today that logic
  (`instruction.md`'s placement paragraphs) is written as prose naming
  work hours by name. It has to be rewritten to reason over *any* zone a
  habit isn't allowed into — not left as-is with zones bolted on beside
  it, which would leave every zone except work hours silently unenforced
  at the "ask before guessing" level.
- **The retroactive-conflict-check trigger must move with the data it's
  checking.** Today, `instruction.md` re-checks already-scheduled habit
  sessions specifically "whenever the user states or changes a preference
  via `update_profile`." Once work hours/sleep/etc. move out of
  `update_profile` and into `create_zone`/`update_zone`/
  `set_sleep_schedule`, that trigger condition has to be rewritten to fire
  on those calls too — otherwise this exact check silently stops firing
  for precisely the cases (work hours, sleep) that motivated it in the
  first place.

---

## 2. Weekend-aware placement

**Priority: 3 of 4, but partially unblocked — see "Suggested build
order" above.** The placement-policy half (below) needs nothing from §1
and can be built in parallel with §1 or §3; only the
zones-naturally-exclude-weekends half depends on §1 landing first.

Mostly falls out of §1's `days_of_week` (a weekday-only work zone simply
doesn't restrict weekends) and the weekday/weekend split on the sleep
zone. One more deliberate policy worth adding on top, not data-dependent:
when a period being planned spans a weekend, prefer loading a larger share
of a habit's weekly target onto the lighter weekend days before falling
back to weekdays. The existing load-adaptive sizing rule in
`instruction.md` ("packed day → shorter session, light day → longer one")
already partially produces this *if* weekends genuinely are lighter for a
given user — this makes it a deliberate preference instead of an accident
of whatever the calendar happens to look like that week.

---

## 3. Learning from past habit-session outcomes

**Priority: 1 of 4 — build this first.** No dependency on §1/§2/§4 at
all; smallest, lowest-risk slice in this document. See "Suggested build
order" above.

Not a new ML/prediction system — the spec's "predicts completion
likelihood" idea (`day-planner-agent-spec.md` §4.2.2) is real overkill for
what's needed, and `review_habit_week` already has the exact data this
calls for. The gap: nothing *uses* that data automatically today. The
agent only checks a habit's recent history if it happens to already know,
in-context, that the last period went badly.

Proposed fix: before placing a habit's sessions for a new period,
unconditionally call `review_habit_week` for its immediately preceding
period (not just when the agent happens to remember it went badly), and
treat a repeated `bumped_by` — the same time slot losing to the same kind
of conflict more than once — as a signal to avoid that slot next time,
when a comparably good alternative exists. No new storage needed; this is
re-deriving from data that already exists on demand, which avoids a second
copy of the same information going stale.

---

## 4. Ask before replanning around a preference/habit change or event deletion

**Priority: 4 of 4 — build last, only after §1 is done.** See "Suggested
build order" above for why building this against `update_profile` alone,
before §1, would be net extra work.

**Status**: designed, briefly implemented directly in `instruction.md`,
then reverted — not shipped. Do not implement this independently of §1:
§1 now has an explicit requirement (the trigger-migration one, in its
"Behavioral requirements" list) that this exact mechanism must be
rewritten to fire on `create_zone`/`update_zone`/`set_sleep_schedule` as
well as `update_profile` — otherwise it silently stops covering work
hours/sleep the moment zones ship, which is precisely backwards.

**The gap, as of this writing**: three trigger points don't prompt the
agent to check whether anything needs replanning:

- **A preference is added/changed** (`update_profile`): today the agent
  only checks for an outright collision with an already-scheduled habit
  session (added after the work-hours bug — see `instruction.md`'s
  placement paragraphs). It doesn't offer to replan short of a hard
  collision, and doesn't proactively mention what upcoming sessions the
  new preference touches.
- **A habit is updated** (`update_habit` — target change, or
  paused/archived): the tool only changes the habit record. Nothing
  checks whether already-placed future sessions still reflect the new
  target, and nothing asks what should happen to a paused/archived
  habit's already-scheduled future sessions.
- **An event is deleted** (`delete_calendar_event`): the deletion itself
  is confirmed beforehand, but nothing follows up afterward — e.g. if the
  deleted event was what made a habit fall behind its target, nothing
  offers to use the newly-freed time.

**Explicit exception, already decided**: a *brand-new* habit
(`create_habit` called for the first time) should **not** gain a
confirmation gate before its first sessions are placed. That's not
"replanning" — there's no existing plan being disrupted yet — and gating
it would reverse the system's core premise of proactively turning a
stated goal into calendar time (`instruction.md`'s opening paragraph).
Confirmation applies to the three cases above, which all involve
disrupting something already on the calendar; it doesn't apply to placing
something for the first time.

**What the reverted draft looked like** (for reference — not current
behavior): extend the preference-conflict paragraph to offer a replan even
without a hard collision, not just flag one; add a sentence to the
habit-update paragraph asking whether to replan upcoming sessions after a
target change, or remove future sessions after a pause/archive; add a
sentence to the deletion paragraph offering to use freed time for a habit
that's behind. Mechanically this needed no new tools — `get_calendar_events`
already surfaces `habit_id` on tagged events (shipped separately, see
`calendar_tool.py`'s `_trim_google_event`/`_fetch_google_events`), which is
what lets the agent find a given habit's sessions across a future window
in the first place.
