"""Command-line interface.

Usage::

    promptproof run suite.yaml            # real endpoint (needs API key)
    promptproof run suite.yaml --mock     # offline demo, no key needed
    promptproof run suite.yaml --json report.json --html report.html --junit report.xml
    promptproof --version

Exit codes: 0 = every case passed, 1 = at least one case failed,
2 = the suite or configuration itself is invalid.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import __version__
from .errors import PromptProofError
from .report import render_console, write_html_report, write_json_report, write_junit_report
from .runner import run_suite
from .suite import load_suite


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="promptproof",
        description="Snapshot & regression testing for LLM prompts.",
    )
    parser.add_argument("--version", action="version", version=f"promptproof {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    run = subparsers.add_parser("run", help="run a prompt test suite")
    run.add_argument("suite", help="path to a suite file (.yaml, .yml or .json)")
    run.add_argument(
        "--mock",
        action="store_true",
        help="use the built-in offline mock provider (no API key needed)",
    )
    run.add_argument(
        "--json",
        metavar="PATH",
        default=None,
        help="also write a machine-readable JSON report to PATH",
    )
    run.add_argument(
        "--html",
        metavar="PATH",
        default=None,
        help="also write a self-contained HTML report to PATH",
    )
    run.add_argument(
        "--junit",
        metavar="PATH",
        default=None,
        help="also write a JUnit XML report to PATH",
    )
    run.add_argument(
        "--record",
        action="store_true",
        help="record snapshot-enabled cases' responses into the golden file "
        "instead of diffing against it",
    )
    run.add_argument("--base-url", default=None, help="override the suite's model.base_url")
    run.add_argument("--model", default=None, help="override the suite's model.name")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command != "run":
        parser.print_help()
        return 2

    try:
        suite = load_suite(args.suite)
        if args.base_url:
            suite.model.base_url = args.base_url
        if args.model:
            suite.model.name = args.model
        report = run_suite(suite, mock=args.mock, record=args.record)
    except PromptProofError as exc:
        print(f"promptproof: error: {exc}", file=sys.stderr)
        return 2

    print(render_console(report))
    if args.record:
        print("Golden snapshots recorded.")
    if args.json:
        write_json_report(report, args.json)
        print(f"JSON report written to {args.json}")
    if args.html:
        write_html_report(report, args.html)
        print(f"HTML report written to {args.html}")
    if args.junit:
        write_junit_report(report, args.junit)
        print(f"JUnit report written to {args.junit}")
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
