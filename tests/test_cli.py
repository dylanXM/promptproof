import json
from pathlib import Path

import pytest

from promptproof import __version__
from promptproof.cli import main

ROOT = Path(__file__).resolve().parent.parent


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == f"promptproof {__version__}"


def test_run_mock_example_exits_zero(capsys):
    code = main(["run", str(ROOT / "examples" / "basic.yaml"), "--mock"])
    assert code == 0
    out = capsys.readouterr().out
    assert "3 passed, 0 failed" in out


def test_run_writes_json_report(tmp_path, capsys):
    report_path = tmp_path / "report.json"
    code = main(
        ["run", str(ROOT / "examples" / "basic.yaml"), "--mock", "--json", str(report_path)]
    )
    assert code == 0
    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert data["suite"] == "sentiment-classifier"
    assert data["summary"]["passed"] == 3
    assert data["summary"]["usage_estimated"] is True
    assert data["cases"][0]["assertions"], "per-assertion detail must be in the report"


def test_run_writes_html_report(tmp_path):
    report_path = tmp_path / "report.html"
    code = main(
        ["run", str(ROOT / "examples" / "basic.yaml"), "--mock", "--html", str(report_path)]
    )
    assert code == 0
    text = report_path.read_text(encoding="utf-8")
    assert text.startswith("<!DOCTYPE html>")
    assert "sentiment-classifier" in text
    assert "glowing review" in text
    assert __version__ in text


def test_run_writes_junit_report(tmp_path, capsys):
    import xml.etree.ElementTree as ET

    report_path = tmp_path / "report.xml"
    code = main(
        ["run", str(ROOT / "examples" / "basic.yaml"), "--mock", "--junit", str(report_path)]
    )
    assert code == 0
    assert "JUnit report written to" in capsys.readouterr().out
    root = ET.parse(report_path).getroot()
    assert root.tag == "testsuites"
    assert root.attrib["tests"] == "3"
    assert root.attrib["failures"] == "0"
    suite = root.find("testsuite")
    assert suite.attrib["name"] == "sentiment-classifier"
    assert len(suite.findall("testcase")) == 3


def test_invalid_repeat_exits_two(tmp_path, capsys):
    suite_file = tmp_path / "bad.yaml"
    suite_file.write_text(
        "name: bad\nprompt: 'x'\nrepeat: 0\ncases:\n  - name: c\n",
        encoding="utf-8",
    )
    code = main(["run", str(suite_file), "--mock"])
    assert code == 2
    assert "repeat" in capsys.readouterr().err


def test_run_missing_file_exits_two(capsys):
    code = main(["run", "does-not-exist.yaml", "--mock"])
    assert code == 2
    assert "error" in capsys.readouterr().err
