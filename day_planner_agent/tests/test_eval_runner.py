"""Unit coverage of evals/runner.py's pure helpers — cost estimation and
the latency percentile helper A3.3 added for the model comparison
matrix, and A3.4's append-only run history. Safe to import in the normal
pytest suite (conftest.py already sets the env vars agent.py needs at
import time, same as the other test_eval_*.py modules) — the real
model-invoking parts of runner.py are exercised separately, by hand,
since they cost real API calls.
"""

import json
from pathlib import Path

from day_planner_agent.evals import runner
from day_planner_agent.evals.scenario import Expect, Given, Scenario


def _trial(*, input_tokens=0, output_tokens=0, thinking_tokens=0):
    return runner.TrialResult(
        tool_calls=[],
        placed_events=[],
        checks=[],
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        thinking_tokens=thinking_tokens,
    )


def test_estimate_cost_usd_known_model():
    trial = _trial(input_tokens=1_000_000, output_tokens=1_000_000)
    cost = runner.estimate_cost_usd(trial, "gemini-2.5-flash")
    assert cost == 0.30 + 2.50


def test_estimate_cost_usd_bills_thinking_at_output_rate():
    with_thinking = _trial(thinking_tokens=1_000_000)
    with_output = _trial(output_tokens=1_000_000)
    assert runner.estimate_cost_usd(with_thinking, "gemini-2.5-flash") == (
        runner.estimate_cost_usd(with_output, "gemini-2.5-flash")
    )


def test_estimate_cost_usd_unknown_model_returns_none():
    trial = _trial(input_tokens=1000, output_tokens=1000)
    assert runner.estimate_cost_usd(trial, "some-unpriced-model") is None


def test_percentile_matches_expected_index():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert runner._percentile(values, 0.50) == 3.0
    assert runner._percentile(values, 0.95) == 5.0


def test_percentile_empty_list_returns_zero():
    assert runner._percentile([], 0.50) == 0.0


def _scenario(name, tier):
    return Scenario(
        name=name,
        tier=tier,
        given=Given(today="2026-08-24"),
        user_says="plan my week",
        expect=Expect(),
        source_file=Path("dummy.yaml"),
    )


def _scenario_result(name, tier, *, passed_flags):
    result = runner.ScenarioResult(scenario=_scenario(name, tier))
    for passed in passed_flags:
        result.trials.append(
            runner.TrialResult(
                tool_calls=[],
                placed_events=[],
                checks=[runner.Check("dummy check", passed)],
            )
        )
    return result


def test_git_commit_sha_returns_a_real_sha_in_this_repo():
    sha = runner._git_commit_sha()
    assert sha != "unknown"
    assert len(sha) == 40


def test_append_history_record_writes_one_json_line(tmp_path, monkeypatch):
    history_path = tmp_path / "history.jsonl"
    monkeypatch.setattr(runner, "HISTORY_PATH", history_path)

    results = [_scenario_result("s1", "constraint", passed_flags=[True, True, False])]
    record = runner.append_history_record(
        results, repeat=3, scenario_dir=Path("scenarios"), model="gemini-2.5-flash"
    )

    lines = history_path.read_text().splitlines()
    assert len(lines) == 1
    written = json.loads(lines[0])
    assert written == record
    assert written["model"] == "gemini-2.5-flash"
    assert written["repeat"] == 3
    assert written["tier_pass_rates"]["constraint"] == 2 / 3
    assert "decision" not in written["tier_pass_rates"]
    assert "commit_sha" in written and "date" in written


def test_append_history_record_appends_never_overwrites(tmp_path, monkeypatch):
    history_path = tmp_path / "history.jsonl"
    monkeypatch.setattr(runner, "HISTORY_PATH", history_path)

    results = [_scenario_result("s1", "constraint", passed_flags=[True])]
    first = runner.append_history_record(
        results, repeat=1, scenario_dir=Path("scenarios"), model="gemini-2.5-flash"
    )
    second = runner.append_history_record(
        results, repeat=1, scenario_dir=Path("scenarios"), model="gemini-2.5-pro"
    )

    lines = history_path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == first
    assert json.loads(lines[1]) == second
    assert first["model"] == "gemini-2.5-flash"
    assert second["model"] == "gemini-2.5-pro"
