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

### Tool 3: `get_user_preferences`
**Purpose:** Get user's constraints, preferences, and capabilities

```yaml
name: get_user_preferences
description: "Retrieve user's stored preferences for day planning"
inputs: {}
outputs:
  gym:
    duration_minutes: integer (e.g., 45)
    preferred_times: list[string] (e.g., ["6-7 PM", "7-8 PM"])
    can_do_early: boolean
    location: string
    address: string (for travel time calculation)
  commute:
    office_to_home_minutes: integer
    office_to_gym_minutes: integer
    gym_to_home_minutes: integer
    office_location: string
  sleep:
    target_hours: integer (e.g., 8)
    latest_bedtime: string (time, e.g., "11 PM")
    earliest_wake: string (time, e.g., "6:30 AM")
  work:
    max_hours_per_day: integer
    unavailable_after: string (time, e.g., "6 PM")
    focus_block_duration: integer (minutes)
  meals:
    breakfast: boolean
    lunch_duration_minutes: integer
    dinner_preferred_time: string
    dinner_duration_minutes: integer
  energy:
    low_energy_times: list[string] (e.g., ["2-3 PM", "8-9 PM"])
    high_energy_times: list[string]
```

---

### Tool 4: `validate_plan`
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

### Tool 5: `suggest_alternatives`
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

### Tool 6: `add_calendar_event`
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

### Tool 7: `update_task`
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

1. UNDERSTANDING: Gather their calendar, tasks, and constraints
2. VALIDATING: Check if proposed plans are feasible (no time overlaps, 
   travel time accounted for, respects preferences)
3. REASONING: Identify conflicts and constraints
4. SUGGESTING: Propose realistic alternatives when conflicts exist
5. EXECUTING: Update calendar/tasks once user agrees

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

TONE: Friendly, efficient, practical. You're their personal assistant, not a robot.
```

---

## 6. Data Model

### User Preferences (Stored in Cloud Storage YAML)

```yaml
# /users/{user_id}/preferences.yaml

user:
  name: "You"
  timezone: "Europe/Stockholm"
  google_calendar_id: "your-email@gmail.com"
  google_tasks_list_id: "abc123"
  notion_database_id: "xyz789"
  slack_user_id: "U123456"

gym:
  duration_minutes: 45
  preferred_times:
    - start: "06:00"
      end: "07:00"
    - start: "18:00"
      end: "19:00"
  can_do_early: false
  can_do_during_work: false
  location: "Planet Fitness Stockholm"
  address: "Drottninggatan 89, Stockholm"

commute:
  work_location: "Office, Stureplan 2, Stockholm"
  home_location: "Home, Södermalm, Stockholm"
  work_to_home_minutes: 30
  home_to_gym_minutes: 15
  gym_to_work_minutes: 25
  work_to_gym_minutes: 20

sleep:
  target_hours: 8
  earliest_wake_time: "06:30"
  latest_bedtime: "23:00"
  strict: true  # Hard constraint

work:
  max_hours_per_day: 9
  earliest_start: "08:00"
  latest_end: "18:00"
  focus_block_duration_minutes: 120
  min_break_between_blocks: 15

meals:
  breakfast:
    eat: true
    time: "07:30"
    duration_minutes: 30
  lunch:
    eat: true
    time: "12:00"
    duration_minutes: 60
  dinner:
    eat: true
    preferred_time: "20:00"
    duration_minutes: 90
    can_be_flexible: true

energy:
  low_energy_times:
    - "14:00-15:00"  # Post-lunch slump
    - "21:00-22:00"  # Evening fatigue
  high_energy_times:
    - "08:00-12:00"  # Morning peak
    - "16:00-18:00"  # Afternoon recovery

birthdays_and_events:
  - name: "Maria's Birthday"
    date: "2024-12-20"
    time: "19:00"
    location: "Restaurant Name"
    notes: "Need to buy wine"
```

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
      "tools_called": ["get_calendar_events", "get_user_preferences", "validate_plan"]
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
│               Cloud Run / App Engine                         │
│  (FastAPI / Node.js server)                                  │
│  - Slack bot handler                                         │
│  - Webhook handlers (calendar, tasks)                        │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│             Vertex AI Agent (or Bedrock)                    │
│  - Reasoning engine                                          │
│  - Tool orchestration                                        │
│  - Plan validation & suggestions                             │
└────────────┬──────────────────────────────┬────────────────┘
             │                              │
      ┌──────▼──────┐              ┌────────▼────────┐
      │   Google    │              │   Cloud         │
      │  APIs       │              │   Storage       │
      │  - Calendar │              │  (Preferences,  │
      │  - Tasks    │              │   Session mem)  │
      └─────────────┘              └─────────────────┘
```

### Platform: GCP Vertex AI Agents

**Why Vertex AI Agents:**
- Native integration with Google APIs (Calendar, Tasks)
- Strong reasoning capability out of the box
- Supports tool calling (function definitions)
- Managed service (no server maintenance)
- ~$0.50-1.00 per day for light usage

**Alternative: AWS Bedrock Agents**
- Similar reasoning, slightly different API
- Works if you prefer AWS ecosystem
- Requires more glue code for Google integrations

### Tech Stack

```
Frontend:
  - Slack Bot (primary chat interface)
  - FastAPI/Express (webhook handler)
  
Backend:
  - Python or Node.js
  - Cloud Run (serverless)
  - Vertex AI Agents (reasoning engine)
  
APIs:
  - Google Calendar API
  - Google Tasks API
  - Notion API
  - Slack Bot API
  
Storage:
  - Cloud Storage (YAML preferences)
  - Firestore (session memory, optional)
  - Secrets Manager (API keys)
  
Orchestration:
  - Cloud Scheduler (morning briefing trigger)
  - Cloud Tasks (deferred actions)
  - Pub/Sub (async task processing, optional)

Deployment:
  - Terraform/Pulumi (IaC)
  - GitHub Actions (CI/CD)
```

### Vertex AI Agent Configuration

```yaml
agent_id: "day-planner-agent"
model: "gemini-2.0-flash-exp" # or latest available
project_id: "your-gcp-project"

tools:
  - id: "get_calendar_events"
    type: "custom_function"
    description: "Fetch Google Calendar events"
  
  - id: "get_tasks"
    type: "custom_function"
    description: "Fetch Google Tasks and Notion tasks"
  
  - id: "get_user_preferences"
    type: "custom_function"
    description: "Get user preferences and constraints"
  
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
- **Days 5-7:** Preferences YAML + testing

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
- [x] Storage: Cloud Storage (preferences YAML)
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
- [ ] Cloud Run service created
- [ ] Cloud Storage bucket for preferences
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
