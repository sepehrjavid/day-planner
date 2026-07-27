# Day Planner Agent — GCP Deployment Guide

**Goal of this doc:** deploy the agent on GCP using managed services for three capabilities you specifically want:
1. A **hosted, versioned conversation/session store** instead of hand-rolled Firestore documents
2. **Grounding/RAG** over your long-term memory document (and room to grow into a larger knowledge base later)
3. **Multi-tenant session isolation**, so more than one person can use the agent without their data crossing

**This costs more than the bare-minimum design.** An earlier version of this doc recommended calling the Gemini API directly from Cloud Run and skipping the managed "Agent" product, purely to minimize cost. That trade-off no longer holds now that you actually want sessions, RAG, and multi-tenancy — building those three yourself is real engineering effort with real ongoing maintenance cost, even if the cloud bill for a hand-rolled version looks smaller on paper. Paying for the managed versions buys you correctness and time, which is the point of using them. This doc is honest that the bill goes up; see §3 for the trade-off in numbers.

---

## 1. Architecture

```
Slack ──(events API)──▶ Cloud Run (thin adapter: verifies Slack signature,
                          calls Agent Engine, formats the response)
                              │
                              ▼
                    Vertex AI Agent Engine (managed runtime, built with the
                    Agent Development Kit — ADK)
                              │
        ┌─────────────────────┼─────────────────────────┬────────────────────┐
        ▼                     ▼                         ▼                    ▼
  Agent Engine Sessions  Agent Engine Memory Bank  Vertex AI Search /   Custom tool functions
  (hosted, versioned,    (long-term, per-user,     RAG Engine           (Cloud Functions or
   per user_id +          auto-extracted facts)    (grounds on          in-runtime code):
   session_id)                                      memory.md +          get_calendar_events,
                                                      future docs)         get_tasks,
                                                                            validate_plan,
                                                                            suggest_alternatives,
                                                                            add_calendar_event,
                                                                            update_task

Cloud Storage — memory.md per user (source of truth, versioned, incrementally
                synced into the RAG data store)
Secret Manager — Slack token, Notion token, Google OAuth creds
Cloud Scheduler — 07:00 Europe/Stockholm trigger → Cloud Run → Agent Engine
```

Cloud Run's job shrinks to almost nothing: verify the Slack request signature, call Agent Engine's `query`/`stream_query` API with the right `user_id`/`session_id`, and format the reply back into Slack's message format. All the reasoning, session handling, memory, and retrieval now live in Agent Engine.

---

## 2. Services, why they're here, and what they teach you

| Service | Role in this project | Why this capability, specifically | What you'll learn |
|---|---|---|---|
| **Vertex AI Agent Engine** | Managed runtime that hosts the agent (built with Google's **Agent Development Kit — ADK**): reasoning, tool orchestration, and the three capabilities below | This is the product that actually provides hosted sessions, long-term memory, and RAG as first-class features — building all three yourself on bare Cloud Run is a much bigger project than it sounds. | ADK's agent/tool abstractions, deploying a packaged agent to a managed runtime, reading orchestration traces |
| **Agent Engine Sessions** | Hosted, versioned conversation store — one session per `(user_id, session_id)` | This *is* the "hosted, versioned session store" requirement. Google manages consistency, expiry, and history instead of you writing Firestore read/write logic and reasoning about race conditions yourself. | Session lifecycle in a managed agent platform; the distinction between *session* state (this conversation) and *long-term* memory (everything the agent knows about a user) |
| **Agent Engine Memory Bank** | Managed long-term memory: automatically extracts durable facts from conversations and stores them per `user_id`, retrievable by semantic search | Complements — doesn't replace — your `memory.md`. `memory.md` is what the user explicitly wrote; Memory Bank captures things the user *said* in passing that never made it into an explicit edit. | Semantic memory retrieval, generative memory extraction, how to reconcile an explicit memory source with an auto-extracted one (see [day-planner-agent-spec.md §6](day-planner-agent-spec.md)) |
| **Vertex AI Search / RAG Engine** | Grounds agent responses on `memory.md` (and, later, larger docs — journal entries, notes) instead of stuffing the whole file into every prompt | This is the "grounding/RAG over a larger knowledge base" requirement. Retrieval also means the memory doc can grow well past what fits in a prompt window without a rewrite. | Chunking and embeddings, retrieval-augmented generation end to end, keeping a Cloud Storage-backed corpus in sync with a search index (incremental ingestion) |
| **Cloud Storage** | Still holds `memory.md` per user as the human-editable, versioned source of truth; feeds the RAG data store via an incremental connector | Cheap, and keeps a plain-text file you can open and edit directly as the ground truth — the index is a derived artifact, not the source. | Data-store connectors, sync/ingestion jobs, object versioning as an audit trail |
| **Secret Manager** | Slack bot token, Google OAuth client secret, Notion token | First 6 secret versions are free; you'll have ~3-4. | Secrets vs. env vars, IAM-scoped secret access |
| **Cloud Scheduler** | Triggers the 07:00 morning briefing on weekdays | First 3 jobs/month are free — you need exactly 1. | Cron-as-a-service, calling authenticated endpoints |
| **Cloud Run** | Thin Slack adapter only now — signature verification + calling Agent Engine | Scales to zero; this piece of the system is small and cheap regardless of what the rest costs. | Container deployment, request-based serverless billing |
| **Cloud Logging / Monitoring** | Error visibility across the adapter and the agent runtime | Bundled free tier is enough for one low-traffic service. | Structured logging, distributed tracing across services |
| **GitHub Actions** | CI/CD → deploy the Cloud Run adapter and the packaged agent to Agent Engine | Free for reasonable usage on a personal repo. | `gcloud` CLI in CI, workload identity federation |

---

## 3. Cost — and the honest trade-off

**Read this before you enable anything.** Agent Engine, Sessions, Memory Bank, and Vertex AI Search are all products that bill separately from plain Gemini API token usage, and — being newer, still-evolving products — their exact pricing (and even packaging) changes more often than core services like Cloud Run. Treat every number below as **directional, not a quote**. Before you deploy anything real:

1. Open the [Vertex AI Agent Builder / Agent Engine pricing page](https://cloud.google.com/vertex-ai/generative-ai/pricing) and the [Vertex AI Search pricing page](https://cloud.google.com/generative-ai-app-builder/pricing) yourself.
2. Set a **Billing budget alert** (e.g., $10/month) before you enable Memory Bank or a RAG data store — do this first, not after.
3. Consider a short pilot: enable Sessions and RAG first (cheaper, more predictable — storage + retrieval calls), and add Memory Bank second once you've seen a week of real usage and cost.

| Item | What drives the cost | Rough shape |
|---|---|---|
| Cloud Run (Slack adapter) | Requests, mostly idle | Effectively $0 — well within the always-free tier |
| Gemini Flash tokens | Turns/day × tokens/turn | Same as before, a few dollars/month at personal scale |
| Agent Engine runtime | Compute time the managed runtime spends per invocation | Small at low request volume, but non-zero — this is genuinely different from Cloud Run's free tier |
| Agent Engine Sessions | Session storage + reads/writes | Should be modest for one or a handful of users; verify current pricing |
| Agent Engine Memory Bank | Memory extraction (LLM calls under the hood) + storage + retrieval | The one most likely to surprise you — extraction runs an LLM call per session, so cost scales with conversation volume, not just token count |
| Vertex AI Search / RAG Engine | Indexing (embedding calls) + query volume | Cheap for a single small `memory.md`; grows if you feed it much larger corpora later |
| **Total** | | Plausibly **$5–20+/month** for light personal use — meaningfully more than the ~$1-3/month DIY design, and with more variance depending on conversation volume |

### Cost-control tips
- Cap **Cloud Run max instances at 1-2**; it's cheap regardless, but there's no reason not to bound it.
- Start with **Gemini Flash**, not Pro, for the underlying model.
- If Memory Bank cost turns out to dominate, you can disable auto-extraction and rely solely on the explicit `memory.md` + `update_user_memory` path — you lose the "implicit" memory capture but keep the other two capabilities.
- Keep the RAG corpus small and deliberate at first (just `memory.md`) rather than dumping in large document sets pre-emptively.

---

## 4. Setup path (also your learning path)

1. **Project & billing** — create a GCP project, link billing, **set the budget alert now**.
2. **Enable APIs** — Vertex AI, Agent Builder / Agent Engine, Vertex AI Search, Cloud Run, Cloud Build, Secret Manager, Cloud Scheduler, Google Calendar API, Google Tasks API.
3. **Local prototype** — build the agent locally with the ADK, one tool (`get_calendar_events`), no infra yet. Confirms tool-calling works before you pay for anything managed.
4. **Memory bucket** — create the Cloud Storage bucket with versioning enabled, seed it with a first-pass `memory.md` you write yourself.
5. **Stand up the RAG data store** — connect Vertex AI Search / RAG Engine to the bucket, verify it retrieves relevant chunks for a test query.
6. **Deploy to Agent Engine** — package the ADK agent (tools + system prompt + grounding config) and deploy it as a managed runtime.
7. **Enable Sessions** — confirm multi-turn conversations persist correctly across separate calls with the same `user_id`/`session_id`.
8. **Enable Memory Bank** — turn it on after you've watched cost for Sessions + RAG for a bit; confirm extracted facts show up and make sense.
9. **Build the Cloud Run adapter** — Slack signature verification, translating events into Agent Engine calls, deploy manually once to see the raw mechanics.
10. **Wire Secret Manager** — move tokens out of `.env`, mount as env vars in the Cloud Run service.
11. **Add Cloud Scheduler** — the 07:00 trigger, calling Cloud Run with an OIDC identity token.
12. **CI/CD** — GitHub Actions: build/deploy the Cloud Run adapter and redeploy the Agent Engine package on push to `main`.
13. **IaC (optional but recommended)** — codify all of the above in Terraform once it works manually.

---

## 5. Terraform skeleton (structure only)

```
infra/
  main.tf              # provider, project config
  cloud_run.tf          # thin Slack adapter service, IAM invoker binding for Scheduler
  agent_engine.tf        # Agent Engine deployment, Sessions + Memory Bank config
  vertex_ai_search.tf    # RAG data store, connector to the memory bucket
  storage.tf             # bucket for memory.md per user, versioning enabled
  secrets.tf              # secret manager entries (values set via CLI, not committed)
  scheduler.tf            # cron job → Cloud Run OIDC-authenticated call
```

---

## 6. Multi-tenant notes

- Scope everything by `user_id`: the Cloud Storage path (`/users/{user_id}/memory.md`), the Agent Engine session (`user_id` + `session_id`), and Memory Bank (memories are stored per `user_id`).
- Map Slack's own user ID (from the Events API payload) to your internal `user_id` at the Cloud Run adapter layer — don't let Slack's ID double as your primary key everywhere; keep a thin mapping so you can support other front ends later without a rewrite.
- Decide up front whether the RAG data store is **one corpus with metadata filtering by `user_id`**, or **one data store per user**. For a handful of users, per-user data stores are simpler to reason about and impossible to leak across; a single filtered corpus scales better if you expect many users later.
