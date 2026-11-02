"""Repeat trials + pass-rate gate: loading, validation, execution."""

import pytest

import promptproof.runner as runner_module
from promptproof.errors import PromptProofError
from promptproof.providers import Completion
from promptproof.runner import run_suite
from promptproof.suite import load_suite


class CyclingProvider:
    """Stub provider whose reply cycles through a fixed list per call."""

    name = "cycling-stub"

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0

    def _next(self):
        text = self._replies[self.calls % len(self._replies)]
        self.calls += 1
        return Completion(text=text, prompt_tokens=10, completion_tokens=5)

    def complete(self, prompt, *, case_mock_response=None):
        return self._next()

    def complete_conversation(self, messages, *, case_mock_response=None):
        return self._next()


def _write(tmp_path, text, name="suite.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _run_with(monkeypatch, suite, replies):
    provider = CyclingProvider(replies)
    monkeypatch.setattr(
        runner_module, "make_provider", lambda s, *, mock: provider
    )
    return run_suite(suite, mock=True), provider


FLAKY_SUITE = """\
name: flaky
prompt: "Answer"
repeat: {suite_repeat}
min_pass_rate: {suite_rate}
cases:
  - name: sometimes
    assertions:
      - contains: good
"""


def test_intermediate_pass_rate_passes_gate(tmp_path, monkeypatch):
    text = FLAKY_SUITE.format(suite_repeat=3, suite_rate="0.66")
    suite = load_suite(_write(tmp_path, text))
    # good, bad, good -> 2/3 trials pass
    report, provider = _run_with(monkeypatch, suite, ["good", "bad", "good"])
    case = report.cases[0]
    assert provider.calls == 3
    assert case.trials == 3
    assert case.passed_trials == 2
    assert case.pass_rate == pytest.approx(2 / 3)
    assert case.min_pass_rate == pytest.approx(0.66)
    assert case.passed
    assert report.ok
    # tokens aggregate across all trials
    assert case.prompt_tokens == 30
    assert case.completion_tokens == 15


def test_intermediate_pass_rate_below_gate_fails(tmp_path, monkeypatch):
    text = FLAKY_SUITE.format(suite_repeat=3, suite_rate="0.67")
    suite = load_suite(_write(tmp_path, text))
    report, _ = _run_with(monkeypatch, suite, ["good", "bad", "good"])
    case = report.cases[0]
    assert case.pass_rate == pytest.approx(2 / 3)
    assert not case.passed
    assert not report.ok
    assert "0.67" in case.error and "2/3" in case.error


def test_no_gate_means_every_trial_must_pass(tmp_path, monkeypatch):
    text = """\
name: strict
prompt: "Answer"
repeat: 2
cases:
  - name: sometimes
    assertions:
      - contains: good
"""
    suite = load_suite(_write(tmp_path, text))
    report, _ = _run_with(monkeypatch, suite, ["good", "bad"])
    case = report.cases[0]
    assert case.min_pass_rate == 1.0
    assert case.pass_rate == pytest.approx(0.5)
    assert not case.passed


def test_case_level_overrides_suite_level(tmp_path, monkeypatch):
    text = """\
name: overrides
prompt: "Answer"
repeat: 3
min_pass_rate: 1.0
cases:
  - name: lenient case
    repeat: 2
    min_pass_rate: 0.5
    assertions:
      - contains: good
  - name: strict case
    assertions:
      - contains: good
"""
    suite = load_suite(_write(tmp_path, text))
    # lenient: good, bad (1/2 >= 0.5 passes); strict: 3x good (passes)
    report, provider = _run_with(
        monkeypatch, suite, ["good", "bad", "good", "good", "good"]
    )
    assert provider.calls == 5  # 2 + 3 trials
    lenient, strict = report.cases
    assert lenient.trials == 2 and lenient.min_pass_rate == pytest.approx(0.5)
    assert lenient.passed
    assert strict.trials == 3 and strict.min_pass_rate == 1.0
    assert strict.passed
    assert report.ok


def test_default_repeat_is_one_trial(tmp_path):
    text = """\
name: plain
prompt: "Answer"
mock:
  default: "good"
cases:
  - name: once
    assertions:
      - contains: good
"""
    report = run_suite(load_suite(_write(tmp_path, text)), mock=True)
    case = report.cases[0]
    assert case.trials == 1 and case.passed_trials == 1
    assert case.pass_rate == 1.0 and case.min_pass_rate == 1.0
    assert report.ok


def test_provider_error_counts_as_failed_trial(tmp_path, monkeypatch):
    class BoomProvider(CyclingProvider):
        def complete(self, prompt, *, case_mock_response=None):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("provider exploded")
            return Completion(text="good", prompt_tokens=1, completion_tokens=1)

    provider = BoomProvider(["good"])
    monkeypatch.setattr(
        runner_module, "make_provider", lambda s, *, mock: provider
    )
    text = FLAKY_SUITE.format(suite_repeat=3, suite_rate="0.66")
    report = run_suite(load_suite(_write(tmp_path, text)), mock=True)
    case = report.cases[0]
    assert case.passed_trials == 2  # trial 2 errored, trials 1 and 3 passed
    assert case.passed
    assert "provider exploded" in case.error


def test_repeat_applies_to_multi_turn_cases(tmp_path, monkeypatch):
    text = """\
name: chatty-repeat
prompt: "You are helpful."
repeat: 2
min_pass_rate: 0.5
cases:
  - name: convo
    turns:
      - prompt: "hi"
        assertions:
          - contains: good
"""
    suite = load_suite(_write(tmp_path, text))
    report, provider = _run_with(monkeypatch, suite, ["good", "bad"])
    case = report.cases[0]
    assert provider.calls == 2
    assert case.trials == 2 and case.passed_trials == 1
    assert case.passed  # 0.5 >= 0.5


def test_record_mode_records_first_trial_and_diffs_later_trials(
    tmp_path, monkeypatch
):
    text = """\
name: rec
prompt: "Answer"
repeat: 2
cases:
  - name: snap case
    snapshot: true
    assertions:
      - contains: good
"""
    path = _write(tmp_path, text)

    def run_record(replies):
        provider = CyclingProvider(replies)
        monkeypatch.setattr(
            runner_module, "make_provider", lambda s, *, mock: provider
        )
        return run_suite(load_suite(path), mock=True, record=True)

    recorded = run_record(["good one", "good one"])
    assert recorded.cases[0].passed
    assert recorded.cases[0].response == "good one"
    import json

    golden = tmp_path / "suite.golden.json"
    assert json.loads(golden.read_text(encoding="utf-8")) == {
        "snap case": "good one"
    }

    # Drifting second trial fails the snapshot diff, so the case's rate
    # drops to 1/2 and (no gate set) the case fails.
    drifted = run_record(["good one", "good two"])
    assert not drifted.cases[0].passed
    assert drifted.cases[0].passed_trials == 1


@pytest.mark.parametrize(
    "line",
    [
        "repeat: 0",
        "repeat: -1",
        "repeat: 1.5",
        "repeat: two",
        "repeat: true",
        "min_pass_rate: -0.1",
        "min_pass_rate: 1.5",
        "min_pass_rate: high",
    ],
)
def test_suite_level_validation(tmp_path, line):
    text = f"name: bad\nprompt: 'x'\n{line}\ncases:\n  - name: c\n"
    with pytest.raises(PromptProofError):
        load_suite(_write(tmp_path, text))


@pytest.mark.parametrize(
    "line",
    ["repeat: 0", "repeat: 2.5", "min_pass_rate: 2", "min_pass_rate: -1"],
)
def test_case_level_validation(tmp_path, line):
    text = f"name: bad\nprompt: 'x'\ncases:\n  - name: c\n    {line}\n"
    with pytest.raises(PromptProofError):
        load_suite(_write(tmp_path, text))


def test_valid_boundary_values_load(tmp_path):
    text = """\
name: boundaries
prompt: "x"
repeat: 1
min_pass_rate: 0
cases:
  - name: c
    repeat: 5
    min_pass_rate: 1
"""
    suite = load_suite(_write(tmp_path, text))
    assert suite.repeat == 1 and suite.min_pass_rate == 0.0
    assert suite.cases[0].repeat == 5 and suite.cases[0].min_pass_rate == 1.0


def test_json_report_includes_trial_fields(tmp_path, monkeypatch):
    from promptproof.report import report_to_dict

    text = FLAKY_SUITE.format(suite_repeat=3, suite_rate="0.66")
    suite = load_suite(_write(tmp_path, text))
    report, _ = _run_with(monkeypatch, suite, ["good", "bad", "good"])
    case = report_to_dict(report)["cases"][0]
    assert case["trials"] == 3
    assert case["passed_trials"] == 2
    assert case["pass_rate"] == pytest.approx(2 / 3, abs=1e-3)
    assert case["min_pass_rate"] == pytest.approx(0.66)


def test_console_shows_pass_rate_for_repeat_cases(tmp_path, monkeypatch):
    from promptproof.report import render_console

    text = FLAKY_SUITE.format(suite_repeat=3, suite_rate="0.66")
    suite = load_suite(_write(tmp_path, text))
    report, _ = _run_with(monkeypatch, suite, ["good", "bad", "good"])
    out = render_console(report)
    assert "pass rate: 0.67 (2/3 trials, required 0.66)" in out


def test_repeat_example_passes_with_mock():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    suite = load_suite(root / "examples" / "repeat.yaml")
    report = run_suite(suite, mock=True)
    assert report.ok
    assert all(c.trials > 1 for c in report.cases)
