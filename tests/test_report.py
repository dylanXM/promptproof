import xml.etree.ElementTree as ET

from promptproof import __version__
from promptproof.assertions import AssertionResult
from promptproof.report import render_html, write_junit_report
from promptproof.runner import CaseResult, RunReport


def make_report():
    return RunReport(
        suite_name="demo-suite",
        provider="mock",
        model="mock",
        cases=[
            CaseResult(
                name="ok case",
                passed=True,
                duration_ms=12.3,
                prompt="Classify this",
                response="positive",
                assertions=[AssertionResult("contains", True)],
            ),
            CaseResult(
                name="bad <case>",
                passed=False,
                duration_ms=5.0,
                prompt="Say hi",
                response='<script>alert("xss")</script> and goodbye',
                assertions=[
                    AssertionResult("equals", False, "response does not exactly equal 'hi'"),
                    AssertionResult("llm_judge", True, "skipped: no judge", skipped=True),
                ],
            ),
        ],
    )


def test_html_is_self_contained_document():
    text = render_html(make_report())
    assert text.startswith("<!DOCTYPE html>")
    assert "<style>" in text and "</html>" in text
    assert "demo-suite" in text
    assert __version__ in text


def test_html_shows_case_results_and_stats():
    text = render_html(make_report())
    assert "ok case" in text
    assert "1 passed" in text and "1 failed" in text and "2 total" in text
    assert "FAIL" in text and "PASS" in text


def test_html_escapes_user_content():
    text = render_html(make_report())
    assert "&lt;script&gt;" in text
    assert "<script>alert" not in text
    assert "bad &lt;case&gt;" in text


def test_html_marks_skipped_assertions():
    text = render_html(make_report())
    assert "⊘ llm_judge" in text


def test_html_shows_prompt_and_response():
    text = render_html(make_report())
    assert "Classify this" in text
    assert "positive" in text


def test_junit_report_is_parseable_with_counts(tmp_path):
    path = tmp_path / "junit.xml"
    write_junit_report(make_report(), path)
    root = ET.parse(path).getroot()
    assert root.tag == "testsuites"
    assert root.attrib["tests"] == "2"
    assert root.attrib["failures"] == "1"
    assert "time" in root.attrib
    suite = root.find("testsuite")
    assert suite.attrib["name"] == "demo-suite"
    assert suite.attrib["tests"] == "2"
    assert suite.attrib["failures"] == "1"
    cases = suite.findall("testcase")
    assert len(cases) == 2
    assert all(c.attrib["classname"] == "demo-suite" for c in cases)
    assert cases[0].attrib["name"] == "ok case"
    assert cases[0].find("failure") is None


def test_junit_failure_element_states_assertion_reason(tmp_path):
    path = tmp_path / "junit.xml"
    write_junit_report(make_report(), path)
    cases = ET.parse(path).getroot().find("testsuite").findall("testcase")
    failure = cases[1].find("failure")
    assert failure is not None
    assert "equals" in failure.attrib["message"]
    assert "does not exactly equal" in failure.attrib["message"]


def test_junit_failure_message_states_pass_rate(tmp_path):
    report = RunReport(
        suite_name="flaky-suite",
        provider="stub",
        model="stub",
        cases=[
            CaseResult(
                name="flaky case",
                passed=False,
                duration_ms=3.0,
                prompt="p",
                response="r",
                trials=3,
                passed_trials=1,
                min_pass_rate=0.66,
            )
        ],
    )
    path = tmp_path / "junit.xml"
    write_junit_report(report, path)
    failure = (
        ET.parse(path).getroot().find("testsuite").find("testcase").find("failure")
    )
    assert failure is not None
    assert "0.33" in failure.attrib["message"]
    assert "0.66" in failure.attrib["message"]
    assert "1/3" in failure.attrib["message"]
