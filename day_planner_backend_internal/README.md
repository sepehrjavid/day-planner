# Day Planner Backend — internal service

FastAPI service serving only `/internal/*` — connect links, calendar
listings, access-token minting, and disconnect, called by the day-planner
agent on a user's behalf.

This is one of two fully independent deployables split out of what used to
be a single `day_planner_backend` codebase. The other is
[../day_planner_backend_app](../day_planner_backend_app) — signup, login, the
OAuth consent redirect, and calendar-account management. They share no code,
no Dockerfile, and no Python package; this service in particular carries no
`argon2-cffi` dependency at all, since it never hashes a password.

**This service is never granted `allUsers` invoker access, at any point —
launch or not.** Unlike the app service (gated by a single
`var.publicly_exposed` flag), there's no toggle here. `/internal/*` mints
tokens capable of reading and writing a user's calendar; it's meant for the
agent's service account and nothing else. See
[../docs/oauth-design.md](../docs/oauth-design.md) for the full reasoning, including
why IAM Conditions can't achieve this on a single shared service (they can't
be combined with `allUsers` at all).

Provisioned together with the app service by [../terraform](../terraform).

---

## Routes

All require a Google-signed OIDC bearer token, audienced to this service's
own URL, from a service account listed in `INTERNAL_CALLER_SERVICE_ACCOUNTS`.

| Route | Purpose |
|---|---|
| `GET /healthz` | liveness / startup probe (unauthenticated) |
| `POST /internal/connect-link` | mint a single-use connect link for a user |
| `GET /internal/calendars` | every selected calendar, across every linked account |
| `POST /internal/access-token` | fresh ~1hr provider token for one account |
| `POST /internal/disconnect` | revoke at the provider and remove |

`user_id` is always a field in the request body here — the caller is a
trusted service acting on a user's behalf. That trust has one condition, and
it lives on the agent side: the value must come from
`tool_context.session.user_id`, never from anything the model produced. A
model-supplied `user_id` is how a prompt injection in a calendar event title
turns into reading someone else's schedule.

---

## Local development

Use Python 3.12 — matches the container, and newer interpreters don't have a
prebuilt `pydantic-core` wheel yet, which makes install fall back to
compiling from Rust source.

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # then fill in
```

```bash
pytest tests -q
uvicorn app.main:app --reload --port 8081   # different port than the app service
```

Tests fake Firestore, KMS, and the OIDC verification call, so they need no
cloud access. Unlike the app service's tests, these seed `FakeStore` directly
with account data rather than driving a live OAuth connect flow — that code
doesn't exist in this codebase, so there's nothing to drive it with. Minting
a real OIDC token to test `require_internal_caller` against a live server
needs `gcloud auth print-identity-token --impersonate-service-account=... `
against a real service account — not something the test suite attempts.

## Container

```bash
docker build -t day-planner-backend-internal .
```

Multi-stage, runs as uid 10001, base image pinned to an exact patch version,
read-only-root-filesystem compatible.
