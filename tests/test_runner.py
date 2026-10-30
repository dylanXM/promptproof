from pathlib import Path

import pytest

from promptproof.errors import PromptProofError
from promptproof.runner import run_suite
from promptproof.suite import load_suite

ROOT = Path(__file__).resolve().parent.parent


def test_basic_example_passes_with_mock():
    suite = load_suite(ROOT / "examples" / "basic.yaml")
    report = run_suite(suite, mock=True)
    assert report.ok, [(c.name, [a.detail for a in c.assertions]) for c in report.cases]
    assert report.passed_count == 3
    # Mock usage must be honestly flagged as estimated.
    assert report.usage_estimated is True
    assert report.total_tokens > 0


def test_headlines_example_uses_case_mock_responses():
    suite = load_suite(ROOT / "examples" / "headlines.yaml")
    report = run_suite(suite, mock=True)
    assert report.ok
    assert report.cases[0].response.startswith("Dark mode")


def test_failing_assertion_fails_the_case_not_the_run(tmp_path):
    suite_file = tmp_path / "suite.yaml"
    suite_file.write_text(
        """
name: will-fail
prompt: "Say something about {{topic}}"
mock:
  default: "bananas"
cases:
  - name: impossible
    vars: {topic: fruit}
    assertions:
      - contains: oranges
""",
        encoding="utf-8",
    )
    report = run_suite(load_suite(suite_file), mock=True)
    assert not report.ok
    assert report.failed_count == 1
    assert "oranges" in report.cases[0].assertions[0].detail


def test_json_suite_files_are_supported(tmp_path):
    suite_file = tmp_path / "suite.json"
    suite_file.write_text(
        '{"name": "json-suite", "prompt": "Hi {{who}}",'
        ' "mock": {"default": "hello"},'
        ' "cases": [{"name": "c1", "vars": {"who": "you"},'
        ' "assertions": [{"contains": "hello"}]}]}',
        encoding="utf-8",
    )
    report = run_suite(load_suite(suite_file), mock=True)
    assert report.ok


def test_not_equals_from_yaml_suite(tmp_path):
    suite_file = tmp_path / "not-equals.yaml"
    suite_file.write_text(
        """
name: not-equals
prompt: "Answer the question"
cases:
  - name: different answer
    mock_response: "yes please"
    assertions:
      - not_equals: "yes"
  - name: same answer
    mock_response: " yes "
    assertions:
      - type: not_equals
        value: "yes"
""",
        encoding="utf-8",
    )
    report = run_suite(load_suite(suite_file), mock=True)
    assert report.passed_count == 1
    assert report.failed_count == 1
    assert not report.ok
    assert [case.assertions[0].kind for case in report.cases] == [
        "not_equals", "not_equals"
    ]


def test_mock_rule_regex_matching(tmp_path):
    suite_file = tmp_path / "suite.yaml"
    suite_file.write_text(
        """
name: regex-rules
prompt: "Order number {{order}}"
mock:
  default: "unknown"
  rules:
    - match: "/Order number \\\\d+/"
      response: "found"
cases:
  - name: has-number
    vars: {order: "12345"}
    assertions:
      - contains: found
""",
        encoding="utf-8",
    )
    report = run_suite(load_suite(suite_file), mock=True)
    assert report.ok


def test_missing_api_key_is_a_clean_error(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    suite_file = tmp_path / "suite.yaml"
    suite_file.write_text(
        """
name: needs-key
prompt: "Hi"
cases:
  - name: c1
    assertions:
      - contains: hi
""",
        encoding="utf-8",
    )
    report = run_suite(load_suite(suite_file), mock=False)
    assert not report.ok
    assert "OPENAI_API_KEY" in report.cases[0].error


def test_missing_suite_file_raises(tmp_path):
    with pytest.raises(PromptProofError, match="not found"):
        load_suite(tmp_path / "nope.yaml")
