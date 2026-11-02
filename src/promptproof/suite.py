"""Suite file loading (YAML or JSON).

A suite file looks like::

    name: sentiment-classifier
    model:
      base_url: https://api.openai.com/v1
      name: gpt-4o-mini
      api_key_env: OPENAI_API_KEY
    prompt: |
      Classify the sentiment of this review: {{review}}
    mock:
      default: '{"sentiment": "neutral"}'
      rules:
        - match: "loved"
          response: '{"sentiment": "positive"}'
    cases:
      - name: glowing review
        vars: {review: "I loved it"}
        assertions:
          - contains: positive

A case may instead define ``turns`` for a multi-turn conversation: each turn
is a mapping with ``prompt`` (required, rendered with the case's ``vars``),
optional ``assertions`` (checked against that turn's reply), and optional
``mock_response``. For multi-turn cases the suite-level ``prompt`` becomes
the conversation's system message, and every turn's reply is appended to
the history sent to the model::

    cases:
      - name: remembers the customer name
        turns:
          - prompt: "Hi, I'm Alex. My order #4821 arrived broken."
            mock_response: "I'm sorry, Alex! Let me check order #4821."
            assertions:
              - contains: Alex
          - prompt: "What's my name again?"
            mock_response: "Your name is Alex."
            assertions:
              - contains: Alex
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import PromptProofError
from .providers import MockConfig


@dataclass
class ModelConfig:
    base_url: str = "https://api.openai.com/v1"
    name: str = "gpt-4o-mini"
    api_key_env: str = "OPENAI_API_KEY"
    temperature: float = 0.0


@dataclass
class Pricing:
    """Per-1k-token prices in USD for cost estimation."""

    input_per_1k: float = 0.0
    output_per_1k: float = 0.0


@dataclass
class Limits:
    """Hard run budgets: the run fails when a budget is exceeded."""

    max_tokens: int | None = None
    max_usd: float | None = None


@dataclass
class Case:
    name: str
    vars: dict[str, Any] = field(default_factory=dict)
    assertions: list[Any] = field(default_factory=list)
    mock_response: str | None = None
    snapshot: bool = False
    #: Repeat-trial overrides; None means "inherit the suite-level value".
    repeat: int | None = None
    min_pass_rate: float | None = None
    #: Multi-turn conversation turns. Each turn is a mapping with ``prompt``
    #: (required), ``assertions`` (optional, checked against that turn's
    #: reply) and ``mock_response`` (optional). Empty for single-turn cases.
    turns: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Suite:
    name: str
    prompt: str
    model: ModelConfig = field(default_factory=ModelConfig)
    mock: MockConfig = field(default_factory=MockConfig)
    cases: list[Case] = field(default_factory=list)
    pricing: Pricing = field(default_factory=Pricing)
    limits: Limits = field(default_factory=Limits)
    #: Repeat trials per case (default 1) and the pass-rate gate a case
    #: must reach across its trials; None gate means "every trial must pass".
    repeat: int = 1
    min_pass_rate: float | None = None
    #: Path to the golden file for snapshot cases; defaults to
    #: ``<suite-stem>.golden.json`` next to the suite file when None.
    snapshot_file: str | None = None
    source: str = ""


def _load_raw(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - dependency declared
            raise PromptProofError("PyYAML is required to read YAML suites") from exc
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise PromptProofError(f"suite file {path} must contain a mapping at the top level")
    return data


def _validate_repeat(value: Any, ref: str) -> int | None:
    """Validate a ``repeat`` value; None (absent) passes through."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PromptProofError(f"{ref}: 'repeat' must be an integer >= 1")
    return value


def _validate_min_pass_rate(value: Any, ref: str) -> float | None:
    """Validate a ``min_pass_rate`` value; None (absent) passes through."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PromptProofError(f"{ref}: 'min_pass_rate' must be a number in [0, 1]")
    rate = float(value)
    if not 0.0 <= rate <= 1.0:
        raise PromptProofError(f"{ref}: 'min_pass_rate' must be a number in [0, 1]")
    return rate


def _load_turns(raw_turns: Any, index: int, suite_path: Path) -> list[dict[str, Any]]:
    """Validate a case's ``turns`` list; returns ``[]`` when absent."""
    if raw_turns is None:
        return []
    case_ref = f"case #{index + 1} in {suite_path}"
    if not isinstance(raw_turns, list) or not raw_turns:
        raise PromptProofError(f"{case_ref}: 'turns' must be a non-empty list")
    turns: list[dict[str, Any]] = []
    for turn_index, raw_turn in enumerate(raw_turns):
        turn_ref = f"{case_ref}, turn #{turn_index + 1}"
        if not isinstance(raw_turn, dict):
            raise PromptProofError(f"{turn_ref} must be a mapping")
        prompt = raw_turn.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise PromptProofError(f"{turn_ref} is missing a non-empty 'prompt'")
        turns.append(
            {
                "prompt": prompt,
                "assertions": list(raw_turn.get("assertions", [])),
                "mock_response": (
                    str(raw_turn["mock_response"]) if "mock_response" in raw_turn else None
                ),
            }
        )
    return turns


def load_suite(path: str | Path) -> Suite:
    """Load and validate a suite file from *path*."""
    suite_path = Path(path)
    if not suite_path.exists():
        raise PromptProofError(f"suite file not found: {suite_path}")
    raw = _load_raw(suite_path)

    if "prompt" not in raw:
        raise PromptProofError(f"suite {suite_path} is missing the required 'prompt' field")
    raw_cases = raw.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise PromptProofError(f"suite {suite_path} must define at least one case")

    model_raw = raw.get("model") or {}
    model = ModelConfig(
        base_url=str(model_raw.get("base_url", ModelConfig.base_url)),
        name=str(model_raw.get("name", ModelConfig.name)),
        api_key_env=str(model_raw.get("api_key_env", ModelConfig.api_key_env)),
        temperature=float(model_raw.get("temperature", 0.0)),
    )

    mock_raw = raw.get("mock") or {}
    mock = MockConfig(
        default=str(mock_raw.get("default", "")),
        rules=[dict(rule) for rule in mock_raw.get("rules", [])],
    )

    pricing_raw = raw.get("pricing") or {}
    pricing = Pricing(
        input_per_1k=float(pricing_raw.get("input_per_1k", 0.0)),
        output_per_1k=float(pricing_raw.get("output_per_1k", 0.0)),
    )
    limits_raw = raw.get("limits") or {}
    limits = Limits(
        max_tokens=(
            int(limits_raw["max_tokens"]) if "max_tokens" in limits_raw else None
        ),
        max_usd=float(limits_raw["max_usd"]) if "max_usd" in limits_raw else None,
    )
    suite_repeat = _validate_repeat(raw.get("repeat"), f"suite {suite_path}")
    suite_min_pass_rate = _validate_min_pass_rate(
        raw.get("min_pass_rate"), f"suite {suite_path}"
    )

    cases: list[Case] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, dict):
            raise PromptProofError(f"case #{index + 1} in {suite_path} must be a mapping")
        turns = _load_turns(raw_case.get("turns"), index, suite_path)
        if turns and raw_case.get("assertions"):
            raise PromptProofError(
                f"case {raw_case.get('name', f'#{index + 1}')} in {suite_path}: "
                "'turns' and case-level 'assertions' cannot be combined — "
                "put assertions on the individual turns"
            )
        case_ref = f"case #{index + 1} in {suite_path}"
        cases.append(
            Case(
                name=str(raw_case.get("name", f"case-{index + 1}")),
                vars=dict(raw_case.get("vars", {})),
                assertions=list(raw_case.get("assertions", [])),
                mock_response=(
                    str(raw_case["mock_response"]) if "mock_response" in raw_case else None
                ),
                snapshot=bool(raw_case.get("snapshot", False)),
                turns=turns,
                repeat=_validate_repeat(raw_case.get("repeat"), case_ref),
                min_pass_rate=_validate_min_pass_rate(
                    raw_case.get("min_pass_rate"), case_ref
                ),
            )
        )

    return Suite(
        name=str(raw.get("name", suite_path.stem)),
        prompt=str(raw["prompt"]),
        model=model,
        mock=mock,
        cases=cases,
        pricing=pricing,
        limits=limits,
        repeat=suite_repeat if suite_repeat is not None else 1,
        min_pass_rate=suite_min_pass_rate,
        snapshot_file=(
            str(raw["snapshot_file"]) if "snapshot_file" in raw else None
        ),
        source=str(suite_path),
    )
