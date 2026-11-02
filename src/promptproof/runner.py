"""Run a suite: render prompts, call the provider, check assertions."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union

from .assertions import AssertionResult, check_assertions
from .judge import LLMJudge
from .providers import MockProvider, OpenAICompatibleProvider
from .snapshots import default_snapshot_path, load_goldens, save_goldens
from .suite import Case, Suite
from .template import render_template

Provider = Union[MockProvider, OpenAICompatibleProvider]


@dataclass
class CaseResult:
    name: str
    passed: bool
    duration_ms: float
    prompt: str
    response: str
    assertions: list[AssertionResult] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usage_estimated: bool = False
    error: str = ""
    #: Per-turn results for multi-turn cases; empty for single-turn cases.
    turns: list["CaseResult"] = field(default_factory=list)
    #: Repeat-trial summary: how many trials ran, how many passed, and the
    #: effective pass-rate gate the case was judged against (1.0 = the
    #: default "every trial must pass"). Single-trial cases report 1/1.
    trials: int = 1
    passed_trials: int = 0
    min_pass_rate: float = 1.0

    @property
    def pass_rate(self) -> float:
        return self.passed_trials / self.trials if self.trials else 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class RunReport:
    suite_name: str
    provider: str
    model: str
    cases: list[CaseResult] = field(default_factory=list)
    estimated_cost_usd: float = 0.0
    cost_estimated: bool = False
    budget_error: str = ""

    @property
    def passed_count(self) -> int:
        return sum(1 for c in self.cases if c.passed)

    @property
    def failed_count(self) -> int:
        return len(self.cases) - self.passed_count

    @property
    def total_tokens(self) -> int:
        return sum(c.total_tokens for c in self.cases)

    @property
    def total_prompt_tokens(self) -> int:
        return sum(c.prompt_tokens for c in self.cases)

    @property
    def total_completion_tokens(self) -> int:
        return sum(c.completion_tokens for c in self.cases)

    @property
    def usage_estimated(self) -> bool:
        return any(c.usage_estimated for c in self.cases)

    @property
    def ok(self) -> bool:
        return self.failed_count == 0 and not self.budget_error


def make_provider(suite: Suite, *, mock: bool) -> Provider:
    """Build the provider for a run (mock for offline, HTTP otherwise)."""
    if mock:
        return MockProvider(suite.mock)
    return OpenAICompatibleProvider(
        base_url=suite.model.base_url,
        model=suite.model.name,
        api_key_env=suite.model.api_key_env,
        temperature=suite.model.temperature,
    )


def snapshot_path(suite: Suite) -> Path | str:
    """Resolve the golden-file path for *suite* (explicit or default)."""
    if suite.snapshot_file:
        return Path(suite.snapshot_file)
    return default_snapshot_path(suite.source or f"{suite.name}.yaml")


def _check_snapshot(response: str, golden: str) -> AssertionResult:
    """Diff a response against its golden value."""
    if response == golden:
        return AssertionResult("snapshot", True, "matches golden snapshot")
    return AssertionResult(
        "snapshot",
        False,
        f"response differs from golden snapshot ({len(response)} chars "
        f"vs golden {len(golden)} chars); re-run with --record to update",
    )


def run_suite(
    suite: Suite, *, mock: bool = False, record: bool = False
) -> RunReport:
    """Execute every case in *suite* and return a :class:`RunReport`.

    With ``record=True`` each snapshot-enabled case's response is written to
    the golden file instead of being diffed, and the run exits green (cases
    that error still fail).
    """
    provider = make_provider(suite, mock=mock)
    # Built once per run; only used by llm_judge assertions, and an
    # unconfigured judge skips instead of failing (see judge.py).
    judge = LLMJudge.from_env()
    report = RunReport(
        suite_name=suite.name,
        provider=provider.name,
        model="mock" if mock else suite.model.name,
    )
    goldens: dict[str, str] = {} if record else load_goldens(snapshot_path(suite))
    recorded: dict[str, str] = {}
    for case in suite.cases:
        result = _run_case_trials(
            case, suite, provider, judge, record=record, goldens=goldens
        )
        report.cases.append(result)
        if record:
            # _run_case_trials puts the first trial's response in
            # result.response in record mode, so this records trial 1.
            recorded[case.name] = result.response
    if record:
        save_goldens(snapshot_path(suite), recorded)
    _apply_budgets(report, suite)
    return report


def _effective_repeat(suite: Suite, case: Case) -> int:
    return case.repeat if case.repeat is not None else suite.repeat


def _effective_min_pass_rate(suite: Suite, case: Case) -> float:
    if case.min_pass_rate is not None:
        return case.min_pass_rate
    if suite.min_pass_rate is not None:
        return suite.min_pass_rate
    return 1.0


def _run_case_trials(
    case: Case,
    suite: Suite,
    provider: Provider,
    judge: LLMJudge,
    *,
    record: bool,
    goldens: dict[str, str],
) -> CaseResult:
    """Run *case* for its effective repeat count and aggregate the trials.

    A trial passes iff all its assertions pass and it raised no error.
    The case passes iff ``passed_trials / trials >= min_pass_rate`` (with
    no gate configured anywhere, that is 1.0 — every trial must pass, the
    pre-repeat behaviour). In ``--record`` mode trial 1 is recorded as
    usual and later trials are run normally but diffed (snapshot check)
    against trial 1's response instead of the golden file, so a drifting
    repeat is caught at record time; the recorded golden is trial 1's
    response. The aggregated result's prompt/response/assertions/turns
    come from the last trial (first trial in record mode, whose response
    is the golden), while token usage and duration are summed over all
    trials.
    """
    repeat = _effective_repeat(suite, case)
    min_pass_rate = _effective_min_pass_rate(suite, case)
    run_one = _run_multi_turn if case.turns else _run_single_turn
    trial_results: list[CaseResult] = []
    first_response = ""
    for trial_index in range(repeat):
        if record and repeat > 1 and trial_index > 0:
            result = run_one(
                case,
                suite,
                provider,
                judge,
                time.perf_counter(),
                record=False,
                goldens=goldens,
                snapshot_golden=first_response,
            )
        else:
            result = run_one(
                case,
                suite,
                provider,
                judge,
                time.perf_counter(),
                record=record,
                goldens=goldens,
            )
        if trial_index == 0:
            first_response = result.response
        trial_results.append(result)

    passed_trials = sum(1 for t in trial_results if t.passed)
    pass_rate = passed_trials / repeat if repeat else 0.0
    passed = pass_rate >= min_pass_rate - 1e-9
    first = trial_results[0]
    last = trial_results[-1]
    error = ""
    for trial in trial_results:
        if trial.error:
            error = trial.error
            break
    if not error and not passed and repeat > 1:
        error = (
            f"pass rate {pass_rate:.2f} < required min_pass_rate "
            f"{min_pass_rate:.2f} ({passed_trials}/{repeat} trials passed)"
        )
    return CaseResult(
        name=case.name,
        passed=passed,
        duration_ms=sum(t.duration_ms for t in trial_results),
        prompt=first.prompt,
        response=first.response if record else last.response,
        assertions=last.assertions,
        prompt_tokens=sum(t.prompt_tokens for t in trial_results),
        completion_tokens=sum(t.completion_tokens for t in trial_results),
        usage_estimated=any(t.usage_estimated for t in trial_results),
        error=error,
        turns=last.turns,
        trials=repeat,
        passed_trials=passed_trials,
        min_pass_rate=min_pass_rate,
    )


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


def _run_single_turn(
    case: Case,
    suite: Suite,
    provider: Provider,
    judge: LLMJudge,
    started: float,
    *,
    record: bool,
    goldens: dict[str, str],
    snapshot_golden: str | None = None,
) -> CaseResult:
    """Run a classic single-turn case.

    ``snapshot_golden``, when given, replaces the golden-file lookup:
    the response is diffed against it regardless of ``case.snapshot``
    (used for repeat trials after the first in ``--record`` mode).
    """
    prompt = render_template(suite.prompt, case.vars)
    try:
        completion = provider.complete(prompt, case_mock_response=case.mock_response)
    except Exception as exc:  # provider errors fail the case, not the run
        return CaseResult(
            name=case.name,
            passed=False,
            duration_ms=_elapsed_ms(started),
            prompt=prompt,
            response="",
            error=str(exc),
        )
    if record:
        return CaseResult(
            name=case.name,
            passed=True,
            duration_ms=_elapsed_ms(started),
            prompt=prompt,
            response=completion.text,
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
            usage_estimated=completion.usage_estimated,
        )
    results = check_assertions(completion.text, case.assertions, judge=judge)
    if snapshot_golden is not None:
        results.append(_check_snapshot(completion.text, snapshot_golden))
    elif case.snapshot:
        golden = goldens.get(case.name)
        if golden is None:
            return CaseResult(
                name=case.name,
                passed=False,
                duration_ms=_elapsed_ms(started),
                prompt=prompt,
                response=completion.text,
                assertions=results,
                prompt_tokens=completion.prompt_tokens,
                completion_tokens=completion.completion_tokens,
                usage_estimated=completion.usage_estimated,
                error=(
                    f"no golden snapshot for case {case.name!r}; "
                    "run with --record to create it"
                ),
            )
        results.append(_check_snapshot(completion.text, golden))
    return CaseResult(
        name=case.name,
        passed=all(r.passed for r in results),
        duration_ms=_elapsed_ms(started),
        prompt=prompt,
        response=completion.text,
        assertions=results,
        prompt_tokens=completion.prompt_tokens,
        completion_tokens=completion.completion_tokens,
        usage_estimated=completion.usage_estimated,
    )


def _run_multi_turn(
    case: Case,
    suite: Suite,
    provider: Provider,
    judge: LLMJudge,
    started: float,
    *,
    record: bool,
    goldens: dict[str, str],
    snapshot_golden: str | None = None,
) -> CaseResult:
    """Run a multi-turn conversation case.

    The suite-level prompt becomes the system message; each turn's rendered
    prompt is sent as a user message and its reply is appended to the
    history, so the model sees the whole conversation. Assertions run per
    turn against that turn's reply; the case passes only if every turn
    passes. ``snapshot: true`` and ``--record`` apply to the conversation's
    final reply. ``snapshot_golden``, when given, replaces the golden-file
    lookup for that final-reply diff (repeat trials in ``--record`` mode).
    """
    messages: list[dict[str, str]] = [{"role": "system", "content": suite.prompt}]
    transcript = [f"system:\n{suite.prompt}"]
    turn_results: list[CaseResult] = []
    prompt_tokens = 0
    completion_tokens = 0
    usage_estimated = False
    final_text = ""
    error = ""
    for turn_index, turn in enumerate(case.turns):
        rendered = render_template(turn["prompt"], case.vars)
        messages.append({"role": "user", "content": rendered})
        transcript.append(f"user (turn {turn_index + 1}):\n{rendered}")
        try:
            completion = provider.complete_conversation(
                messages,
                case_mock_response=turn["mock_response"] or case.mock_response,
            )
        except Exception as exc:  # provider errors fail the case, not the run
            error = str(exc)
            break
        messages.append({"role": "assistant", "content": completion.text})
        transcript.append(f"assistant (turn {turn_index + 1}):\n{completion.text}")
        prompt_tokens += completion.prompt_tokens
        completion_tokens += completion.completion_tokens
        usage_estimated = usage_estimated or completion.usage_estimated
        final_text = completion.text
        if record:
            turn_result = CaseResult(
                name=f"{case.name} · turn {turn_index + 1}",
                passed=True,
                duration_ms=0.0,
                prompt=rendered,
                response=completion.text,
            )
        else:
            results = check_assertions(completion.text, turn["assertions"], judge=judge)
            turn_result = CaseResult(
                name=f"{case.name} · turn {turn_index + 1}",
                passed=all(r.passed for r in results),
                duration_ms=0.0,
                prompt=rendered,
                response=completion.text,
                assertions=results,
            )
        turn_results.append(turn_result)
    if not error and snapshot_golden is not None:
        if final_text != snapshot_golden:
            turn_results.append(
                CaseResult(
                    name=f"{case.name} · snapshot",
                    passed=False,
                    duration_ms=0.0,
                    prompt="",
                    response=final_text,
                    assertions=[_check_snapshot(final_text, snapshot_golden)],
                )
            )
    elif not record and case.snapshot and not error:
        golden = goldens.get(case.name)
        if golden is None:
            error = (
                f"no golden snapshot for case {case.name!r}; "
                "run with --record to create it"
            )
        elif final_text != golden:
            turn_results.append(
                CaseResult(
                    name=f"{case.name} · snapshot",
                    passed=False,
                    duration_ms=0.0,
                    prompt="",
                    response=final_text,
                    assertions=[_check_snapshot(final_text, golden)],
                )
            )
    return CaseResult(
        name=case.name,
        passed=not error and bool(turn_results) and all(t.passed for t in turn_results),
        duration_ms=_elapsed_ms(started),
        prompt="\n\n".join(transcript),
        response=final_text,
        # Per-turn assertions live on the turns; the case-level list is the
        # union only for single-turn cases, so reports read case.turns here.
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        usage_estimated=usage_estimated,
        error=error,
        turns=turn_results,
    )


def estimate_cost_usd(report: RunReport, suite: Suite) -> float:
    """Estimate the run's cost in USD from the suite's per-1k-token pricing."""
    return (
        report.total_prompt_tokens * suite.pricing.input_per_1k / 1000
        + report.total_completion_tokens * suite.pricing.output_per_1k / 1000
    )


def _apply_budgets(report: RunReport, suite: Suite) -> None:
    """Fill cost fields and fail the run when a suite budget is exceeded."""
    report.estimated_cost_usd = estimate_cost_usd(report, suite)
    report.cost_estimated = report.usage_estimated
    limits = suite.limits
    if limits.max_tokens is not None and report.total_tokens > limits.max_tokens:
        report.budget_error = (
            f"token budget exceeded: {report.total_tokens} tokens > "
            f"max_tokens {limits.max_tokens}"
        )
    elif limits.max_usd is not None and report.estimated_cost_usd > limits.max_usd:
        report.budget_error = (
            f"cost budget exceeded: ${report.estimated_cost_usd:.4f} > "
            f"max_usd ${limits.max_usd:.4f}"
        )
