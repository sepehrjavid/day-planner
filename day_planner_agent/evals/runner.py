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
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
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

from day_planner_agent.evals.invariants import TIER1_INVARIANTS, World  # noqa: E402
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
        return self.exception is None and all(c.passed for c in self.checks)


@dataclass
class ScenarioResult:
    scenario: Scenario
    trials: list[TrialResult] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        if not self.trials:
            return 0.0
        return sum(1 for t in self.trials if t.passed) / len(self.trials)


async def run_trial(scenario: Scenario, *, instruction_template: str | None = None) -> TrialResult:
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

    fixture = ScenarioFixture(
        zones=scenario.given.zones,
        sleep_schedule=scenario.given.sleep_schedule,
        habits=scenario.given.habits,
        calendar_events=scenario.given.calendar_events,
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
    input_tokens = output_tokens = thinking_tokens = 0
    started = time.monotonic()
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
    except Exception as exc:  # noqa: BLE001 — a scenario run failing outright is a result, not a crash
        return TrialResult(
            tool_calls=tool_calls,
            placed_events=[],
            checks=[],
            exception=repr(exc),
            wall_s=time.monotonic() - started,
        )
    wall_s = time.monotonic() - started

    placed_events = fixture.calendar_service.placed_events()
    checks: list[Check] = []

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
    world = World(zones=fixture.zones, sleep_schedule=fixture.sleep_schedule, habits=fixture.habits)
    for name in scenario.expect.invariants:
        fn = TIER1_INVARIANTS.get(name)
        if fn is None:
            checks.append(Check(name, False, "unknown invariant name"))
            continue
        result = fn(world, placed_events, tool_calls)
        checks.append(Check(name, result.passed, result.detail))

    return TrialResult(
        tool_calls,
        placed_events,
        checks,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        thinking_tokens=thinking_tokens,
        wall_s=wall_s,
    )


async def run_scenario(
    scenario: Scenario, *, repeat: int, instruction_template: str | None = None
) -> ScenarioResult:
    result = ScenarioResult(scenario=scenario)
    for _ in range(repeat):
        result.trials.append(await run_trial(scenario, instruction_template=instruction_template))
    return result


async def run_suite(
    scenario_dir: Path, *, repeat: int = DEFAULT_REPEAT, instruction_template: str | None = None
) -> list[ScenarioResult]:
    scenarios = load_scenarios(scenario_dir)
    results = []
    for scenario in scenarios:
        results.append(
            await run_scenario(scenario, repeat=repeat, instruction_template=instruction_template)
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
                lines.append(f"  trial {i}: EXCEPTION {trial.exception}")
                continue
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
    args = parser.parse_args()

    instruction_template = Path(args.instruction).read_text() if args.instruction else None
    results = asyncio.run(
        run_suite(Path(args.scenario_dir), repeat=args.repeat, instruction_template=instruction_template)
    )
    print(format_report(results))

    constraint_rate = tier_pass_rate(results, "constraint")
    return 0 if constraint_rate is None or constraint_rate >= 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
