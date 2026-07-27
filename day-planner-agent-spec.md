# Interactive Day Planner Agent - Project Specification

**Project Name:** Agentic Day Planner  
**Status:** Concept / MVP Planning  
**Platform:** GCP Vertex AI Agents (primary) / AWS Bedrock Agents (alternative)  
**Timeline:** 2-3 weeks  
**Owner:** Cloud Engineer (Stockholm)

---

## 1. Project Overview

An AI agent that helps you plan your day by:
- Understanding your calendar, tasks, and preferences
- Validating whether plans are feasible given constraints (travel time, duration, energy, preferences)
- Suggesting trade-offs when conflicts exist (e.g., "Move dinner 1 hour later to fit gym")
- Being conversational and interactive (chat-based, not batch reports)

**Key Differentiator:** The agent doesn't just dump a schedule—it reasons about feasibility, identifies conflicts, and actively suggests solutions.

---

## 2. Objectives

### Primary Objectives (MVP)
1. **Reduce morning planning friction** - No more "I forgot to fit X in"
2. **Learn agentic AI platforms** - Hands-on with Vertex AI Agents or Bedrock
3. **Interactive reasoning** - Agent validates plans and suggests fixes in real-time
4. **Calendar automation** - Successfully add/update events based on agent decisions

### Secondary Objectives (Phase 2)
1. Integrate Revolut spending data (budget-aware planning)
2. Add Nordea/BankID integration (financial visibility)
3. Multi-day planning (not just daily)
4. Learning recommendations (tech news based on your schedule gaps)

---

## 3. User Flows

### Flow 1: "Quick Plan Check" (Most Common)

```
User (Slack):
"I have a report due 5 PM, want to go to the gym 6-7 PM, 
and dinner at 8 PM. Does this work?"

Agent thinks:
1. Fetch calendar → "Report meeting 2-5 PM"
2. Fetch preferences → "Gym 45 min, commute 30 min, dinner 90 min"
3. Validate → "5 PM done, 5:30 PM travel + freshen up, 6:45 PM dinner start = 8:15 PM finish. Tight."
4. Suggest → "Dinner at 8 PM won't work. Options:
   a) Move dinner to 8:30 PM (gives 15 min buffer)
   b) Skip gym today, do tomorrow
   c) Gym 5:30-6:15 PM (shorter), but rushed"

User replies:
"Let's do 8:30 PM dinner"

Agent:
"Done! Calendar updated:
- Report: 2-5 PM
- Gym: 5:30-6:15 PM
- Freshen up: 6:15-6:45 PM
- Dinner: 8:30 PM
Ready?"

User: "Yes"

Agent updates Google Calendar with all events.
```

---

### Flow 2: "Plan My Day" (Morning Briefing Alternative)

```
User (Slack):
"Plan my day. I have: report due 5 PM, gym, dinner at 8 PM, 
catch up with Alex. Prioritize"

Agent thinks:
1. Fetch all calendar + tasks
2. Analyze workload → "Report 2h, gym 45m, dinner 90m, catchup 1h"
3. Reason → "Morning free (9 AM-2 PM). Can do catchup 10-11 AM, report 1-5 PM"
4. Validate → "Feasible with buffer"
5. Suggest improvements → "Catchup before lunch (easier). Skip gym today (you went yesterday) or do light 30m workout?"

Agent response:
"Here's my suggested day:
09:00-10:00 AM: Deep work (prepare for report)
10:00-11:00 AM: Catchup with Alex
11:00 AM-01:00 PM: Lunch break
01:00-05:00 PM: Report (focused)
05:30-06:00 PM: Light workout (optional, saves 30m)
06:00-06:30 PM: Freshen up
08:00-09:30 PM: Dinner

Concerns: Tight schedule. Suggestion: Do full gym tomorrow instead?

OK?"

User: "Move catchup to 3 PM instead"

Agent:
"Conflict: Report ends 5 PM, catchup would be 3-4 PM.
That breaks report flow. Better: Catchup ends 5 PM, dinner at 8 PM?
Works if catchup location is close to dinner."

User: "Yes, same area"

Agent: "Perfect, calendar updated."
```

---

### Flow 3: "What If" Scenario

```
User:
"What if I move dinner to 7 PM instead? Will gym still work?"

Agent:
"No. Timeline:
- Report ends 5 PM
- Gym 6-7 PM
- Freshen up 7-7:30 PM
- Travel to dinner 7:30-7:50 PM
- Arrive at 7:50 PM (late by 10 min)

Options:
a) Gym 5:30-6:15 PM (shorter, rushed)
b) Skip gym
c) Move dinner to 8 PM (back to original)"

User: "Do gym 5:30-6:15"

Agent: "Done."
```

---

### Flow 4: "Add Task Then Replan"

```
User:
"Add: Pick up wine for birthday party at 7 PM"

Agent:
"Got it. New plan impact:
- Birthday 7 PM
- Wine shop closes 8 PM
- Wine shopping: 30 min (closes 8 PM)
- Your current calendar: Free from 4-7 PM

Suggestion: Wine 5:30-6 PM (leaves 1h buffer before party, shop closes 8 PM)
Update calendar?"

User: "Yes"

Agent: "Done. Added to calendar: Wine shopping 5:30-6 PM"
```

---

## 4. Agent Tools (Tool Definitions)

The agent will have access to these tools. It calls them based on reasoning.

### Tool 1: `get_calendar_events`
**Purpose:** Fetch user's Google Calendar events for a date/time range

```yaml
name: get_calendar_events
description: "Retrieve all calendar events for a user within a date/time range"
inputs:
  date_from: string (YYYY-MM-DD)
  date_to: string (YYYY-MM-DD)
  time_zone: string (default: "Europe/Stockholm")
outputs:
  - event_id: string
    title: string
    start_time: datetime (ISO 8601)
    end_time: datetime (ISO 8601)
    location: string (optional)
    duration_minutes: integer
```

---

### Tool 2: `get_tasks`
**Purpose:** Fetch open tasks from Google Tasks + Notion

```yaml
name: get_tasks
description: "Retrieve user's open tasks from Google Tasks and Notion"
inputs:
  status: string (enum: "open", "completed", "all")
  date_from: string (optional)
  tags: list[string] (optional, filter by tags)
outputs:
  - task_id: string
  - title: string
  - status: string
  - due_date: string (YYYY-MM-DD, optional)
  - estimated_duration_minutes: integer (optional)
  - priority: string (enum: "high", "medium", "low")
  - source: string (enum: "google_tasks", "notion")
```

---

### Tool 3: `get_user_memory`
**Purpose:** Retrieve the user's long-term memory — a free-form markdown document the user has written and the agent maintains, covering constraints, preferences, capabilities, and anything else worth remembering about how they live their day.

Unlike a rigid preferences schema, this is prose the user (and the agent) can write naturally — "I usually skip breakfast unless I'm going to the gym" reads and updates more naturally than a boolean field. The agent is expected to parse meaning from the text rather than expect fixed fields.

```yaml
name: get_user_memory
description: "Retrieve the user's long-term memory document (markdown) — preferences, constraints, and standing facts the agent has learned over time"
inputs:
  section: string (optional — e.g. "gym", "sleep"; omit to fetch the full document)
outputs:
  content: string (markdown)
  last_updated: datetime (ISO 8601)
```

---

### Tool 4: `update_user_memory`
**Purpose:** Persist something newly learned, corrected, or clarified about the user into the long-term memory document. This is what makes memory *long-term* rather than a static config file — the agent writes to it, not just reads it.

```yaml
name: update_user_memory
description: "Append or edit a section of the user's long-term memory markdown document"
inputs:
  section: string (e.g. "gym", "sleep", "meals" — matches a markdown heading; created if it doesn't exist)
  content: string (markdown prose to write)
  mode: string (enum: "append", "replace") — append adds to the section, replace overwrites it
  reason: string (short note on why this is being saved, e.g. "user said they now prefer evening workouts")
outputs:
  success: boolean
  section: string
  updated_at: datetime (ISO 8601)
```

**When the agent should call this:** whenever the user states a new fact, preference, correction, or standing constraint in conversation (e.g., "actually I hate mornings," "my new gym is closer, only 10 min away," "skip asking about breakfast, I never eat it"). The agent should save it and tell the user what it saved — the same way it would tell them it updated the calendar. It should *not* silently rewrite large chunks of memory without a clear signal that something changed.

---

### Tool 5: `validate_plan`
**Purpose:** Check if a proposed plan is feasible given constraints

```yaml
name: validate_plan
description: "Validate if a day plan is logically feasible (no overlaps, travel accounted for, respects preferences)"
inputs:
  plan: list
    - task: string
      start_time: string (HH:MM AM/PM)
      end_time: string (HH:MM AM/PM)
      location: string (optional)
      type: string (enum: "work", "gym", "meal", "travel", "personal")
  date: string (YYYY-MM-DD)
outputs:
  is_feasible: boolean
  conflicts: list
    - conflict_id: string
    - task_1: string
    - task_2: string
    - issue: string (e.g., "30 min overlap")
    - severity: string (enum: "critical", "warning", "info")
  travel_gaps: list (flagging inadequate travel time)
  constraint_violations: list (e.g., "Ends at 11 PM, target bedtime 11 PM")
  feasibility_score: float (0-100)
```

---

### Tool 6: `suggest_alternatives`
**Purpose:** Generate alternative plans when conflicts/constraints exist

```yaml
name: suggest_alternatives
description: "Generate alternative scheduling options to resolve conflicts"
inputs:
  current_plan: list (structured like validate_plan input)
  conflict_or_constraint: string (e.g., "Dinner at 8 PM conflicts with gym")
  flexibility: string (enum: "low", "medium", "high")
outputs:
  alternatives: list
    - option_id: integer (1, 2, 3...)
      description: string
      modified_tasks: list (show what changed)
      trade_offs: list (e.g., "Skips gym", "Dinner 30 min later")
      reasoning: string
      feasibility_score: float (0-100)
```

---

### Tool 7: `add_calendar_event`
**Purpose:** Add or update events on Google Calendar

```yaml
name: add_calendar_event
description: "Add or update an event on user's Google Calendar"
inputs:
  title: string
  start_time: string (ISO 8601)
  end_time: string (ISO 8601)
  location: string (optional)
  description: string (optional)
  update_existing: boolean (if true, update matching title instead of creating new)
outputs:
  event_id: string
  success: boolean
  calendar_url: string (link to view in Google Calendar)
```

---

### Tool 8: `update_task`
**Purpose:** Mark tasks complete or update status

```yaml
name: update_task
description: "Update task status (mark done, reschedule, etc.)"
inputs:
  task_id: string
  status: string (enum: "completed", "open", "deferred")
  new_due_date: string (YYYY-MM-DD, optional)
  source: string (enum: "google_tasks", "notion")
outputs:
  success: boolean
  task_id: string
  new_status: string
```

---

## 5. Agent Prompt (System Message)

```
You are an intelligent day planner assistant. Your role is to help the user 
plan their day by:

1. UNDERSTANDING: Gather their calendar, tasks, and long-term memory (preferences/constraints)
2. VALIDATING: Check if proposed plans are feasible (no time overlaps, 
   travel time accounted for, respects preferences)
3. REASONING: Identify conflicts and constraints
4. SUGGESTING: Propose realistic alternatives when conflicts exist
5. EXECUTING: Update calendar/tasks once user agrees
6. REMEMBERING: Save new facts, preferences, or corrections to long-term memory as they come up

KEY PRINCIPLES:
- Be conversational and helpful, not robotic
- Always explain your reasoning (e.g., "Dinner at 8 PM + gym 6-7 PM means 
  you'd be 10 min late")
- Ask clarifying questions if ambiguous (e.g., "Is dinner location near your gym?")
- Suggest trade-offs, not just say "no" (e.g., "Move dinner to 8:30 PM OR skip gym")
- Respect user preferences (e.g., if they hate early mornings, don't suggest 6 AM workouts)
- Be realistic about buffer time (travel, freshening up, unexpected delays)

CONSTRAINTS TO ALWAYS CHECK:
- Time overlaps (no two events at same time)
- Travel time between locations
- Sleep requirement (target bedtime, wake time)
- Work hours cap (max 9h per day)
- Meal times (breakfast, lunch, dinner)
- Preferences (gym timing, energy levels, etc.)

WHEN YOU FIND A CONFLICT:
1. Clearly state the conflict: "Dinner at 8 PM + 30 min travel = arrive 8:30 PM"
2. Show the impact: "You'd be late, or gym time is too tight"
3. Suggest options: "Move dinner to 8:30 PM, OR shorter gym 30 min, OR skip gym"
4. Let user choose: "What works best for you?"

WHEN VALIDATING A PLAN:
- Check feasibility_score >= 70 before suggesting
- Flag warnings (feasibility_score 50-70)
- Reject infeasible plans (feasibility_score < 50) with alternatives

MEMORY:
- At the start of a session, read the user's long-term memory (`get_user_memory`) — treat it as ground truth about how they live, not a form to re-ask every time
- When the user states a new fact, preference, or correction ("actually I hate early mornings", "my gym moved, it's 10 min away now"), save it with `update_user_memory` and briefly confirm what you saved ("Got it, noted you prefer evening workouts now")
- Don't ask the user to repeat information already in memory
- If memory is missing something you need (e.g., no gym duration on record), ask once, then save the answer so you never have to ask again

TONE: Friendly, efficient, practical. You're their personal assistant, not a robot.
```

---

## 6. Data Model

### User Long-Term Memory (Stored in Cloud Storage as Markdown)

This is no longer a static config file the agent reads once per request — it's a **living memory document**. The user writes to it in their own words (mostly free-text explanations, not fields), and the agent appends or edits sections over time as it learns things in conversation (via `update_user_memory`). Think of it as the agent's evolving understanding of the user, not a form the user filled out once.

A short YAML frontmatter block holds the handful of values that are genuinely machine identifiers (not something a user would ever phrase as prose) — timezone and integration IDs. Everything else is markdown headings and prose, one heading per `section` used by `get_user_memory` / `update_user_memory`.

```markdown
---
# /users/{user_id}/memory.md
timezone: "Europe/Stockholm"
google_calendar_id: "your-email@gmail.com"
google_tasks_list_id: "abc123"
notion_database_id: "xyz789"
slack_user_id: "U123456"
last_updated: "2026-07-20"
---

# About Me

I'm based in Stockholm. I work a fairly standard office job and try to keep
work capped around 9 hours a day — I burn out fast if it creeps later than
6 PM regularly.

## Gym

I go to Planet Fitness on Drottninggatan 89. A full session is about 45
minutes. I much prefer evening workouts (6-7 PM ish) — I am not a morning
person and asking me to work out before 8 AM basically guarantees I skip it.
I can't work out during work hours, my calendar is too packed for that.

*(updated 2026-06-02 — I used to do mornings, but that never actually
stuck, so don't suggest it anymore.)*

## Commute

Office is at Stureplan 2, home is in Södermalm. Roughly: office → home is
30 min, home → gym is 15 min, gym → office is 25 min, office → gym is 20 min.

## Sleep

I need a solid 8 hours. I try to be in bed by 11 PM and up by 6:30 AM at the
latest. This is a hard constraint for me — don't suggest plans that push
bedtime past 11 unless I explicitly say it's a one-off.

## Work

Max 9 hours a day, ideally 8 AM to 6 PM. I do my best focused work in
2-hour blocks and need at least 15 minutes between them or I get scattered.

## Meals

I eat breakfast around 7:30 (30 min), lunch at noon (an hour), and dinner
around 8 PM (90 min) — dinner time is the one I'm most willing to move
around if something conflicts.

## Energy

I hit a wall after lunch, roughly 2-3 PM — don't schedule anything that
needs real focus then. Same in the evening around 9-10 PM, I'm winding
down. Mornings (8 AM-noon) and late afternoon (4-6 PM) are when I actually
have energy to do hard things.

## Upcoming Events

- Maria's Birthday — 2026-08-14, 7 PM, at [Restaurant Name]. Need to
  remember to buy wine beforehand.
```

**Why markdown instead of YAML here:** the content is mostly explanation and nuance ("I *used to* do mornings, but it never stuck") that reads naturally as prose and awkwardly as strict fields. The agent doesn't need a rigid schema to reason over this — an LLM parses "I much prefer evening workouts" just fine without a `preferred_times` array. Keeping only true identifiers in frontmatter avoids forcing everything into structure that doesn't need it, while still giving the agent a fast, unambiguous way to look up IDs.

**Versioning:** enable object versioning on the storage bucket (Cloud Storage / S3) so agent-written edits are never destructive — you can always diff or roll back to a prior version of `memory.md` if the agent misreads something.

**Two layers of memory:** `memory.md` remains the human-curated, explicit memory — things the user deliberately wrote or told the agent to save via `update_user_memory`. Alongside it, the managed platform's own long-term memory (Vertex AI Agent Engine Memory Bank / Bedrock Agents built-in memory) captures *implicit* facts extracted automatically from conversation, without a human or the agent explicitly deciding "this belongs in memory.md." The agent should treat `memory.md` as authoritative when the two disagree, since it's the version the user can actually read and edit.

**Multi-tenant path:** the `/users/{user_id}/memory.md` layout was already per-user, which is what makes multi-tenant isolation straightforward — each user's memory document, session history, and extracted long-term memory are all scoped by the same `user_id`, so one deployed agent can safely serve more than one person (e.g., Slack workspace members) without their plans or preferences leaking into each other's context.

---

### Chat Session Memory (Temporary, per conversation)

```json
{
  "session_id": "session_123",
  "user_id": "user_456",
  "started_at": "2024-12-19T07:00:00Z",
  "messages": [
    {
      "role": "user",
      "content": "I have a report due 5 PM, gym 6-7 PM, dinner 8 PM. Does this work?",
      "timestamp": "2024-12-19T07:05:00Z"
    },
    {
      "role": "assistant",
      "content": "Let me check... Report ends 5 PM, travel 20 min, gym 6-7 PM. That's tight. Dinner at 8 PM means 30 min to freshen up. Feasible but rushed.",
      "timestamp": "2024-12-19T07:05:30Z",
      "tools_called": ["get_calendar_events", "get_user_memory", "validate_plan"]
    }
  ],
  "pending_actions": [
    {
      "type": "add_calendar_event",
      "title": "Gym",
      "start": "2024-12-19T18:00:00",
      "end": "2024-12-19T19:00:00"
    }
  ]
}
```

---

## 7. Triggers

### Trigger 1: Slack Message (Chat Initiation)
- **When:** User sends message in Slack channel/DM with bot
- **Channels:** Private DM with agent OR designated `#day-planner` channel
- **Format:** Natural language (no special commands needed)
- **Example:** "Can I fit gym and dinner today?"

### Trigger 2: Morning Briefing (Time-based)
- **When:** 7:00 AM Stockholm time, Monday-Friday
- **Action:** Agent automatically plans the day
- **Delivery:** Slack message OR email
- **Example:** "Good morning! Here's your optimized day plan..."

### Trigger 3: New Calendar Event (Webhook)
- **When:** User adds event to Google Calendar
- **Action:** Agent validates impact on plan, flags conflicts
- **Delivery:** Slack notification
- **Example:** "⚠️ Heads up: New meeting 6-7 PM conflicts with gym. Want alternatives?"

### Trigger 4: New Task Added (Webhook)
- **When:** User adds task to Google Tasks or Notion
- **Action:** Agent checks if task can fit into today's plan
- **Delivery:** Slack notification
- **Example:** "Task added: 'Buy wine'. Can fit 5:30-6 PM before dinner?"

### Trigger 5: Manual Command (Optional, Phase 2)
- **When:** User types `/plan-my-day` or similar Slack command
- **Action:** Instant day planning without chat
- **Delivery:** Slack message with full plan

---

## 8. Technical Specifications

### Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        User Interface                        │
│  (Slack Bot / Web Dashboard / Email)                         │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│               Cloud Run / Lambda (thin adapter)               │
│  - Slack bot handler (signature verification, formatting)    │
│  - Webhook handlers (calendar, tasks)                         │
│  - Translates Slack events into agent invocations             │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│      Managed Agent Platform (Vertex AI Agent Engine /        │
│                    AWS Bedrock Agents)                        │
│  - Reasoning engine + tool orchestration                      │
│  - Plan validation & suggestions                               │
│  - Hosted, versioned session store (per user_id + session_id) │
│  - Long-term memory (auto-extracted facts, per user)          │
│  - RAG / grounding over the memory document + future docs     │
└────────────┬──────────────────────────────┬────────────────┘
             │                              │
      ┌──────▼──────┐              ┌────────▼────────┐
      │   Google    │              │   Cloud Storage /│
      │  APIs       │              │   S3              │
      │  - Calendar │              │  (memory.md,      │
      │  - Tasks    │              │   versioned,      │
      │             │              │   synced into RAG)│
      └─────────────┘              └─────────────────┘
```

The platform box now does more than "reasoning + tool calling": it also owns session storage, long-term memory, and retrieval — capabilities that used to be hand-rolled (Firestore/DynamoDB sessions, a single flat memory file read in full every time). See the per-cloud deployment guides for exactly which managed services provide each of these.

### Platform: GCP Vertex AI Agents (Agent Engine)

**Why Vertex AI Agent Engine specifically (not just "Vertex AI"):**
- Native integration with Google APIs (Calendar, Tasks)
- Strong reasoning capability out of the box, tool calling via the Agent Development Kit (ADK)
- Managed service (no server maintenance)
- **Agent Engine Sessions** — hosted, versioned conversation store instead of hand-rolled Firestore documents
- **Agent Engine Memory Bank** — managed long-term memory, auto-extracts durable facts from conversations per user
- **Vertex AI Search / RAG Engine** — grounds the agent on `memory.md` (and future, larger docs) instead of stuffing the whole file into every prompt
- Together these also give clean **multi-tenant isolation**: sessions and memory are scoped per `user_id`, so more than one person can use the same deployed agent without cross-talk

This costs more than calling the Gemini API directly from a bare Cloud Run service — you're now paying for managed session/memory/retrieval infrastructure. See [deployment-gcp.md](deployment-gcp.md) for the cost breakdown and trade-off.

**Alternative: AWS Bedrock Agents**
- Same three capabilities via different managed services (Bedrock Agents Sessions, Bedrock Agents built-in memory, Bedrock Knowledge Bases)
- Requires more glue code for Google integrations (Calendar/Tasks aren't natively wired in the way they are on GCP)
- See [deployment-aws.md](deployment-aws.md) for the AWS-equivalent architecture and cost breakdown

### Tech Stack

```
Frontend:
  - Slack Bot (primary chat interface)
  - Thin webhook adapter (Cloud Run / Lambda)

Backend:
  - Vertex AI Agent Engine (GCP) or Bedrock Agents (AWS) — reasoning + orchestration
  - Agent Engine Sessions / Bedrock Agents Sessions — hosted conversation state
  - Agent Engine Memory Bank / Bedrock Agents memory — long-term per-user memory
  - Vertex AI Search / RAG Engine (GCP) or Bedrock Knowledge Bases (AWS) — RAG grounding

APIs:
  - Google Calendar API
  - Google Tasks API
  - Notion API
  - Slack Bot API

Storage:
  - Cloud Storage / S3 (memory.md — source of truth, versioned, synced into the RAG index)
  - Secret Manager / SSM Parameter Store (API keys)

Orchestration:
  - Cloud Scheduler / EventBridge Scheduler (morning briefing trigger)

Deployment:
  - Terraform/Pulumi (IaC)
  - GitHub Actions (CI/CD)
```

### Vertex AI Agent Engine Configuration

```yaml
agent_id: "day-planner-agent"
model: "gemini-2.0-flash-exp" # or latest available
project_id: "your-gcp-project"

sessions:
  provider: "agent_engine_sessions"  # hosted, versioned, scoped per user_id + session_id

memory:
  provider: "agent_engine_memory_bank"  # long-term, auto-extracted, scoped per user_id

grounding:
  provider: "vertex_ai_search"  # or rag_engine
  data_store: "day-planner-memory-corpus"
  source: "gs://day-planner-memory/{user_id}/memory.md"
  sync: "incremental"  # re-index on Cloud Storage change

tools:
  - id: "get_calendar_events"
    type: "custom_function"
    description: "Fetch Google Calendar events"
  
  - id: "get_tasks"
    type: "custom_function"
    description: "Fetch Google Tasks and Notion tasks"
  
  - id: "get_user_memory"
    type: "custom_function"
    description: "Get user's long-term memory (markdown): preferences and constraints"

  - id: "update_user_memory"
    type: "custom_function"
    description: "Save a new fact, preference, or correction to the user's long-term memory"
  
  - id: "validate_plan"
    type: "custom_function"
    description: "Validate plan feasibility"
  
  - id: "suggest_alternatives"
    type: "custom_function"
    description: "Generate alternative plans"
  
  - id: "add_calendar_event"
    type: "google_calendar_api"
    description: "Add/update Google Calendar event"
  
  - id: "update_task"
    type: "custom_function"
    description: "Update task status"

system_prompt: |
  You are an intelligent day planner assistant...
  [Full prompt as specified in Section 5]

instructions:
  - Always validate plans before suggesting
  - Provide reasoning for all suggestions
  - Ask clarifying questions when needed
  - Be conversational and helpful
```

---

## 9. Success Criteria (MVP)

### Functional Requirements
- [ ] User can chat with agent in Slack
- [ ] Agent successfully fetches Google Calendar events
- [ ] Agent successfully fetches Google Tasks
- [ ] Agent validates plans and identifies conflicts
- [ ] Agent suggests alternatives with trade-offs
- [ ] Agent can add events to Google Calendar
- [ ] Agent respects user preferences (gym timing, sleep, work hours)

### Non-Functional Requirements
- [ ] Agent responds within 5 seconds (including API calls)
- [ ] 95% uptime for production
- [ ] Secure API key management (no hardcoding)
- [ ] Clear error messages to user ("Failed to fetch calendar. Try again?")

### Learning Objectives
- [ ] Understand Vertex AI Agents tool calling
- [ ] Implement stateful conversation with agent
- [ ] Integrate multiple Google APIs
- [ ] Deploy serverless function on Cloud Run

### Success Definition (Personal)
- Use the agent 5+ mornings a week
- No "I forgot to fit X in" situations
- Successfully plan at least 3 multi-activity days

---

## 10. Implementation Timeline

### Week 1: Foundation
- **Days 1-2:** GCP setup, Vertex AI Agent creation
- **Days 3-4:** Google Calendar + Tasks API integration
- **Days 5-7:** Basic Slack bot + agent chat loop

### Week 2: Reasoning & Logic
- **Days 1-2:** Implement `validate_plan` logic
- **Days 3-4:** Implement `suggest_alternatives` logic
- **Days 5-7:** Long-term memory doc (markdown) + testing

### Week 3: Polish & Deployment
- **Days 1-2:** Error handling, edge cases
- **Days 3-4:** Slack formatting, UX improvements
- **Days 5-7:** Deploy to production, monitor

---

## 11. Future Enhancements (Phase 2+)

1. **Financial Awareness**
   - Integrate Revolut API for spending data
   - Budget-aware suggestions ("You're at budget limit, skip expensive restaurant?")

2. **Nordea/BankID Integration**
   - Pull account balance, recent transactions
   - Financial recommendations

3. **Learning Suggestions**
   - Analyze schedule gaps and interest
   - Suggest tech news, tutorials to read during breaks
   - "You have 1h free 10-11 AM. Read this DevOps article?"

4. **Multi-Day Planning**
   - "Plan my week"
   - "Schedule gym 3x this week"

5. **Habit Tracking**
   - "You skipped gym 3 days last week. Want to commit to 2x this week?"

6. **Social Integration**
   - Suggest activities with friends based on schedule
   - "Alex free Thursday 6 PM. Coffee?"

7. **Morning Briefing Email**
   - Daily email with plan + tech news recommendations
   - Alternative to Slack

---

## 12. Questions & Decisions

### Decisions Made
- [x] Platform: GCP Vertex AI Agents
- [x] Chat interface: Slack (primary)
- [x] Storage: Cloud Storage (long-term memory, markdown, versioned)
- [x] Deployment: Cloud Run + Cloud Scheduler

### Decisions Pending
- [ ] Include morning briefing email, or just Slack?
- [ ] Add Revolut integration in MVP, or Phase 2?
- [ ] Add Nordea/BankID in MVP, or Phase 2?
- [ ] Web dashboard as alternative UI, or Slack-only?

### Known Constraints
- BankID required for Nordea (adds complexity, defer to Phase 2)
- Revolut API has rate limits (handle gracefully)
- Google Calendar has sharing/permission complexity (start with personal calendar)

---

## 13. Sample Prompts for Testing Agent

Once deployed, test with these scenarios:

1. "I have a 2-hour report due at 5 PM, want to gym 6-7 PM, dinner at 8 PM. Works?"
2. "Add: Buy wine for party at 7 PM today. When can I do it?"
3. "What if my meeting runs late until 5:30 PM? Can I still make dinner at 8?"
4. "Plan my day: Report, gym, lunch, catchup with Alex, dinner"
5. "I'm exhausted. Should I skip gym today or shorten it?"
6. "Can I fit a 1-hour call before lunch?"

---

## 14. Deployment Checklist

- [ ] GCP project created
- [ ] Vertex AI API enabled
- [ ] Google Calendar API enabled + OAuth configured
- [ ] Google Tasks API enabled
- [ ] Slack app created + bot token generated
- [ ] Cloud Run service created (thin Slack adapter)
- [ ] Cloud Storage bucket for long-term memory (versioning enabled)
- [ ] Vertex AI Agent Engine deployed (agent + tools via ADK)
- [ ] Agent Engine Sessions enabled (hosted conversation store)
- [ ] Agent Engine Memory Bank enabled (long-term per-user memory)
- [ ] Vertex AI Search / RAG Engine data store created, synced to the memory bucket
- [ ] Cloud Scheduler job for morning briefing (optional)
- [ ] Secrets Manager configured (API keys)
- [ ] Terraform/Pulumi infrastructure as code
- [ ] GitHub repo with CI/CD pipeline
- [ ] Monitoring (Cloud Logging, Error Reporting)
- [ ] Disaster recovery plan

---

**Document Version:** 1.0  
**Last Updated:** 2024-12-19  
**Next Review:** After MVP completion
