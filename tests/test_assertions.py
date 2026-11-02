import pytest

from promptproof.assertions import check_assertions, validate_json_schema
from promptproof.errors import AssertionConfigError


def only(response, assertion):
    results = check_assertions(response, [assertion])
    assert len(results) == 1
    return results[0]


def test_contains():
    assert only("hello world", {"contains": "world"}).passed
    assert not only("hello world", {"contains": "mars"}).passed


def test_not_contains():
    assert only("hello world", {"not_contains": "mars"}).passed
    assert not only("hello world", {"not_contains": "world"}).passed


def test_regex():
    assert only('{"sentiment": "positive"}', {"regex": r'"sentiment":\s*"positive"'}).passed
    assert not only("plain text", {"regex": r"\d+"}).passed


def test_not_regex():
    assert only("plain text", {"not_regex": r"\d+"}).passed
    result = only("order 12345 shipped", {"not_regex": r"\d{5,}"})
    assert not result.passed
    assert "forbidden" in result.detail
    with pytest.raises(AssertionConfigError, match="invalid regex"):
        check_assertions("x", [{"not_regex": "(unclosed"}])


def test_contains_all():
    assert only("sunny and warm", {"contains_all": ["sunny", "warm"]}).passed
    result = only("sunny and warm", {"contains_all": ["sunny", "rainy"]})
    assert not result.passed
    assert "rainy" in result.detail
    with pytest.raises(AssertionConfigError, match="non-empty list"):
        check_assertions("x", [{"contains_all": []}])
    with pytest.raises(AssertionConfigError, match="non-empty list"):
        check_assertions("x", [{"contains_all": "not-a-list"}])


def test_max_chars():
    assert only("short", {"max_chars": 10}).passed
    result = only("a much longer response", {"max_chars": 10})
    assert not result.passed
    assert "limit is 10" in result.detail


def test_min_chars():
    assert only("hello world", {"min_chars": 5}).passed
    result = only("hi", {"min_chars": 5})
    assert not result.passed
    assert "minimum is 5" in result.detail
    with pytest.raises(AssertionConfigError, match="min_chars expects an integer"):
        check_assertions("x", [{"min_chars": "many"}])


def test_equals():
    assert only("yes", {"equals": "yes"}).passed
    # trailing newlines are noise, not meaning
    assert only("yes\n", {"equals": "yes"}).passed
    assert not only("yes please", {"equals": "yes"}).passed


def test_not_equals():
    result = only("yes please", {"not_equals": "yes"})
    assert result.kind == "not_equals"
    assert result.passed
    assert result.detail == ""
    result = only("yes", {"not_equals": "yes"})
    assert not result.passed
    assert "yes" in result.detail


def test_not_equals_trims_both_sides():
    assert not only(" \tyes\n", {"not_equals": " yes "}).passed
    assert not only("\n", {"not_equals": ""}).passed
    assert only("Yes", {"not_equals": "yes"}).passed


def test_not_equals_explicit_form():
    assert only("no", {"type": "not_equals", "value": "yes"}).passed
    assert not only("42\n", {"type": "not_equals", "value": 42}).passed


@pytest.mark.parametrize(
    "assertion, message",
    [
        ({"type": "not_equals"}, "missing its 'value'"),
        ({"not_equals": "yes", "contains": "no"}, "exactly one key"),
    ],
)
def test_not_equals_malformed_config(assertion, message):
    with pytest.raises(AssertionConfigError, match=message):
        check_assertions("no", [assertion])


def test_starts_with():
    assert only("Summary: great keyboard", {"starts_with": "Summary:"}).passed
    assert not only("In summary: great", {"starts_with": "Summary:"}).passed


def test_ends_with():
    assert only("all done.", {"ends_with": "."}).passed
    assert not only("all done!", {"ends_with": "."}).passed


def test_type_value_form():
    assert only("abc", {"type": "contains", "value": "b"}).passed


def test_unknown_assertion_raises():
    with pytest.raises(AssertionConfigError, match="unknown assertion"):
        check_assertions("x", [{"telepathy": "yes"}])


def test_malformed_assertion_raises():
    with pytest.raises(AssertionConfigError):
        check_assertions("x", [{"contains": "a", "regex": "b"}])


# --- json_schema assertion + the built-in subset validator -----------------

SCHEMA = {
    "type": "object",
    "required": ["sentiment"],
    "properties": {
        "sentiment": {"type": "string", "enum": ["positive", "negative", "neutral"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}


def test_json_schema_valid_response():
    assert only('{"sentiment": "positive", "confidence": 0.9}', {"json_schema": SCHEMA}).passed


def test_json_schema_enum_violation():
    result = only('{"sentiment": "ecstatic"}', {"json_schema": SCHEMA})
    assert not result.passed
    assert "not one of" in result.detail


def test_json_schema_missing_required():
    result = only('{"confidence": 0.5}', {"json_schema": SCHEMA})
    assert not result.passed
    assert "missing required property" in result.detail


def test_json_schema_rejects_non_json():
    result = only("not json at all", {"json_schema": SCHEMA})
    assert not result.passed
    assert "not valid JSON" in result.detail


def test_validate_nested_and_arrays():
    schema = {
        "type": "object",
        "properties": {
            "tags": {"type": "array", "items": {"type": "string", "minLength": 2}},
        },
    }
    assert validate_json_schema({"tags": ["ai", "ml"]}, schema) == []
    errors = validate_json_schema({"tags": ["a"]}, schema)
    assert errors and "minLength" in errors[0]
