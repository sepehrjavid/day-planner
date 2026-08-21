"""Unit coverage of evals/runner.py's pure helpers — cost estimation and
the latency percentile helper A3.3 added for the model comparison
matrix. Safe to import in the normal pytest suite (conftest.py already
sets the env vars agent.py needs at import time, same as the other
test_eval_*.py modules) — the real model-invoking parts of runner.py are
exercised separately, by hand, since they cost real API calls.
"""

from day_planner_agent.evals import runner


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
