# Known issues

Deferred problems worth tracking, not yet fixed. Each entry should have
enough context to pick back up cold.

## Auto-removing a calendar on a 404 is not safe enough to ship as-is

**Where it would live**: `day_planner_agent/calendar_tool.py` (detecting the
404 during `get_calendar_events`/`add_calendar_event`) and a
`day_planner_backend_internal` `/internal/remove-calendar` route +
`Store.remove_calendar` (pruning it from Firestore). A first version of this
was built, tested, deployed, and then reverted (see commit history around
"Resolve event times against the calendar's own timezone and gate writes on
access" / its revert) specifically because of the risk below — the timezone
and write-access-gating fixes from that same commit were kept.

**The problem**: treating any HTTP 404 from Google's Calendar API as proof
that the user actually deleted or unsubscribed from that calendar is too
strong an inference. A 404 with the same shape could also come from a bug on
our side — e.g. a mismatched `calendar_id`/`account_id` pairing introduced
by some unrelated change, or Google deprecating/changing the v3 API. Using
`googleapiclient`'s generated client (`build("calendar", "v3")...`) rather
than a hand-rolled URL rules out the "we typo'd a path string" version of
this, but not the others.

**Why it's worse than a normal bug**: per the investigation that motivated
this feature, there is no resync path anywhere in the codebase — the
calendar list is fetched from Google exactly once, at OAuth connect time,
and never refreshed. So if a false-positive 404 causes us to prune a
calendar that's still alive, the only way for the user to get it back is to
fully disconnect and reconnect that Google account. A bug here doesn't just
misbehave once — it silently and permanently forgets a real calendar, at
whatever scale the triggering bug reaches (could be one user, could be the
whole fleet, and nothing would surface it).

**What would make it safe enough**:
- Log a structured warning (or emit a metric) every time a removal actually
  fires, so a systemic bug causing mass false-positive 404s is visible
  immediately in logs/monitoring instead of silently eating calendars.
- Consider requiring the same calendar to 404 twice, across two separate
  requests, before pruning — trades a slower cleanup for real protection
  against acting on one anomalous response.
- Inspect the error body's structured `reason`/`domain` fields (Google's own
  Calendar API 404s carry a specific JSON error shape) rather than trusting
  the bare HTTP status code alone, to rule out a 404 coming from something
  other than the Calendar API method itself.

None of the above were implemented in the reverted version. Whoever picks
this back up should decide how much of this is worth the complexity before
re-shipping it.
