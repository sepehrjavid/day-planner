# Day Planner Backend — app service

FastAPI service handling everything an end user's browser calls: signup,
login, the OAuth consent redirect, and calendar-account management.

This is one of two fully independent deployables split out of what used to
be a single `day_planner_backend` codebase. The other is
[../day_planner_backend_internal](../day_planner_backend_internal) — the
agent-facing `/internal/*` API. They share no code, no Dockerfile, and no
Python package; each is a standalone service with its own `requirements.txt`.
See [../oauth-design.md](../oauth-design.md) for why they're split rather
than one process with a routing flag: `/internal/*` mints tokens capable of
reading/writing a user's calendar and must never be reachable by an
anonymous browser, at any point — Cloud Run IAM can enforce that only if it's
a genuinely separate service, not a route toggled off by config.

Provisioned together with the internal service by [../terraform](../terraform).

---

## Routes

| Route | Identity comes from | Purpose |
|---|---|---|
| `GET /healthz` | — | liveness / startup probe |
| `POST /auth/signup` | email + password | create an account, returns a session |
| `POST /auth/login` | email + password | returns a session |
| `POST /auth/logout` | session | revokes it server-side |
| `GET /auth/{provider}/start?s=` | the nonce | the link a user clicks |
| `GET /auth/{provider}/callback` | the nonce | where Google redirects back |
| `GET /me` | session | account + every connected calendar account |
| `POST /me/calendar-accounts/connect-link` | session | link another calendar account |
| `PATCH /me/calendar-accounts/{id}/calendars` | session | choose which calendars count |
| `DELETE /me/calendar-accounts/{id}` | session | revoke and remove |

Public exposure is gated by `var.publicly_exposed` in
`../terraform/cloud_run.tf` — `false` (the default, test phase) means nobody
without an explicit `roles/run.invoker` grant can reach this service at all,
not even Google's OAuth redirect. Test that flow locally instead (`uvicorn`
has no IAM layer in front of it) until you're ready to flip it.

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
uvicorn app.main:app --reload --port 8080
```

Tests fake Firestore and KMS, so they need no cloud access. They do not cover
`Store`'s Firestore-specific behaviour (the transactional email claim, merge
semantics, TTL) — that needs the Firestore emulator.

## Container

```bash
docker build -t day-planner-backend-app .
```

Multi-stage, runs as uid 10001, base image pinned to an exact patch version,
read-only-root-filesystem compatible.
