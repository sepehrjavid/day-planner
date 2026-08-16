-- Queries against A1.1/A1.2's turn-record telemetry. Kept alongside
-- turn_log.py (the thing that produces the rows these query) rather than
-- reinvented per question — see docs/roadmaps/1-agent.md A1.2.
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
-- fields (turn_id, session_id, user_ref, tool_calls[], model_calls,
-- input_tokens, output_tokens, thinking_tokens, preload_ok, outcome,
-- wall_ms — see turn_log.py's TurnRecorder.emit).

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
