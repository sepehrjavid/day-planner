# Pricing & quota roadmap

Where the current quota mechanism is headed once there's billing to back it.
Nothing below is built — this is a menu for that future work, not a plan with
dates.

## Current state

Every user shares one flat daily quota: `settings.chat_daily_quota` (default
50 messages/day, UTC calendar day), enforced atomically in
`Store.check_and_consume_quota` (`day_planner_backend_app/app/db/store.py`)
and checked by `/me/chat` before it ever calls the agent
(`day_planner_backend_app/app/api/routes/chat.py`). No tiers, no payment, no
per-user overrides yet.

## 1. Tiered plans (subscription)

- Add a `tier` field on the user doc (e.g. `free` / `plus` / `pro`), each
  mapped to its own `daily_quota` in a small tier→limit table instead of one
  global setting.
- `check_and_consume_quota` already takes `daily_limit` as a parameter, so
  tiering is mostly "look up the caller's tier, resolve its limit, pass it
  in" — no rewrite of the enforcement path itself.
- Tier changes should come from a billing webhook (e.g. Stripe subscription
  events), never from a field the user can edit directly.

## 2. Pay-as-you-go / metered overage

- Instead of hard-blocking at the ceiling, let paid tiers go past their
  included quota and track the excess separately (e.g. `overage_count`),
  reconciled against a payment method at the end of the billing period.
- Needs a payment method on file, a per-unit price, and a monthly usage
  report/invoice.
- Even a metered plan should keep *some* hard ceiling as a cost-runaway /
  abuse backstop — "pay as you go" shouldn't mean literally unbounded.
- Natural split: free tier stays hard-capped (today's behavior), paid tiers
  move from hard cap to soft cap + overage billing.

## 3. Quota granularity

- Today's counter is flat messages/day. Options worth weighing later:
  - Token- or cost-based quota instead of raw message count — a single
    "message" can vary a lot in actual agent cost (tool calls, calendar
    context size).
  - A rolling window (trailing 24h) instead of a fixed UTC-midnight reset, so
    a burst that straddles midnight can't get two allowances back to back.

## 4. Billing integration

- Stripe (or similar) subscriptions for tiers; Stripe metered billing for
  pay-as-you-go.
- Webhook updates the user's tier/limit in Firestore; Firestore stays the
  source of truth for enforcement so `/me/chat` never calls out to a billing
  provider synchronously on the hot path.

## 5. User-facing quota visibility

- `QuotaState` already computes remaining/limit/reset_at — nothing surfaces
  it to the client yet. Once there's a UI for it: either add those fields to
  `ChatResponse`, or add a dedicated `/me/quota` endpoint.

## 6. Admin/ops overrides

- A `quota_override` field on the user doc, checked before falling back to
  the tier default — for support cases, promos, or abuse investigation
  without a redeploy.
