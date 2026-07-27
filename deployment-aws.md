# Day Planner Agent — AWS Deployment Guide

**Goal of this doc:** deploy the agent on AWS using managed services for the same three capabilities as the GCP guide:
1. A **hosted, versioned conversation/session store** instead of hand-rolled DynamoDB items
2. **Grounding/RAG** over your long-term memory document (and room to grow into a larger knowledge base later)
3. **Multi-tenant session isolation**, so more than one person can use the agent without their data crossing

**This costs more than the bare-minimum design.** An earlier version of this doc recommended calling Bedrock's `Converse` API directly from a plain Lambda function, skipping Bedrock Agents entirely, purely to save money. That trade-off no longer holds now that you want sessions, RAG, and multi-tenancy for real — those are exactly the things Bedrock Agents exists to manage for you. See §3 for the honest cost picture, including the one AWS-specific cost trap (OpenSearch Serverless) that's now avoidable thanks to a cheaper vector store option.

---

## 1. Architecture

```
Slack ──(events API, via Lambda Function URL)──▶ AWS Lambda (thin adapter:
                          verifies Slack signature, calls Bedrock Agent
                          Runtime, formats the response)
                              │
                              ▼
                    Amazon Bedrock Agent (managed orchestration)
                              │
        ┌─────────────────────┼─────────────────────────┬────────────────────┐
        ▼                     ▼                         ▼                    ▼
  Bedrock Agents          Bedrock Agents            Bedrock Knowledge   Action groups (Lambda):
  Session management      built-in memory           Bases (RAG) —       get_calendar_events,
  (hosted, encrypted,     (memoryId — summarized     grounds on          get_tasks,
   per sessionId)          context across sessions,   memory.md +         validate_plan,
                            per user)                  future docs;        suggest_alternatives,
                                                        vector store =       add_calendar_event,
                                                        Amazon S3 Vectors     update_task

S3 — memory.md per user (source of truth, versioned, synced into the Knowledge Base)
SSM Parameter Store — Slack token, Notion token, Google OAuth creds
EventBridge Scheduler — 07:00 Europe/Stockholm trigger → Lambda → Bedrock Agent
```

Lambda's job shrinks to almost nothing: verify the Slack request signature, call `bedrock-agent-runtime InvokeAgent` with the right `sessionId`/`memoryId`, and format the reply back into Slack's message format. Action groups (the actual tools) stay as small, single-purpose Lambda functions, but the orchestration, session handling, memory, and retrieval now live in Bedrock Agents.

---

## 2. Services, why they're here, and what they teach you

| Service | Role in this project | Why this capability, specifically | What you'll learn |
|---|---|---|---|
| **Amazon Bedrock Agents** | Managed orchestration: reasoning, tool calling (via action groups), and the three capabilities below | Bedrock Agents' orchestration itself has no separate charge beyond the underlying model invocations — you're paying for the capability, not a markup on top of it. | Action groups, agent instructions, orchestration traces for debugging tool-calling decisions |
| **Bedrock Agents Session management** | Hosted, encrypted session state per `sessionId` | This *is* the "hosted, versioned session store" requirement — replaces hand-rolled DynamoDB session items and the race-condition handling that comes with rolling your own. | Session lifecycle and TTL in a managed agent platform |
| **Bedrock Agents built-in memory (`memoryId`)** | Retains summarized context across sessions per user, so the agent recalls prior conversations without you re-feeding them | Complements — doesn't replace — your `memory.md`. `memory.md` is what the user explicitly wrote; built-in memory captures what came up in conversation but was never explicitly saved. | Session memory vs. cross-session memory, summarization strategies, reconciling an explicit memory source with an auto-summarized one (see [day-planner-agent-spec.md §6](day-planner-agent-spec.md)) |
| **Bedrock Knowledge Bases** | RAG over `memory.md` (and, later, larger docs) in S3 | This is the "grounding/RAG over a larger knowledge base" requirement. Retrieval means the memory doc can grow well past prompt-window limits without a rewrite. | Chunking and embeddings, retrieval-augmented generation end to end, keeping an S3-backed corpus in sync via ingestion jobs |
| **Amazon S3 Vectors** (vector store for the Knowledge Base) | Native vector index inside S3, used as the Knowledge Base's backing store | **This is the important cost decision on AWS.** The traditional Bedrock Knowledge Base vector store is **OpenSearch Serverless**, which has a real minimum cost floor (OCU-based billing) regardless of how little data you have — it's the single most common "why is my hobby AWS bill $50+" surprise. S3 Vectors is a much cheaper, S3-native alternative purpose-built for exactly this scale. | Vector store trade-offs, why "managed" doesn't mean "cheap by default" — the backing store choice matters as much as the feature choice |
| **S3** | Still holds `memory.md` per user as the human-editable, versioned source of truth; feeds the Knowledge Base via ingestion jobs | Fractions of a cent for a small file; the index is a derived artifact, not the source. | Ingestion/sync jobs, S3 bucket versioning as an audit trail |
| **SSM Parameter Store (Standard tier)** | Slack bot token, Google OAuth client secret, Notion token | Free, unlike Secrets Manager ($0.40/secret/month + API charges) — sufficient for a handful of static tokens. | Parameter Store vs. Secrets Manager trade-offs |
| **EventBridge Scheduler** | Triggers the 07:00 morning briefing on weekdays | ~22 invocations/month (weekdays) — effectively free. | Cron-as-a-service on AWS |
| **AWS Lambda** | Thin Slack adapter + the action-group functions | Always-free tier: 1M requests + 400,000 GB-seconds/month, permanently. | Serverless function packaging, event-driven handlers |
| **CloudWatch Logs/Alarms** | Error visibility across the adapter and the agent | Free tier covers a low-traffic setup. | Structured logging, alarm-based notifications |
| **GitHub Actions** | CI/CD → deploys the Lambda adapter and the Bedrock Agent/Knowledge Base config | Free for reasonable personal-repo usage. | OIDC federation to AWS (no long-lived access keys in CI) |

### The one thing to actively avoid
**Do not let the Knowledge Base default to OpenSearch Serverless.** Explicitly choose Amazon S3 Vectors as the vector store when you create it. This single choice is the difference between a Knowledge Base costing a couple of dollars a month and one with a real fixed floor regardless of usage.

---

## 3. Cost — and the honest trade-off

**Read this before you enable anything.** Bedrock Agents' Session management and built-in memory are relatively new features, and — like the GCP equivalents — their exact pricing and packaging move faster than core services like Lambda. Treat every number below as **directional, not a quote**.

1. Check the [Bedrock pricing page](https://aws.amazon.com/bedrock/pricing/) and [S3 pricing page](https://aws.amazon.com/s3/pricing/) (for S3 Vectors specifically) yourself before deploying.
2. Set an **AWS Budget alert** (e.g., $10/month) before you create the Knowledge Base — do this first.
3. Double- and triple-check the vector store selection when creating the Knowledge Base — this is the step most likely to silently blow the budget if defaulted.

| Item | What drives the cost | Rough shape |
|---|---|---|
| Lambda (adapter + action groups) | Requests, mostly idle | Effectively $0 — well within the permanent free tier |
| Bedrock model tokens (Nova Micro/Lite or Claude Haiku) | Turns/day × tokens/turn | Same as before, a few dollars/month at personal scale — model choice matters a lot here, start cheap |
| Bedrock Agents Session management | Session storage + reads/writes | Should be modest for one or a handful of users; verify current pricing, this is a newer feature |
| Bedrock Agents built-in memory | Summarization (LLM calls under the hood) + storage | Scales with conversation volume, not just token count — watch this one |
| Bedrock Knowledge Base — ingestion | Embedding model calls when `memory.md` changes | Cheap for one small file, re-run only on actual edits |
| Bedrock Knowledge Base — S3 Vectors storage/query | Vector count + query volume | Cheap at this scale — this is the number that would *not* be cheap with OpenSearch Serverless |
| **Total** | | Plausibly **$5–15+/month** for light personal use, avoiding OpenSearch Serverless — meaningfully more than the ~$1-5/month DIY design, but without the OpenSearch cost floor this could otherwise hit |

### Cost-control tips
- Model choice matters more than the managed-feature overhead: start with **Amazon Nova Micro** or **Claude Haiku**, not a larger model.
- Set Lambda **reserved concurrency of 1-2** to cap blast radius from bugs.
- If built-in memory cost turns out to dominate, you can disable it and rely solely on the explicit `memory.md` + `update_user_memory` path.
- **S3 Vectors over OpenSearch Serverless, always, at this scale** — this is the single biggest lever on the AWS side.

---

## 4. Setup path (also your learning path)

1. **Account & billing** — set up (or reuse) an AWS account, enable Cost Explorer, **set the budget alert now**.
2. **Request Bedrock model access** — enable the specific models you'll use in the Bedrock console.
3. **Local prototype** — call `bedrock-runtime Converse` from a local script with one tool (`get_calendar_events`), no infra yet.
4. **Memory bucket** — create the S3 bucket with versioning enabled, seed it with a first-pass `memory.md` you write yourself.
5. **Create the Knowledge Base** — connect it to the bucket, **explicitly select Amazon S3 Vectors** as the vector store, verify ingestion and a test retrieval query.
6. **Create the Bedrock Agent** — instructions, action groups (Lambda-backed tools), attach the Knowledge Base for grounding.
7. **Enable Session management** — confirm multi-turn conversations persist correctly across calls with the same `sessionId`.
8. **Enable built-in memory** — turn on after watching cost for Sessions + Knowledge Base for a bit; confirm summarized recall works as expected.
9. **Build the Lambda adapter** — Slack signature verification, `InvokeAgent` calls, response formatting; add a Function URL.
10. **Wire Parameter Store** — tokens out of `.env`, read via IAM-scoped `ssm:GetParameter` calls.
11. **Add EventBridge Scheduler** — the 07:00 rule invoking Lambda directly.
12. **CI/CD** — GitHub Actions using OIDC to assume a deploy role, deploying the adapter and the agent/Knowledge Base config on push to `main`.
13. **IaC (optional but recommended)** — Terraform or AWS SAM/CDK once it works manually.

---

## 5. IaC skeleton (structure only, Terraform example)

```
infra/
  main.tf              # provider, account/region config
  lambda.tf              # adapter function, Function URL, action-group functions
  bedrock_agent.tf        # agent definition, action groups, instructions
  knowledge_base.tf        # Bedrock Knowledge Base + S3 Vectors index (NOT OpenSearch Serverless)
  s3.tf                    # memory.md bucket per user, versioning enabled
  ssm.tf                    # parameter definitions (values set via CLI, not committed)
  eventbridge.tf             # scheduler rule → Lambda target
  iam.tf                     # least-privilege execution roles
```

---

## 6. Multi-tenant notes

- Scope everything by user: the S3 path (`/users/{user_id}/memory.md`), the Bedrock `sessionId`, and the `memoryId` used for built-in memory.
- Map Slack's own user ID (from the Events API payload) to your internal `user_id` at the Lambda adapter layer — keep this mapping thin and separate so you're not locked to Slack as the only front end later.
- Decide up front whether the Knowledge Base is **one index with metadata filtering by `user_id`**, or **one Knowledge Base per user**. For a handful of users, per-user Knowledge Bases are simpler and impossible to leak across; a single filtered index scales better if you expect many users later.
