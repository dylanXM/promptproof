"""Assertions evaluated against a model's raw text response.

A suite lists assertions as single-key mappings, e.g.::

    assertions:
      - contains: "positive"
      - not_contains: "negative"
      - regex: '"sentiment":\\s*"positive"'
      - max_chars: 500
      - equals: "yes"
      - starts_with: "Summary:"
      - json_schema: {type: object, required: [sentiment]}
      - llm_judge: "the response is polite and answers the question"

``equals`` and ``not_equals`` compare after stripping leading/trailing
whitespace (models love trailing newlines); ``starts_with`` / ``ends_with``
match literally.

``not_regex`` is the mirror of ``regex``: it passes when the pattern does
*not* match anywhere in the response — handy for banning PII shapes,
stack-trace fragments, or forbidden phrasing.

``contains_all`` takes a list of substrings and passes only when every one
is present; the detail names the missing ones:

    assertions:
      - contains_all: ["positive", "confidence"]

``json_schema`` ships with a small built-in validator covering the subset of
JSON Schema that prompt tests actually use (``type``, ``required``,
``properties``, ``items``, ``enum``, ``minimum``/``maximum``,
``minLength``/``maxLength``, ``pattern``). That keeps PromptProof free of a
hard dependency on the ``jsonschema`` package; see the README for the exact
supported subset.

``llm_judge`` is the odd one out: it needs a second model, configured via
environment variables (see :mod:`promptproof.judge`). When no judge endpoint
is configured the assertion reports itself as *skipped* instead of failing,
so suites keep running offline and in CI without secrets.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .errors import AssertionConfigError
from .judge import LLMJudge


@dataclass
class AssertionResult:
    """Outcome of a single assertion check."""

    kind: str
    passed: bool
    detail: str = ""
    #: True when the check never ran (e.g. ``llm_judge`` with no judge
    #: endpoint configured). Skipped results count as passed so a suite
    #: stays green, but are flagged in reports.
    skipped: bool = False


# ---------------------------------------------------------------------------
# Minimal JSON Schema subset validator
# ---------------------------------------------------------------------------

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate_json_schema(instance: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Validate *instance* against the supported JSON Schema subset.

    Returns a list of human-readable error strings; empty means valid.
    """
    errors: list[str] = []

    expected_type = schema.get("type")
    if expected_type is not None:
        check = _TYPE_CHECKS.get(expected_type)
        if check is None:
            errors.append(f"{path}: unsupported schema type {expected_type!r}")
            return errors
        if not check(instance):
            errors.append(f"{path}: expected type {expected_type}, got {type(instance).__name__}")
            return errors  # deeper checks would only add noise

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} is not one of {schema['enum']!r}")

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required property {key!r}")
        for key, subschema in schema.get("properties", {}).items():
            if key in instance and isinstance(subschema, dict):
                errors.extend(validate_json_schema(instance[key], subschema, f"{path}.{key}"))

    if isinstance(instance, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(instance):
            errors.extend(validate_json_schema(item, schema["items"], f"{path}[{index}]"))

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: longer than maxLength {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append(f"{path}: does not match pattern {schema['pattern']!r}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: {instance} < minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: {instance} > maximum {schema['maximum']}")

    return errors


# ---------------------------------------------------------------------------
# Assertion checks
# ---------------------------------------------------------------------------


def _check_contains(response: str, expected: Any) -> AssertionResult:
    needle = str(expected)
    ok = needle in response
    return AssertionResult("contains", ok, "" if ok else f"response does not contain {needle!r}")


def _check_not_contains(response: str, expected: Any) -> AssertionResult:
    needle = str(expected)
    ok = needle not in response
    return AssertionResult("not_contains", ok, "" if ok else f"response contains forbidden {needle!r}")


def _check_not_regex(response: str, expected: Any) -> AssertionResult:
    pattern = str(expected)
    try:
        matched = re.search(pattern, response) is not None
    except re.error as exc:
        raise AssertionConfigError(f"invalid regex {pattern!r}: {exc}") from exc
    return AssertionResult(
        "not_regex", not matched, "" if not matched else f"response matches forbidden /{pattern}/"
    )


def _check_contains_all(response: str, expected: Any) -> AssertionResult:
    if not isinstance(expected, list) or not expected:
        raise AssertionConfigError(
            f"contains_all expects a non-empty list of substrings, got {expected!r}"
        )
    needles = [str(item) for item in expected]
    missing = [needle for needle in needles if needle not in response]
    return AssertionResult(
        "contains_all",
        not missing,
        "" if not missing else f"response is missing {missing!r}",
    )


def _check_regex(response: str, expected: Any) -> AssertionResult:
    pattern = str(expected)
    try:
        ok = re.search(pattern, response) is not None
    except re.error as exc:
        raise AssertionConfigError(f"invalid regex {pattern!r}: {exc}") from exc
    return AssertionResult("regex", ok, "" if ok else f"response does not match /{pattern}/")


def _check_max_chars(response: str, expected: Any) -> AssertionResult:
    try:
        limit = int(expected)
    except (TypeError, ValueError) as exc:
        raise AssertionConfigError(f"max_chars expects an integer, got {expected!r}") from exc
    ok = len(response) <= limit
    return AssertionResult(
        "max_chars", ok, "" if ok else f"response is {len(response)} chars, limit is {limit}"
    )


def _check_min_chars(response: str, expected: Any) -> AssertionResult:
    try:
        minimum = int(expected)
    except (TypeError, ValueError) as exc:
        raise AssertionConfigError(f"min_chars expects an integer, got {expected!r}") from exc
    ok = len(response) >= minimum
    return AssertionResult(
        "min_chars", ok, "" if ok else f"response is {len(response)} chars, minimum is {minimum}"
    )


def _check_equals(response: str, expected: Any) -> AssertionResult:
    wanted = str(expected).strip()
    ok = response.strip() == wanted
    return AssertionResult(
        "equals", ok, "" if ok else f"response does not exactly equal {wanted!r}"
    )


def _check_not_equals(response: str, expected: Any) -> AssertionResult:
    wanted = str(expected).strip()
    ok = response.strip() != wanted
    return AssertionResult(
        "not_equals", ok, "" if ok else f"response equals forbidden {wanted!r}"
    )


def _check_starts_with(response: str, expected: Any) -> AssertionResult:
    prefix = str(expected)
    ok = response.startswith(prefix)
    return AssertionResult(
        "starts_with", ok, "" if ok else f"response does not start with {prefix!r}"
    )


def _check_ends_with(response: str, expected: Any) -> AssertionResult:
    suffix = str(expected)
    ok = response.endswith(suffix)
    return AssertionResult(
        "ends_with", ok, "" if ok else f"response does not end with {suffix!r}"
    )


def _check_json_schema(response: str, expected: Any) -> AssertionResult:
    if not isinstance(expected, dict):
        raise AssertionConfigError("json_schema expects a mapping (schema object)")
    try:
        parsed = json.loads(response)
    except json.JSONDecodeError as exc:
        return AssertionResult("json_schema", False, f"response is not valid JSON: {exc.msg}")
    errors = validate_json_schema(parsed, expected)
    return AssertionResult("json_schema", not errors, "; ".join(errors))


def _check_llm_judge(response: str, expected: Any, judge: LLMJudge) -> AssertionResult:
    """Grade *response* with *judge* against a natural-language criterion.

    Accepts a plain string criterion, or a mapping with ``criterion`` plus
    optional per-assertion ``model`` / ``base_url`` overrides::

        - llm_judge: "the response is polite"
        - llm_judge: {criterion: "the response is polite", model: gpt-4o}
    """
    overrides: dict[str, str] = {}
    if isinstance(expected, dict):
        criterion = expected.get("criterion")
        if criterion is None:
            raise AssertionConfigError("llm_judge mapping form requires a 'criterion' key")
        for key in ("base_url", "model"):
            if key in expected:
                overrides[key] = str(expected[key])
    else:
        criterion = expected
    criterion = str(criterion).strip()
    if not criterion:
        raise AssertionConfigError("llm_judge expects a non-empty criterion")
    if overrides:
        judge = judge.with_overrides(**overrides)
    verdict = judge.evaluate(criterion, response)
    if verdict.skipped:
        return AssertionResult("llm_judge", True, f"skipped: {verdict.reason}", skipped=True)
    detail = f"judge: {verdict.reason}" if verdict.reason else ""
    return AssertionResult("llm_judge", verdict.passed, detail)


_CHECKS = {
    "contains": _check_contains,
    "contains_all": _check_contains_all,
    "not_contains": _check_not_contains,
    "regex": _check_regex,
    "not_regex": _check_not_regex,
    "max_chars": _check_max_chars,
    "min_chars": _check_min_chars,
    "equals": _check_equals,
    "not_equals": _check_not_equals,
    "starts_with": _check_starts_with,
    "ends_with": _check_ends_with,
    "json_schema": _check_json_schema,
}

#: Assertion kinds implemented in this version.
SUPPORTED_ASSERTIONS = tuple(_CHECKS) + ("llm_judge",)


def normalize_assertion(raw: Any) -> tuple[str, Any]:
    """Normalize one raw assertion entry to a ``(kind, expected)`` pair.

    Accepts either ``{contains: "x"}`` or ``{type: contains, value: "x"}``.
    """
    if not isinstance(raw, dict) or not raw:
        raise AssertionConfigError(f"assertion must be a non-empty mapping, got {raw!r}")
    if "type" in raw:
        kind = raw["type"]
        if "value" not in raw:
            raise AssertionConfigError(f"assertion {kind!r} is missing its 'value'")
        return str(kind), raw["value"]
    if len(raw) != 1:
        raise AssertionConfigError(
            f"assertion must have exactly one key, got {sorted(raw)}: {raw!r}"
        )
    kind, expected = next(iter(raw.items()))
    return str(kind), expected


def check_assertions(
    response: str,
    assertions: list[Any],
    *,
    judge: LLMJudge | None = None,
) -> list[AssertionResult]:
    """Run every assertion in *assertions* against *response*.

    *judge* supplies the backend for ``llm_judge`` assertions; when omitted,
    one is built from the environment (and skips gracefully if unconfigured).
    """
    results: list[AssertionResult] = []
    for raw in assertions:
        kind, expected = normalize_assertion(raw)
        if kind == "llm_judge":
            results.append(_check_llm_judge(response, expected, judge or LLMJudge.from_env()))
            continue
        check = _CHECKS.get(kind)
        if check is None:
            raise AssertionConfigError(
                f"unknown assertion {kind!r}; supported: {', '.join(SUPPORTED_ASSERTIONS)}"
            )
        results.append(check(response, expected))
    return results


@dataclass
class CaseAssertions:
    """Convenience bundle: all assertion results for one case."""

    results: list[AssertionResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)
