"""A3.1's eval runner. Loads scenarios, runs each one against the real
model (Vertex AI — this is the thing under test, never faked) with
fixture backend/calendar state, and checks the declared tool-call and
invariant expectations.

Not collected by the regular `pytest day_planner_agent/tests` suite —
that suite is deliberately credential-free (see conftest.py's own
comment on why), and evals need a real model. Run this as a plain
script, not `python -m` — day_planner_agent/__init__.py unconditionally
imports agent.py (for Agent Engine's own entrypoint resolution), and
`-m` would trigger that package import, and agent.py's project/
credential resolution, before this file's own _configure_environment()
ever got a chance to run:

    day_planner_agent/.venv/bin/python day_planner_agent/evals/runner.py \\
        [scenario_dir] [--repeat N]

Set GOOGLE_CLOUD_PROJECT/GOOGLE_CLOUD_LOCATION/GOOGLE_APPLICATION_CREDENTIALS
yourself first if the defaults below (this deployment's own project,
picked up via `gcloud auth application-default login`) aren't right for
your environment.

CI wiring (running this automatically on every commit, per the roadmap's
own scope item 9) is a deliberate follow-up, not part of this change —
see the PR description for why.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _configure_environment() -> None:
    """Must run before any day_planner_agent import — agent.py builds its
    Agent (and resolves a Vertex AI project) at import time. Uses
    setdefault throughout specifically so a caller's own env (a real CI
    service account, a different project) always wins; the fallback here
    is just "make it work out of the box on a dev machine already logged
    in via `gcloud auth application-default login`."""
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "sepi-dev-planner")
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "europe-west1")
    os.environ.setdefault("INTERNAL_BACKEND_URL", "https://internal.example.invalid")
    adc_path = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
    if adc_path.exists():
        os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", str(adc_path))


_configure_environment()
sys.path.insert(0, str(_REPO_ROOT))

from google.adk.runners import InMemoryRunner  # noqa: E402
from google.genai import types as genai_types  # noqa: E402

from day_planner_agent.evals.invariants import ALL_INVARIANTS, World  # noqa: E402
from day_planner_agent.evals.scenario import Scenario, load_scenarios  # noqa: E402

DEFAULT_SCENARIO_DIR = Path(__file__).parent / "scenarios"
DEFAULT_REPEAT = 3  # roadmap says 3-5; 3 keeps a ~10-scenario suite's real-model cost bounded


class _PlainPatcher:
    """conftest.py's ScenarioFixture.install() expects pytest's
    monkeypatch interface (.setattr(obj, name, value)) — this is the
    same three-argument shape with no teardown, which is fine here since
    each trial builds a brand-new ScenarioFixture and overwrites the
    same module attributes again rather than needing them restored."""

    def setattr(self, obj, name, value):
        setattr(obj, name, value)


@dataclass
class Check:
    description: str
    passed: bool
    detail: str = ""


@dataclass
class TrialResult:
    tool_calls: list[dict]
    placed_events: list[dict]
    checks: list[Check]
    exception: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    wall_s: float = 0.0

    @property
    def passed(self) -> bool:
        # Deliberately does NOT require exception is None — a scenario
        # can assert exactly one property (e.g. no_events_actually_placed
        # for A3.2's failure-injection scenarios) and that property can
        # hold even when the run also raised partway through (the model
        # re-checking a still-broken backend mid-turn, say). The
        # exception is never hidden either way — it's always in the
        # report — but whether it makes the trial *fail* is up to
        # whether it's reflected in a check the scenario actually
        # declared, not an automatic blanket rule.
        return all(c.passed for c in self.checks)


@dataclass
class ScenarioResult:
    scenario: Scenario
    trials: list[TrialResult] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        if not self.trials:
            return 0.0
        return sum(1 for t in self.trials if t.passed) / len(self.trials)


async def run_trial(
    scenario: Scenario,
    *,
    instruction_template: str | None = None,
    model: str | None = None,
) -> TrialResult:
    # Imported lazily (after _configure_environment has run) rather than
    # at module scope — agent.py resolves Vertex AI project/credentials
    # at import time, so it must not be imported before the environment
    # variables above are in place.
    from day_planner_agent import agent as agent_module
    from day_planner_agent.tests.conftest import ScenarioFixture

    if instruction_template is not None:
        # _build_instruction re-reads this module global on every call
        # (that's the whole point of A0.4's injectable clock pattern
        # applying here too) — swapping it is enough to run the same
        # scenario against a different instruction.md without touching
        # the file on disk or reconstructing the Agent.
        agent_module._INSTRUCTION_TEMPLATE = instruction_template

    if model is not None:
        # A3.3: LlmAgent.model is a plain, uncached pydantic field —
        # canonical_model re-resolves it via LLMRegistry on every access
        # (see google.adk.agents.llm_agent.LlmAgent.canonical_model), so
        # reassigning it here is enough to run the same scenario against
        # a different model without reconstructing the Agent.
        agent_module._llm_agent.model = model

    fixture = ScenarioFixture(
        zones=scenario.given.zones,
        sleep_schedule=scenario.given.sleep_schedule,
        habits=scenario.given.habits,
        calendar_events=scenario.given.calendar_events,
        zones_fetch_fails=scenario.given.zones_fetch_fails,
        needs_auth=scenario.given.needs_auth,
        calendar_access_role=scenario.given.calendar_access_role,
    )
    fixture.install(_PlainPatcher())
    agent_module._now = lambda: datetime.strptime(scenario.given.today, "%Y-%m-%d")

    app_name = "day_planner_agent_eval"
    session_id = f"eval-{uuid.uuid4()}"
    runner = InMemoryRunner(agent=agent_module._llm_agent, app_name=app_name)
    await runner.session_service.create_session(
        app_name=app_name, user_id="eval-user", session_id=session_id
    )

    tool_calls: list[dict] = []
    reply_texts: list[str] = []
    input_tokens = output_tokens = thinking_tokens = 0
    started = time.monotonic()
    exception_repr: str | None = None
    message = genai_types.Content(role="user", parts=[genai_types.Part(text=scenario.user_says)])
    try:
        async for event in runner.run_async(
            user_id="eval-user", session_id=session_id, new_message=message
        ):
            usage = getattr(event, "usage_metadata", None)
            if usage is not None:
                input_tokens += getattr(usage, "prompt_token_count", None) or 0
                output_tokens += getattr(usage, "candidates_token_count", None) or 0
                thinking_tokens += getattr(usage, "thoughts_token_count", None) or 0
            content = getattr(event, "content", None)
            if not content:
                continue
            for part in content.parts or []:
                call = getattr(part, "function_call", None)
                if call is not None:
                    tool_calls.append({"name": call.name, "args": dict(call.args or {})})
                text = getattr(part, "text", None)
                if text:
                    reply_texts.append(text)
    except Exception as exc:  # noqa: BLE001
        # Recorded, not swallowed — but deliberately doesn't bail out of
        # checking whatever the run did manage to do before it broke
        # (see TrialResult.passed's own docstring on why an exception
        # doesn't automatically fail a trial).
        exception_repr = repr(exc)
    wall_s = time.monotonic() - started
    reply_text = "".join(reply_texts)

    placed_events = fixture.calendar_service.placed_events()
    checks: list[Check] = []

    if scenario.expect.model_invoked:
        # input_tokens only accumulates from usage_metadata events the
        # model actually produced — a turn that crashes at the preload
        # callback, before the model is ever called, never yields one,
        # regardless of what tool_calls/placed_events end up being (see
        # A3.2's zone_fetch_fails scenario, where "nothing was placed"
        # is true both when the agent correctly declines and when the
        # turn crashes before ever starting — this is what tells them
        # apart).
        checks.append(
            Check(
                "model was invoked (produced token usage) before any exception",
                input_tokens > 0,
                f"input_tokens={input_tokens}, exception={exception_repr!r}",
            )
        )

    for expectation in scenario.expect.tool_calls:
        count = sum(1 for c in tool_calls if c["name"] == expectation.name)
        ok = count >= expectation.min_count and (
            expectation.max_count is None or count <= expectation.max_count
        )
        checks.append(
            Check(
                f"{expectation.name} called >= {expectation.min_count} time(s)",
                ok,
                f"actual count: {count}",
            )
        )

    # World reflects the fixture's *final* state, not scenario.given — a
    # scenario where the model calls create_habit/update_habit mid-run
    # (e.g. the zone-anchored commute habit) only has a real habit_id and
    # allowed_zones once the run is over. Checking against the static
    # given.habits would fail every such scenario for a reason that has
    # nothing to do with whether the agent behaved correctly (see
    # invariants.py's own module docstring).
    world = World(
        zones=fixture.zones,
        sleep_schedule=fixture.sleep_schedule,
        habits=fixture.habits,
        calendar_events=scenario.given.calendar_events,
        today=scenario.given.today,
    )
    for name in scenario.expect.invariants:
        fn = ALL_INVARIANTS.get(name)
        if fn is None:
            checks.append(Check(name, False, "unknown invariant name"))
            continue
        result = fn(world, placed_events, tool_calls, reply_text)
        checks.append(Check(name, result.passed, result.detail))

    return TrialResult(
        tool_calls,
        placed_events,
        checks,
        exception=exception_repr,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        thinking_tokens=thinking_tokens,
        wall_s=wall_s,
    )


async def run_scenario(
    scenario: Scenario,
    *,
    repeat: int,
    instruction_template: str | None = None,
    model: str | None = None,
) -> ScenarioResult:
    result = ScenarioResult(scenario=scenario)
    for _ in range(repeat):
        result.trials.append(
            await run_trial(scenario, instruction_template=instruction_template, model=model)
        )
    return result


async def run_suite(
    scenario_dir: Path,
    *,
    repeat: int = DEFAULT_REPEAT,
    instruction_template: str | None = None,
    model: str | None = None,
) -> list[ScenarioResult]:
    scenarios = load_scenarios(scenario_dir)
    results = []
    for scenario in scenarios:
        results.append(
            await run_scenario(
                scenario, repeat=repeat, instruction_template=instruction_template, model=model
            )
        )
    return results


def tier_pass_rate(results: list[ScenarioResult], tier: str) -> float | None:
    trials = [t for r in results if r.scenario.tier == tier for t in r.trials]
    if not trials:
        return None
    return sum(1 for t in trials if t.passed) / len(trials)


def format_report(results: list[ScenarioResult]) -> str:
    lines = []
    all_trials = [t for r in results for t in r.trials]
    for r in results:
        lines.append(f"{r.scenario.name} [{r.scenario.tier}] — {r.pass_rate:.0%} ({len(r.trials)} trials)")
        for i, trial in enumerate(r.trials, start=1):
            if trial.exception:
                # Still shown, never instead of the checks below — see
                # TrialResult.passed's docstring on why an exception
                # doesn't automatically fail a trial.
                lines.append(f"  trial {i}: EXCEPTION (informational) {trial.exception}")
            for c in trial.checks:
                mark = "OK" if c.passed else "FAIL"
                lines.append(f"  trial {i}: [{mark}] {c.description}" + (f" — {c.detail}" if c.detail and not c.passed else ""))
    lines.append("")
    for tier in ("constraint", "decision", "quality"):
        rate = tier_pass_rate(results, tier)
        if rate is not None:
            lines.append(f"tier={tier} pass_rate={rate:.1%}")
    lines.append("")
    lines.append(f"scenarios={len(results)} trials={len(all_trials)}")
    lines.append(f"total_wall_s={sum(t.wall_s for t in all_trials):.1f}")
    lines.append(f"total_input_tokens={sum(t.input_tokens for t in all_trials)}")
    lines.append(f"total_output_tokens={sum(t.output_tokens for t in all_trials)}")
    lines.append(f"total_thinking_tokens={sum(t.thinking_tokens for t in all_trials)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# A3.3 — model comparison matrix.
#
# Pricing is standard (non-batch, non-priority) pay-as-you-go, USD per 1M
# tokens, from https://ai.google.dev/gemini-api/docs/pricing, fetched
# 2026-08-21. Gemini list pricing is unified across the Gemini Developer
# API and Vertex AI for standard-tier usage; this deployment's own
# prompts are far under the 200k-token tier boundary Pro's pricing has
# (see BASELINE.md — tens of thousands of tokens per trial, not
# hundreds of thousands), so only the <=200k row is included here.
# **Verify against Vertex AI's own current pricing page before trusting
# this for a real budget decision** — same "verify, don't assume"
# discipline A0.5 already applied to the model-version string itself;
# this table is a snapshot, not a live source.
# ---------------------------------------------------------------------------
MODEL_PRICING_USD_PER_1M_TOKENS = {
    # (input, output — thinking tokens are billed at the output rate,
    # per Google's pricing page: "includes thinking tokens")
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
}


def estimate_cost_usd(trial: TrialResult, model: str) -> float | None:
    pricing = MODEL_PRICING_USD_PER_1M_TOKENS.get(model)
    if pricing is None:
        return None
    input_rate, output_rate = pricing
    # Thinking tokens are billed at the output rate — see the pricing
    # note above — so they're added to output, not tracked separately.
    return (
        trial.input_tokens * input_rate
        + (trial.output_tokens + trial.thinking_tokens) * output_rate
    ) / 1_000_000


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(pct * (len(ordered) - 1))))
    return ordered[index]


async def run_comparison(
    scenario_dir: Path, *, models: list[str], repeat: int = DEFAULT_REPEAT
) -> dict[str, list[ScenarioResult]]:
    """A3.3 scope item 1+2: the same suite, run once per candidate model,
    for the comparison matrix in format_comparison_matrix. Scenarios are
    reloaded per model rather than shared — cheap, and avoids any risk of
    one model's run mutating Scenario objects the next model's run reads."""
    results_by_model: dict[str, list[ScenarioResult]] = {}
    for model in models:
        results_by_model[model] = await run_suite(scenario_dir, repeat=repeat, model=model)
    return results_by_model


def format_comparison_matrix(results_by_model: dict[str, list[ScenarioResult]]) -> str:
    lines = ["model comparison matrix (A3.3)", ""]
    header = f"{'model':<20}{'tier1':>8}{'tier2':>8}{'p50 wall_s':>12}{'p95 wall_s':>12}{'$/scenario':>14}{'total $':>12}"
    lines.append(header)
    lines.append("-" * len(header))
    for model, results in results_by_model.items():
        all_trials = [t for r in results for t in r.trials]
        wall_times = [t.wall_s for t in all_trials]
        costs = [estimate_cost_usd(t, model) for t in all_trials]
        known_costs = [c for c in costs if c is not None]
        total_cost = sum(known_costs) if known_costs else None
        per_scenario = total_cost / len(results) if total_cost is not None and results else None
        tier1 = tier_pass_rate(results, "constraint")
        tier2 = tier_pass_rate(results, "decision")
        lines.append(
            f"{model:<20}"
            f"{(f'{tier1:.1%}' if tier1 is not None else 'n/a'):>8}"
            f"{(f'{tier2:.1%}' if tier2 is not None else 'n/a'):>8}"
            f"{_percentile(wall_times, 0.50):>12.1f}"
            f"{_percentile(wall_times, 0.95):>12.1f}"
            f"{(f'${per_scenario:.4f}' if per_scenario is not None else 'unknown'):>14}"
            f"{(f'${total_cost:.4f}' if total_cost is not None else 'unknown'):>12}"
        )
    lines.append("")
    lines.append(
        "cost = input_tokens*input_rate + (output_tokens+thinking_tokens)*output_rate, "
        "per MODEL_PRICING_USD_PER_1M_TOKENS above — verify against current Vertex AI "
        "pricing before treating this as a budget figure, not just a relative comparison."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# A3.4 (rescoped) — a committed, append-only run history.
#
# Not BigQuery — that's explicitly deferred until there's enough history
# to want querying rather than reading (see the roadmap's own note).
# Version control gives history, diffs and blame for free, which is the
# whole requirement at this stage: catch a model alias silently moving
# underneath the pin, or slow erosion across A4.3's rule-by-rule cuts,
# by comparing rows over time rather than only against the immediately
# preceding run.
# ---------------------------------------------------------------------------
HISTORY_PATH = Path(__file__).parent / "history.jsonl"


def _git_commit_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=_REPO_ROOT, text=True
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return "unknown"


def append_history_record(
    results: list[ScenarioResult], *, repeat: int, scenario_dir: Path, model: str
) -> dict:
    """One line per eval run, appended never rewritten — see this
    module's own note on why a committed file rather than BigQuery. The
    model string is the one field this exists specifically to capture:
    it's the only signal that would ever distinguish a moved model alias
    from a genuine prompt regression (see A0.5, A3.3's own retirement-date
    comment on `agent.py`'s pinned model)."""
    record = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "commit_sha": _git_commit_sha(),
        "model": model,
        "repeat": repeat,
        "scenario_dir": str(scenario_dir),
        "tier_pass_rates": {
            tier: rate
            for tier in ("constraint", "decision", "quality")
            if (rate := tier_pass_rate(results, tier)) is not None
        },
    }
    with HISTORY_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario_dir", nargs="?", default=str(DEFAULT_SCENARIO_DIR))
    parser.add_argument("--repeat", type=int, default=DEFAULT_REPEAT)
    parser.add_argument(
        "--instruction",
        type=str,
        default=None,
        help=(
            "Path to an instruction.md to run against instead of the "
            "current one — e.g. a git-checked-out pre-A2.5 version, for "
            "the old-vs-new comparison A3.1's own notes suggest."
        ),
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Run the suite against this model instead of agent.py's pinned one (A3.3).",
    )
    parser.add_argument(
        "--compare-models",
        type=str,
        default=None,
        help=(
            "Comma-separated model names — runs the full suite once per model and "
            "prints a comparison matrix (pass rate x latency x cost) instead of the "
            "normal per-scenario report (A3.3). Mutually exclusive with --model."
        ),
    )
    args = parser.parse_args()

    if args.compare_models:
        if args.model:
            print("--model and --compare-models are mutually exclusive", file=sys.stderr)
            return 2
        models = [m.strip() for m in args.compare_models.split(",") if m.strip()]
        results_by_model = asyncio.run(
            run_comparison(Path(args.scenario_dir), models=models, repeat=args.repeat)
        )
        for model, results in results_by_model.items():
            print(f"=== {model} ===")
            print(format_report(results))
            print()
        print(format_comparison_matrix(results_by_model))
        return 0

    instruction_template = Path(args.instruction).read_text() if args.instruction else None
    results = asyncio.run(
        run_suite(
            Path(args.scenario_dir),
            repeat=args.repeat,
            instruction_template=instruction_template,
            model=args.model,
        )
    )
    print(format_report(results))

    if instruction_template is None:
        # An --instruction override is a deliberate what-if (a meta-test
        # rule removal, an old-vs-new comparison) against text that isn't
        # the real, deployed instruction.md — recording it here would
        # make a genuine regression indistinguishable from an
        # intentional experiment in the same trend line.
        from day_planner_agent import agent as agent_module

        record = append_history_record(
            results,
            repeat=args.repeat,
            scenario_dir=Path(args.scenario_dir),
            model=agent_module._llm_agent.model,
        )
        print(f"appended to {HISTORY_PATH}: {json.dumps(record)}")

    constraint_rate = tier_pass_rate(results, "constraint")
    return 0 if constraint_rate is None or constraint_rate >= 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
