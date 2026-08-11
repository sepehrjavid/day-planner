# Feature ideas — differentiation roadmap

This started as a menu of candidate features, not a plan with dates — items
without a **Status: Built** line are still just that, a proposal. Each one
came out of a review of [day-planner-agent-spec.md](day-planner-agent-spec.md)
(the aspirational product roadmap) against what's actually shipped today: a
Google Calendar agent (multi-account get/add/update/delete events), a
structured preference profile + free-form memory via Vertex AI Memory Bank,
and habit-goal placement that fits sessions around existing calendar load
(see [day_planner_agent/instruction.md](../day_planner_agent/instruction.md)).

These three were picked over the rest of the spec's roadmap (wearables,
auto-decline, team tiers, multi-provider calendars) because none of them
require a new integration — they're synthesis and bookkeeping on top of
calendar access the agent already has, which makes them the highest
differentiation-per-effort items on the list.

Each entry below is written to be pasted as the opening message of a fresh
chat when it's time to design and implement that item.

---

## 1. Capacity forecasting

**Problem**: The agent is purely reactive — it only looks at the calendar
when asked. Nothing warns the user in advance that a week can't hold what's
already committed plus the standing habit goals. The spec calls this out
directly (§4.1.5, "calendar overload causes habit abandonment"): a habit
doesn't fail because the user didn't want it, it fails because nobody
flagged the week as unwinnable before it was too late to do anything about
it. It's also a silent gap in the existing habit-placement logic —
[instruction.md](../day_planner_agent/instruction.md) already looks at
*daily* load to choose where a session goes, but has no way to say "this
week genuinely doesn't have room for the stated goal" and will just cram in
whatever fits without telling anyone.

**What exists to build on**: `get_calendar_events` in
[calendar_tool.py](../day_planner_agent/calendar_tool.py) already returns
everything needed to compute booked-vs-free time over an arbitrary period —
no new calendar-side capability required.

**What's new**: a synthesis step — sum committed time per day/week, compare
against stated habit goals (from the profile) and some notion of available
hours, and surface a warning. Open design questions: does this run only
when asked ("plan my week") or proactively at conversation start; where does
"available hours" come from when the profile has no explicit work-hours
preference stored; what's the actual warning threshold.

**Starter prompt for the design chat**:
> I want to design a capacity-forecasting feature for my day-planner agent
> (ADK-based, Gemini, tools in `day_planner_agent/calendar_tool.py` and
> `memory_tools.py`). Today the agent only looks at the calendar when asked;
> I want it to detect when a week's existing commitments plus the user's
> standing habit goals (stored via `update_profile`) don't actually fit, and
> surface that as a warning — e.g. "your commitments exceed free time by 5
> hours this week, X habit is at risk." Help me design: (1) a new tool or
> extension to existing calendar tools that computes free-vs-committed time
> over a period, (2) how to define "available hours" given we don't
> currently store work-hours/sleep-boundary preferences, (3) whether this
> should be proactive (checked every session) or on-demand only, and (4) how
> to phrase the warning so it's useful without being naggy. See
> `docs/feature-ideas.md` §1 for the full problem statement.

---

## 2. Weekly behavioral review

**Status**: Built. `review_habit_week` in
[habit_tools.py](../day_planner_agent/habit_tools.py) compares habit
sessions the agent planned against what actually survived on the calendar,
and names what bumped anything that didn't. On-demand only — see "Deferred"
below for the one piece that didn't ship.

**Original problem** (kept for context): the agent had no memory of
*outcomes* — only of preferences and events. If a habit block got deleted,
moved, or silently skipped, nothing captured that or asked why, so the same
losing pattern would get scheduled again next week with no one the wiser.
This is the specific capability the spec's positioning is actually built on
(§4.3.1, "you miss gym on Thursdays because meetings run late") — the one
thing neither a calendar app nor a habit app can do alone, since it
requires seeing both at once over time.

**How it was solved**: the blocking design problem was that
`get_calendar_events` only shows current state, not history — a deleted
event leaves nothing to diff against. Two pieces fixed that: `add_calendar_event`'s
`habit_id` param tags the Google Calendar event itself
(`extendedProperties.private`, `calendar_tool.py`) so it's identifiable
later, and a separate plan-log record — `day_planner_backend_internal`'s
`habit_sessions` collection (`POST`/`GET /internal/habit-sessions`) — is
written alongside it and *outlives* the calendar event on purpose, so
`review_habit_week` still has something to compare against even after the
event is gone. `update_calendar_event` keeps that log's planned time in
sync when the agent itself reschedules a tagged session, so a deliberate
replan doesn't read as a loss later.

**Deferred: a proactive/cron trigger**

`review_habit_week` only runs when asked — nobody gets a review unless they
ask "how did this week go?". A proactive version (fired automatically, e.g.
every Monday morning, with no user in the loop) is real future work, not
built. This was deliberate: validating the comparison logic itself (is
`bumped_by` actually useful, do outcomes get bucketed sensibly) was worth
doing before spending effort on delivery infrastructure for it.

**Starter prompt for that follow-up design chat**:
> I want to add a proactive trigger for my day-planner agent's
> `review_habit_week` tool (`day_planner_agent/habit_tools.py`) — right now
> it only runs when the user asks about their week. I want it to fire on
> its own, e.g. weekly per user, likely via Cloud Scheduler (already used
> elsewhere in this project's GCP deployment — see `docs/deployment-gcp.md`)
> hitting a new lightweight endpoint that invokes the agent with a
> synthetic "review last week" prompt, similar to how `/me/chat`
> (`day_planner_backend_app`) invokes it today but without a live user
> waiting on the response. Help me design: (1) how the agent's reply
> reaches the user with nobody watching the chat in real time (push
> notification? email digest? queued as the opening message next time they
> open a session?), (2) how to avoid "reviewing" a week where the user had
> zero planned habit sessions (silence isn't a review — see
> `review_habit_week`'s own empty-sessions-vs-everything-kept distinction),
> and (3) whether this should be opt-in per user or on by default. See
> `docs/feature-ideas.md` §2 for the full history of this feature.

---

## 3. Habit protection (soft version)

**Problem**: Once the agent places a habit block, it's indistinguishable
from any other event — there's no concept of "this time is protected." If
something conflicting gets added on top of it (by the user, or in a fuller
version by an external invite), the agent has no special awareness that a
*habit* just lost, unless someone happens to ask. This is the gap directly
under the product's positioning statement ("habits aren't afterthoughts")
— structurally, today, they still are.

This is deliberately scoped down from the spec's §4.2.1 ("auto-decline
conflicting invites automatically"). Unilaterally declining something on a
user's behalf is a high-blast-radius, hard-to-reverse action — the same
risk category already flagged in [known-issues.md](known-issues.md) for
the reverted calendar-auto-removal feature, where an automated action that
is wrong even rarely causes silent, hard-to-notice damage. The soft version
keeps a human in the loop: detect the collision, ask which one should move,
never act unilaterally.

**What exists to build on**: `add_calendar_event`/`update_calendar_event`
in [calendar_tool.py](../day_planner_agent/calendar_tool.py) already handle
creating and moving events.

**What's new**: tagging habit-created events so they're identifiable later
— Google Calendar's extended properties fit this — and a check, run when a
new event is added or an existing one is edited, for overlap against any
tagged block, which triggers a clarifying question instead of a silent
write.

**Starter prompt for the design chat**:
> I want to design a "soft" habit-protection feature for my day-planner
> agent (ADK-based, Gemini, tools in `day_planner_agent/calendar_tool.py`).
> Today habit blocks the agent creates are indistinguishable from any other
> calendar event, so nothing notices when a new event collides with one.
> I want the agent to detect the collision and ask the user which one
> should move — explicitly not auto-decline or auto-move anything
> unilaterally (see the auto-removal caution in `docs/known-issues.md` for
> why I'm scoping out unilateral action). Help me design: (1) how to tag
> agent-created habit events so they're identifiable later (Google
> Calendar extended properties vs. some other approach), (2) where the
> overlap check hooks into the existing `add_calendar_event`/
> `update_calendar_event` flow, (3) how the clarifying-question UX should
> work when a collision is detected mid-conversation, and (4) what happens
> if the colliding event was created outside this agent entirely (e.g.
> directly in Google Calendar) — does anything catch that case at all
> given the agent only sees the calendar when it's invoked. See
> `docs/feature-ideas.md` §3 for the full problem statement.
