-- Queries against turn_log.py's turn-record telemetry (A1.1/A1.2, loop
-- detection A1.3, habit session outcomes A1.4). Kept alongside
-- turn_log.py (the thing that produces the rows these query) rather than
-- reinvented per question — see docs/roadmaps/1-agent.md.
--
-- Table placeholder: `{{PROJECT}}.day_planner_turns.{{TABLE}}`
--
-- The dataset id (day_planner_turns) is fixed by terraform/bigquery.tf.
-- The *table* name is not something Terraform assigns directly — Cloud
-- Logging's BigQuery sink derives it from the exported log's name
-- (expected to be run_googleapis_com_stdout, since every turn_log.py line
-- is written to the app service's stdout), and the table itself is only
-- created lazily on the sink's first matching delivery, not at apply time.
-- Confirm the actual name in the BigQuery console (or `bq ls
-- day_planner_turns`) once a turn has actually been logged, and replace
-- {{TABLE}} below — do not assume the name without checking.
--
-- Every row is one turn_log.py record: LogEntry's own `timestamp` column
-- for when the turn happened, `jsonPayload.*` for the turn record's own
-- fields (turn_id, session_id, user_ref, tool_calls[] — each with name,
-- args_fingerprint, duration_ms, status — model_calls, input_tokens,
-- output_tokens, thinking_tokens, preload_ok, outcome, wall_ms,
-- loop_detected, habit_session_outcomes[] — each with habit_id,
-- session_status, outcome, hour_of_day, day_of_week, zone_constrained,
-- source — see turn_log.py's TurnRecorder.emit).

-- 1. Tokens per turn (input / output / thinking), and the distribution.
SELECT
  APPROX_QUANTILES(jsonPayload.input_tokens, 100)[OFFSET(50)] AS input_p50,
  APPROX_QUANTILES(jsonPayload.input_tokens, 100)[OFFSET(95)] AS input_p95,
  APPROX_QUANTILES(jsonPayload.output_tokens, 100)[OFFSET(50)] AS output_p50,
  APPROX_QUANTILES(jsonPayload.output_tokens, 100)[OFFSET(95)] AS output_p95,
  APPROX_QUANTILES(jsonPayload.thinking_tokens, 100)[OFFSET(50)] AS thinking_p50,
  APPROX_QUANTILES(jsonPayload.thinking_tokens, 100)[OFFSET(95)] AS thinking_p95,
  COUNT(*) AS turns
FROM `{{PROJECT}}.day_planner_turns.{{TABLE}}`
WHERE timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY);

-- 2. Turn latency p50 / p95 / p99.
SELECT
  APPROX_QUANTILES(jsonPayload.wall_ms, 100)[OFFSET(50)] AS wall_ms_p50,
  APPROX_QUANTILES(jsonPayload.wall_ms, 100)[OFFSET(95)] AS wall_ms_p95,
  APPROX_QUANTILES(jsonPayload.wall_ms, 100)[OFFSET(99)] AS wall_ms_p99,
  COUNT(*) AS turns
FROM `{{PROJECT}}.day_planner_turns.{{TABLE}}`
WHERE timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY);

-- 3. Tool calls per turn, as a distribution — the long tail is where
-- loops live. A single turn issuing the same tool with identical
-- arguments 3+ times is A1.3's loop-detector signal; this is the
-- historical view of the same shape.
SELECT
  calls_in_turn,
  COUNT(*) AS turns_with_this_many_calls
FROM (
  SELECT
    jsonPayload.turn_id,
    ARRAY_LENGTH(jsonPayload.tool_calls) AS calls_in_turn
  FROM `{{PROJECT}}.day_planner_turns.{{TABLE}}`
  WHERE timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
)
GROUP BY calls_in_turn
ORDER BY calls_in_turn;

-- 4. Tool error rate, split by tool name and by returned status. "error"
-- here means the tool's own status contract, not a Python exception —
-- every tool in this codebase returns {"status": ...}, and turn_log.py
-- only ever keeps that field from a tool's response (see turn_log.py's
-- module docstring on why response payloads are never logged).
--
-- Two steps because a window function can't safely nest COUNTIF over a
-- GROUP BY that's already collapsed status into one row per group — that
-- would count distinct status *groups*, not occurrences weighted by how
-- often each one actually happened.
WITH per_status AS (
  SELECT
    tool_call.name,
    tool_call.status,
    COUNT(*) AS occurrences
  FROM `{{PROJECT}}.day_planner_turns.{{TABLE}}`,
    UNNEST(jsonPayload.tool_calls) AS tool_call
  WHERE timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  GROUP BY tool_call.name, tool_call.status
)
SELECT
  name,
  status,
  occurrences,
  SAFE_DIVIDE(
    SUM(IF(status = 'error', occurrences, 0)) OVER (PARTITION BY name),
    SUM(occurrences) OVER (PARTITION BY name)
  ) AS error_rate_for_tool
FROM per_status
ORDER BY name, occurrences DESC;

-- 5. needs_auth rate — a product signal (users who haven't connected a
-- calendar, or whose connection went stale) disguised as a technical one.
SELECT
  COUNTIF(tool_call.status = 'needs_auth') AS needs_auth_calls,
  COUNT(*) AS total_tool_calls,
  SAFE_DIVIDE(COUNTIF(tool_call.status = 'needs_auth'), COUNT(*)) AS needs_auth_rate
FROM `{{PROJECT}}.day_planner_turns.{{TABLE}}`,
  UNNEST(jsonPayload.tool_calls) AS tool_call
WHERE timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY);

-- 6. Locate one turn by turn_id and inspect it fully — e.g. from a user
-- report ("it booked my gym during work hours") once you have the
-- turn_id, or from spotting an outlier in one of the queries above.
SELECT *
FROM `{{PROJECT}}.day_planner_turns.{{TABLE}}`
WHERE jsonPayload.turn_id = @turn_id;

-- =============================================================================
-- A1.4 — completion rate, survival rate, restatement/abandonment signals.
--
-- COVERAGE CAVEAT — read this before trusting either rate below as a
-- fleet-wide figure. Both are opt-in biased, in different ways:
--   * Completion rate only observes sessions someone actually marked
--     (mark_habit_session, either the agent on the user's say-so or a
--     future UI). A session nobody ever mentioned again stays "pending"
--     forever — it is NOT evidence of failure, and this data is silent
--     about it, not negative about it.
--   * Survival rate is only computed for sessions that happened to fall
--     inside a review_habit_week call's date range, for a user who
--     triggered one. Users who never ask "how'd my week go" contribute
--     zero survival rows, not zero (or full) survival.
-- Every query below reports its denominator for exactly this reason — a
-- rate with no visible sample size invites being read as more
-- representative than it is. jsonPayload.habit_session_outcomes[].source
-- is "organic" for every row today (an agent- or user-triggered
-- review_habit_week call); Roadmap 2's B2.2 will start writing "push"
-- into the same schema once change-detection lands — filter or group on
-- source once both exist so the two don't get silently averaged together.
-- =============================================================================

-- 7a. Completion rate by habit. session_status is one of "pending",
-- "completed", "skipped" — pending gets its own column here, never
-- folded into skipped/failure. completion_rate's denominator is every
-- placed session with a recorded outcome, not just completed+skipped, so
-- a habit nobody has marked yet correctly shows a low rate with a
-- visible pending_count explaining why, rather than looking abandoned.
SELECT
  hso.habit_id,
  COUNTIF(hso.session_status = 'completed') AS completed_count,
  COUNTIF(hso.session_status = 'skipped') AS skipped_count,
  COUNTIF(hso.session_status = 'pending') AS pending_count,
  COUNT(*) AS total_sessions,
  SAFE_DIVIDE(COUNTIF(hso.session_status = 'completed'), COUNT(*)) AS completion_rate
FROM `{{PROJECT}}.day_planner_turns.{{TABLE}}`,
  UNNEST(jsonPayload.habit_session_outcomes) AS hso
WHERE timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
GROUP BY hso.habit_id
ORDER BY total_sessions DESC;

-- 7b. Completion rate by hour of day (0-23, the session's planned local
-- start hour) — where in the day placements actually get done.
SELECT
  hso.hour_of_day,
  COUNTIF(hso.session_status = 'completed') AS completed_count,
  COUNTIF(hso.session_status = 'skipped') AS skipped_count,
  COUNTIF(hso.session_status = 'pending') AS pending_count,
  COUNT(*) AS total_sessions,
  SAFE_DIVIDE(COUNTIF(hso.session_status = 'completed'), COUNT(*)) AS completion_rate
FROM `{{PROJECT}}.day_planner_turns.{{TABLE}}`,
  UNNEST(jsonPayload.habit_session_outcomes) AS hso
WHERE timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
GROUP BY hso.hour_of_day
ORDER BY hso.hour_of_day;

-- 7c. Completion rate by day of week ("mon".."sun", the session's
-- planned local day).
SELECT
  hso.day_of_week,
  COUNTIF(hso.session_status = 'completed') AS completed_count,
  COUNTIF(hso.session_status = 'skipped') AS skipped_count,
  COUNTIF(hso.session_status = 'pending') AS pending_count,
  COUNT(*) AS total_sessions,
  SAFE_DIVIDE(COUNTIF(hso.session_status = 'completed'), COUNT(*)) AS completion_rate
FROM `{{PROJECT}}.day_planner_turns.{{TABLE}}`,
  UNNEST(jsonPayload.habit_session_outcomes) AS hso
WHERE timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
GROUP BY hso.day_of_week
ORDER BY
  CASE hso.day_of_week
    WHEN 'mon' THEN 1 WHEN 'tue' THEN 2 WHEN 'wed' THEN 3 WHEN 'thu' THEN 4
    WHEN 'fri' THEN 5 WHEN 'sat' THEN 6 WHEN 'sun' THEN 7
  END;

-- 7d. Completion rate by whether a zone constrained the placement
-- (habit_tools.py's _zone_constrains — a diagnostic overlap check
-- against the zones preloaded for that session, not a hard-constraint
-- re-verification). Tests whether being squeezed into open time around a
-- zone predicts follow-through any differently than unconstrained time.
SELECT
  hso.zone_constrained,
  COUNTIF(hso.session_status = 'completed') AS completed_count,
  COUNTIF(hso.session_status = 'skipped') AS skipped_count,
  COUNTIF(hso.session_status = 'pending') AS pending_count,
  COUNT(*) AS total_sessions,
  SAFE_DIVIDE(COUNTIF(hso.session_status = 'completed'), COUNT(*)) AS completion_rate
FROM `{{PROJECT}}.day_planner_turns.{{TABLE}}`,
  UNNEST(jsonPayload.habit_session_outcomes) AS hso
WHERE timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
GROUP BY hso.zone_constrained;

-- 8. Survival rate — DIAGNOSTIC ONLY, never a success measure (see the
-- coverage caveat above). Share of sessions still present and unmoved
-- (outcome = "kept") — useful for explaining *why* a session wasn't
-- completed (bumped, moved, dropped), not for claiming it was: a "kept"
-- session that's still "pending" only means nobody deleted it, and a
-- "moved" + "completed" session is a full success despite a survival
-- rate of zero for that row. Use query 7 for the actual success metric.
SELECT
  COUNTIF(hso.outcome = 'kept') AS kept_count,
  COUNTIF(hso.outcome = 'moved') AS moved_count,
  COUNTIF(hso.outcome = 'gone') AS gone_count,
  COUNT(*) AS total_sessions,
  SAFE_DIVIDE(COUNTIF(hso.outcome = 'kept'), COUNT(*)) AS survival_rate_diagnostic_only
FROM `{{PROJECT}}.day_planner_turns.{{TABLE}}`,
  UNNEST(jsonPayload.habit_session_outcomes) AS hso
WHERE timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY);

-- 9. Restatement/correction signal — the user immediately re-does or
-- corrects something in the very next turn, a negative on comprehension
-- rather than on placement. Approximated from data already collected
-- since A1.1/A1.3: the same tool name + args_fingerprint (A1.3's one-way
-- argument hash — see turn_log.py) called again in the session's very
-- next turn, within a short window. No new instrumentation.
--
-- turn_order and turn_calls are deliberately separate CTEs: turn_order
-- has to be one row per *turn* for ROW_NUMBER() to number turns in
-- sequence, but computing it over UNNEST(tool_calls) directly (multiple
-- rows per turn when a turn makes >1 call) would number tool calls
-- instead, silently corrupting "the next turn" into "the next call,
-- maybe from the same turn." Splitting them out and joining by turn_id
-- keeps the two concerns apart.
WITH turn_order AS (
  SELECT
    jsonPayload.session_id AS session_id,
    jsonPayload.turn_id AS turn_id,
    timestamp,
    ROW_NUMBER() OVER (PARTITION BY jsonPayload.session_id ORDER BY timestamp) AS turn_seq
  FROM `{{PROJECT}}.day_planner_turns.{{TABLE}}`
  WHERE timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
),
turn_calls AS (
  SELECT
    jsonPayload.turn_id AS turn_id,
    tool_call.name,
    tool_call.args_fingerprint
  FROM `{{PROJECT}}.day_planner_turns.{{TABLE}}`,
    UNNEST(jsonPayload.tool_calls) AS tool_call
  WHERE timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
)
SELECT
  a.session_id,
  a.turn_id AS first_turn_id,
  b.turn_id AS restating_turn_id,
  ac.name AS repeated_tool,
  TIMESTAMP_DIFF(b.timestamp, a.timestamp, SECOND) AS seconds_between_turns
FROM turn_order a
JOIN turn_order b
  ON a.session_id = b.session_id AND b.turn_seq = a.turn_seq + 1
JOIN turn_calls ac ON ac.turn_id = a.turn_id
JOIN turn_calls bc
  ON bc.turn_id = b.turn_id
  AND bc.name = ac.name
  AND bc.args_fingerprint = ac.args_fingerprint
-- "Immediately" — a repeat call hours later is a legitimate new request,
-- not a correction of the last one. Tune this window against real data
-- before trusting it (see A3.4's guidance on grounding thresholds in
-- observed behaviour rather than a guess).
WHERE TIMESTAMP_DIFF(b.timestamp, a.timestamp, SECOND) < 600
ORDER BY a.session_id, a.timestamp;

-- 10. Turn abandonment — the user placed sessions and then the
-- conversation just stopped, with no follow-up turn at all. Also a pure
-- query over existing data: the last turn per session_id, checked for
-- whether it ever got a successor.
WITH last_turn_per_session AS (
  SELECT
    jsonPayload.session_id,
    jsonPayload.turn_id,
    timestamp,
    ARRAY_LENGTH(jsonPayload.tool_calls) > 0 AS placed_or_acted,
    ROW_NUMBER() OVER (PARTITION BY jsonPayload.session_id ORDER BY timestamp DESC) AS rn
  FROM `{{PROJECT}}.day_planner_turns.{{TABLE}}`
  WHERE timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
)
SELECT session_id, turn_id, timestamp AS abandoned_after
FROM last_turn_per_session
WHERE rn = 1
  AND placed_or_acted
  -- Not abandoned if it's still within a plausible reply window — this
  -- only flags a session whose last turn is old enough that a reply was
  -- realistically expected and never came.
  AND timestamp < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 HOUR)
ORDER BY abandoned_after DESC;
