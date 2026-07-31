# Day Planner Backend

FastAPI service on Cloud Run that owns user accounts and calendar connections.

It exists because Vertex AI Agent Engine structurally cannot do OAuth: the
authorization code flow needs a browser redirect to a public HTTPS endpoint,
and Agent Engine is invoke-only. So signup, consent, code exchange, and
credential storage happen here; the agent's tools ask this service for a
short-lived access token when they need one.

See [../oauth-design.md](../oauth-design.md) for the design rationale.

---

## Layout

```
app/
  main.py              create_app() + lifespan. Wiring only.
  api/
    deps.py            dependencies — where identity is established
    router.py          aggregates the route modules
    routes/            health, auth, oauth, me, internal
  core/
    config.py          Settings (env)
    security.py        Argon2 hashing, internal OIDC verification
    pkce.py
  db/
    models.py          domain types, no Firestore import
    store.py           the Firestore client and all queries
  providers/
    base.py            OAuthProvider interface
    google.py
  schemas/             pydantic request/response models
  services/
    connections.py     the connect / refresh / disconnect use cases
    crypto.py          Cloud KMS
  web/pages.py         the two HTML pages a browser lands on
tests/
terraform/
```

Route handlers stay thin: they establish identity, call a service, and map
domain exceptions to status codes. The logic lives in `services/`, which is
reachable without an HTTP client.

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
| `POST /internal/connect-link` | OIDC + body | mint a link on a user's behalf |
| `GET /internal/calendars?user_id=` | OIDC + query | every selected calendar, all accounts |
| `POST /internal/access-token` | OIDC + body | fresh token for one account |
| `POST /internal/disconnect` | OIDC + body | revoke and remove |

### Why `/internal/*` isn't gated by Cloud Run IAM

The service is deployed allow-unauthenticated because Google's OAuth servers
must be able to redirect an anonymous browser to `/auth/*/callback`. Cloud Run
IAM is service-wide and can't exempt one route, so `/internal/*` verifies a
Google-signed OIDC token in application code ([app/core/security.py](app/core/security.py))
— the same mechanism, one layer up. An empty
`INTERNAL_CALLER_SERVICE_ACCOUNTS` fails closed with a 503 rather than opening
the door.

### The identity rule

Each route group has exactly one legitimate source of `user_id`, and
[app/api/deps.py](app/api/deps.py) is where that's enforced:

- `/me/*` — the session token. Never the request body. This is what makes
  `account_id` safe as a path parameter: it's always resolved *within* the
  caller's own subcollection, so another user's id is a 404.
- `/internal/*` — the request body, from an authenticated service. The agent
  must fill it from `tool_context.session.user_id`, never from model output.
- `/auth/*` — the single-use nonce.

---

## Firestore

```
users/{user_id}
  email, password_hash (argon2id), email_verified
  default_account_id                  # target for writes
  created_at, updated_at

users/{user_id}/connected_accounts/{provider}__{provider_account_id}
  provider: "google"
  credential_type: "oauth2"           # the Apple/CalDAV discriminator
  provider_account_id                 # OIDC sub — stable; emails get reassigned
  email                               # display only
  encrypted_refresh_token             # KMS ciphertext, base64
  kms_key_name, scopes
  status: "active" | "needs_reauth"
  calendars: [{calendar_id, summary, is_primary, selected}]

user_emails/{normalized_email} -> {user_id}    # signup uniqueness lock
sessions/{sha256(token)}                        # TTL on expires_at
login_throttle/{normalized_email}               # TTL on locked_until
oauth_states/{nonce}                            # TTL on expires_at, single-use
```

A few decisions worth knowing:

**A user can link many calendar accounts.** Personal Google and work Google are
separate documents in the subcollection, each with its own credential and its
own calendar list. `GET /internal/calendars` flattens the selected ones across
all of them, so the agent can plan against everything at once.

**The account document ID is `{provider}__{provider_account_id}`.** Firestore
can't enforce a unique index, but it can enforce a document ID — so
re-consenting to an account you already linked heals it in place instead of
growing a duplicate. Calendar selection survives that reconnect.

**`user_emails/` is a real constraint, not a cache.** Firestore has no unique
index, so "query for the email, then write" races two concurrent signups into
duplicate accounts. Claiming a document keyed by the address, inside a
transaction, is the constraint.

**Nothing stores access tokens or raw session tokens.** Access tokens last an
hour and the refresh token can always mint another; sessions are stored as
SHA-256 digests so a database dump can't be replayed as a set of live logins.

---

## Auth notes

Passwords use **Argon2id** via argon2-cffi's defaults, which track OWASP's
recommended parameters. `check_needs_rehash` means those parameters can be
raised later and existing users get upgraded transparently on their next login.

Login is deliberately uniform: an unregistered address and a wrong password
return the identical response, and the unregistered path still runs a dummy
Argon2 verification so the timing matches. Otherwise the endpoint is a
membership oracle. Repeated failures lock the address for 15 minutes.

Password policy is length-only (12–1024), per NIST SP 800-63B — composition
rules measurably push users toward weaker, more predictable passwords. The
upper bound matters too: Argon2 is intentionally expensive, so an unbounded
input is a cheap way to burn the server's CPU.

> **Worth considering:** GCP Identity Platform (or Firebase Auth) would hand
> you password reset, email verification, MFA, breach-list checks, and
> federated login for roughly nothing, and would let you delete
> `core/security.py` and `routes/auth.py`. What's here is solid, but rolling
> your own auth means owning those flows forever — notably, there is currently
> **no password reset and no email verification**, and `email_verified` is
> written as `False` and never updated.

---

## Local development

Use **Python 3.12** — it's what the container image runs
([Dockerfile](Dockerfile)), and `pydantic-core`'s pin doesn't ship a prebuilt
wheel for very new interpreters (e.g. 3.14), which makes `pip install` fall
back to compiling it from Rust source via maturin/cargo and fail without a
working toolchain. If `python3 --version` isn't 3.12.x, point the venv at a
3.12 install explicitly (`brew install python@3.12` on macOS):

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # then fill it in
```

Tests fake Firestore, KMS, and Google, so they need no cloud access:

```bash
pytest tests -q
```

They cover the routes — auth boundaries, the nonce lifecycle, `needs_reauth`
transitions, multi-account fan-out. They do **not** cover `Store`'s
Firestore-specific behaviour (the transactional email claim, merge semantics,
TTL), because `FakeStore` reimplements it. Covering that properly means running
against the Firestore emulator — worth doing before this handles real users.

To run against real GCP you need ADC and a real KMS key:

```bash
gcloud auth application-default login
uvicorn app.main:app --reload --port 8080
```

Add `http://localhost:8080/auth/google/callback` to the OAuth client's
authorized redirect URIs and set `PUBLIC_BASE_URL=http://localhost:8080`.

---

## Container

Multi-stage, runs as uid 10001, base image pinned to an exact patch version.
Application code is root-owned and the runtime user cannot write to it, so the
image also runs fine with a read-only root filesystem.

```bash
docker build -t day-planner-backend .
```

---

## Deploying

1. `cd terraform && terraform init`
2. Create `terraform.tfvars`:
   ```hcl
   project_id                  = "your-project"
   google_oauth_client_id      = "xxxx.apps.googleusercontent.com"
   agent_service_account_email = "day-planner-agent@your-project.iam.gserviceaccount.com"
   image                       = "europe-north1-docker.pkg.dev/your-project/day-planner/backend:bootstrap"
   ```
3. `terraform apply` — Firestore + TTL policies, the KMS key, the secret,
   Artifact Registry, the service account, and the Cloud Run service.
4. Put the OAuth client secret in Secret Manager (never in tfvars or state):
   ```bash
   printf '%s' "$CLIENT_SECRET" | gcloud secrets versions add \
     day-planner-google-oauth-client-secret --data-file=-
   ```
5. Build and push, then re-apply with the real digest.
6. Compare the `service_uri` output against `public_base_url`. If they differ,
   set `var.public_base_url` to the real URI and re-apply — otherwise every
   redirect fails with `redirect_uri_mismatch`.
7. Register `terraform output oauth_redirect_uri` verbatim on the OAuth client.

### Console gotchas

- **The OAuth client must be type "Web application"**, not Desktop. A Desktop
  client will refuse the Cloud Run redirect URI.
- **A consent screen left in "Testing" status issues refresh tokens that expire
  after 7 days.** The scheduled morning briefing will work all week and die
  every weekend until the app is published.
- **`calendar.readonly` and `calendar.events` are sensitive scopes.** Publishing
  to production with them requires Google's verification review; unverified,
  you're capped at 100 users and everyone sees an "unverified app" warning.
- **Internal vs External user type.** Internal skips both the cap and
  verification, but only with a Workspace org containing every user. Personal
  `@gmail.com` users mean External.

---

## Wiring the agent (not done yet)

Nothing on the agent side has been changed. When it is:

```python
user_id = tool_context.session.user_id   # NEVER a model-supplied argument
```

That single line is the tenant boundary. If `user_id` ever becomes a tool
parameter the model can fill in, a prompt injection in a calendar event title
can make the agent read someone else's calendar.

The shape for a multi-calendar lookup:

1. `GET /internal/calendars?user_id=…` → every selected calendar, each tagged
   with the `account_id` it belongs to.
2. For each distinct `account_id`, `POST /internal/access-token` → a ~1 hour
   token.
3. Fetch events per calendar and merge.
4. On a `409`, surface `POST /internal/connect-link`'s URL instead of failing.
   `needs_reauth` in the step-1 response means "tell the user this calendar
   went stale", not "pretend it isn't there".

`calendar_id` and `calendar_type` can then come out of the Memory Bank profile.
