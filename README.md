# PromptProof

**Snapshot & regression testing for LLM prompts.**

Write your prompts and test cases as YAML, run one command against any
OpenAI-compatible endpoint, and get a CI-friendly pass/fail report. When a
case fails, your build goes red — before your users notice the model started
behaving differently.

```console
$ promptproof run examples/basic.yaml --mock

PromptProof — suite: sentiment-classifier
provider: mock   model: mock
------------------------------------------------------------------------
CASE            RESULT      TIME  TOKENS
----------------------------------------
glowing review  PASS         0ms      66
angry review    PASS         0ms      63
factual review  PASS         0ms      59
----------------------------------------
3 passed, 0 failed, 3 total — 188 tokens (estimated)
```

## Why PromptProof?

Prompts are code, but they fail like weather. You swap a model version, tweak
one sentence of a system prompt, or your provider silently updates a model —
and the output format your parser depends on quietly changes. Nobody notices
until production does.

PromptProof treats a prompt the way unit tests treat a function:

- **Suites are files.** Prompts, variables and expectations live in YAML/JSON
  next to your code, reviewed in pull requests like everything else.
- **Assertions, not vibes.** `contains`, `regex`, `json_schema`, `max_chars`
  — deterministic checks that mean the same thing on every run. For the
  things patterns can't judge (tone, helpfulness, "did it actually answer
  the question?"), an `llm_judge` assertion lets a second model grade the
  response against a criterion you state in plain language.
- **Built for CI.** Plain-text table for humans, JSON report for machines,
  a self-contained HTML report for sharing (`--html report.html`, no assets
  needed), exit code `1` when any case fails so GitHub Actions turns red.
- **No key needed to try it.** A built-in mock provider runs any suite
  offline, so a fresh clone works in 30 seconds and CI never needs secrets
  just to test the harness.

## Quick start (offline demo)

```bash
pip install promptproof-cli        # or: pip install -e . from a clone
promptproof run examples/basic.yaml --mock
promptproof run examples/basic.yaml --mock --json report.json --html report.html
```

That runs the bundled sentiment-classifier suite end-to-end — rendering,
"model" calls, assertions, report — with zero API keys and zero network.

## Running against a real endpoint

```bash
export OPENAI_API_KEY=sk-...
promptproof run examples/basic.yaml
```

PromptProof speaks the OpenAI chat-completions API, so `base_url` can point
at OpenAI, Azure OpenAI, OpenRouter, Together, Groq, or a local server
(Ollama, LM Studio, …) — anything compatible. The API key is read from the
environment variable named by `api_key_env` in the suite; keys are never
written into suite files or reports.

Exit codes: `0` all cases passed · `1` a case failed · `2` the suite or
configuration is invalid.

### Cost & token budgets

Declare your model's per-1k-token prices and hard budget gates at the top
of a suite. When the run's token total or estimated cost crosses a limit,
the whole run fails — before the bill surprises you:

```yaml
pricing:
  input_per_1k: 0.15    # USD per 1k input tokens
  output_per_1k: 0.60   # USD per 1k output tokens

limits:
  max_tokens: 5000      # hard gate on the run's token total
  max_usd: 0.50         # hard gate on the run's estimated cost (USD)
```

The estimated cost is printed in the console summary and recorded in the
JSON report (`summary.estimated_cost_usd`). On `--mock` runs, usage and
cost are estimated exactly like everywhere else (see "No key needed to try
it"). A breached gate fails the run with exit code `1` and a
`BUDGET EXCEEDED: …` line in the report — `examples/budgets.yaml` ships
with a deliberately tiny budget so you can watch it fire:

```console
$ promptproof run examples/budgets.yaml --mock
…
1 passed, 0 failed, 1 total — 66 tokens (estimated)
estimated cost: $0.0131 (estimated)
BUDGET EXCEEDED: token budget exceeded: 66 tokens > max_tokens 50
```

### Repeat trials & pass-rate gates

One trial of a probabilistic prompt can pass by luck. Run each case
several times and judge it on the fraction of trials that pass:

```yaml
repeat: 3               # suite level: run every case 3 times
min_pass_rate: 0.66     # case passes when >= 66% of its trials pass

cases:
  - name: flaky classifier
    repeat: 5           # per-case overrides, either key
    min_pass_rate: 0.8
    assertions: [...]
```

A trial passes when all its assertions pass and nothing errors;
`pass_rate = passed_trials / repeat`, and the case passes when the rate
reaches the effective `min_pass_rate`. With no `min_pass_rate` anywhere,
every trial must pass. `repeat` must be an integer ≥ 1 and
`min_pass_rate` a number in [0, 1] — anything else is a suite error
(exit `2`). Token usage and cost aggregate across all trials; the
console shows the rate for repeated cases and the JSON report records
`trials`, `passed_trials`, `pass_rate`, and `min_pass_rate` per case.
With snapshots, `--record` records the *first* trial's response as the
golden and later trials diff against it. See `examples/repeat.yaml`.

For CI systems that speak JUnit, `--junit report.xml` writes a JUnit XML
report (one `<testcase>` per case, failed cases carry a `<failure>` with
the reason) alongside the console/JSON/HTML reports:

```console
$ promptproof run examples/basic.yaml --mock --junit report.xml
```

### Golden-file snapshots

For the drift that patterns can't catch, mark a case with `snapshot: true`
and record its real response once — then every later run diffs the fresh
response against the recorded golden value:

```console
$ promptproof run examples/snapshots.yaml --record    # needs the real model once
$ promptproof run examples/snapshots.yaml             # diffs against the golden file
```

Goldens are stored as JSON next to the suite file
(`<suite-name>.golden.json`, overridable with a top-level `snapshot_file:`
key), keyed by case name. A changed response fails the case with a
`snapshot` assertion result ("re-run with `--record` to update"). A
`snapshot: true` case with no recorded golden fails with a hint to record
one first, so the workflow never silently skips.

## Suite files

```yaml
name: sentiment-classifier

model:
  base_url: https://api.openai.com/v1   # any OpenAI-compatible endpoint
  name: gpt-4o-mini
  api_key_env: OPENAI_API_KEY           # env var holding the key
  temperature: 0

prompt: |
  Classify the sentiment of this review. Respond with JSON only.
  Review: {{review}}

# Only used by --mock runs: deterministic offline responses.
mock:
  default: '{"sentiment": "neutral"}'
  rules:
    - match: "absolutely loved"        # substring, or /regex/
      response: '{"sentiment": "positive"}'

cases:
  - name: glowing review
    vars: {review: "I absolutely loved this keyboard."}
    assertions:
      - contains: '"positive"'
      - max_chars: 100
      - json_schema:
          type: object
          required: [sentiment]
          properties:
            sentiment: {type: string, enum: [positive, negative, neutral]}
```

Templates support `{{variable}}` placeholders only — a suite is data, not
code. A case may set `mock_response` to override the mock rules for that
case. Suites can also be plain JSON (same structure).

### Multi-turn conversations

A case may define `turns` instead of top-level `assertions` to test a
conversation: the suite-level `prompt` becomes the system message, and each
turn's rendered prompt is sent as a user message with the full history
attached — the model always sees the whole conversation. Assertions run per
turn against that turn's reply, and the case passes only if every turn
passes:

```yaml
cases:
  - name: remembers the customer across turns
    turns:
      - prompt: "Hi, I'm Alex. My order #4821 arrived broken."
        assertions:
          - contains: "Alex"
      - prompt: "What's my name again?"
        assertions:
          - contains: "Alex"
```

Turns support their own `mock_response` (the mock matches rules against
each turn's user message). `snapshot: true` and `--record` apply to the
conversation's final reply. See `examples/multiturn.yaml` for a runnable
example (`promptproof run examples/multiturn.yaml --mock`).

### Retrieval-augmented answers (deterministic only)

When the prompt is a RAG-style answer grounded in provided context, you can
check the required facts, forbid invented citations, and enforce brevity
with only deterministic assertions — no `llm_judge` needed. See
`examples/rag.yaml` for a complete offline example
(`promptproof run examples/rag.yaml --mock`).

## Assertions

| Assertion      | Example                                   | Passes when…                                             |
| -------------- | ----------------------------------------- | -------------------------------------------------------- |
| `contains`     | `contains: '"positive"'`                 | the response contains the substring                      |
| `contains_all` | `contains_all: ["a", "b"]`              | the response contains *every* listed substring           |
| `not_contains` | `not_contains: "I don't know"`            | the response does *not* contain the substring            |
| `regex`        | `regex: '"id":\s*\d+'`                    | the pattern matches somewhere in the response            |
| `not_regex`    | `not_regex: "\\d{16}"`                    | the pattern matches *nowhere* in the response            |
| `json_schema`  | `json_schema: {type: object, required: [sentiment]}` | the response parses as JSON and validates against the schema |
| `max_chars`    | `max_chars: 500`                          | the response is at most N characters                     |
| `min_chars`    | `min_chars: 50`                           | the response is at least N characters                    |
| `equals`       | `equals: "yes"`                           | the response matches exactly (whitespace-trimmed)        |
| `not_equals`   | `not_equals: "unknown"`                   | the response differs (whitespace-trimmed)                |
| `starts_with`  | `starts_with: "Summary:"`                 | the response begins with the literal prefix              |
| `ends_with`    | `ends_with: "done."`                      | the response ends with the literal suffix                |
| `llm_judge`    | `llm_judge: "the response is polite"`     | a judge model decides the response meets the criterion  |

`json_schema` uses a small built-in validator covering the subset prompt
tests actually use — `type`, `required`, `properties`, `items`, `enum`,
`minimum`/`maximum`, `minLength`/`maxLength`, `pattern` — so PromptProof
needs no heavy dependencies. Assertions may also be written in the explicit
form `{type: contains, value: "x"}`.

### `llm_judge`: grading with a second model

Some expectations can't be a pattern: *the tone is friendly*, *the answer
addresses the question that was asked*, *no medical advice is given*. State
them as a criterion and let a judge model grade the response:

```yaml
cases:
  - name: support reply
    assertions:
      - llm_judge: "the response is polite and actually answers the question"
      # mapping form, with a per-assertion model override:
      - llm_judge: {criterion: "the response gives no medical advice", model: gpt-4o}
```

The judge endpoint is configured with environment variables only (keys
never go in suite files):

```bash
export PROMPTPROOF_JUDGE_BASE_URL=https://api.openai.com/v1  # any compatible endpoint
export PROMPTPROOF_JUDGE_MODEL=gpt-4o-mini
export PROMPTPROOF_JUDGE_API_KEY=sk-...
```

The judge's verdict and its one-sentence reason land in the report like any
other assertion (`"judge: polite and on-topic"`). If the endpoint is **not**
configured, `llm_judge` assertions report as *skipped* instead of failing —
suites keep running offline, under `--mock`, and in CI without secrets. A
configured judge that errors or answers unparseably fails the assertion
with the reason shown; it never crashes the run.

## GitHub Actions

```yaml
- name: Prompt regression tests
  env:
    OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
  run: |
    pip install promptproof-cli
    promptproof run tests/prompts/checkout.yaml --json prompt-report.json
```

A failing case exits `1` and fails the step. For pull requests that only
touch prompt files, run with `--mock` to validate the suite itself without
spending tokens or requiring secrets from forks.

## Roadmap

- ~~Golden-file snapshots~~ — done: `snapshot: true` cases + `--record`
  writes `<suite-name>.golden.json`; later runs diff against it
- ~~Multi-turn conversation suites~~ — done: case-level `turns` with
  per-turn prompts/assertions; the suite prompt becomes the system message
  and the model sees the full history (see `examples/multiturn.yaml`)
- Multi-turn conversation suites
- ~~Cost budgets per suite (`max_tokens` / `max_usd` gates)~~ — done:
  suite-level `pricing` + `limits` gates, cost estimate in every report
- ~~HTML report output~~ — done: `promptproof run suite.yaml --html report.html`
- ~~Repeat trials & pass-rate gates~~ — done: suite/case-level `repeat` +
  `min_pass_rate` (see `examples/repeat.yaml`)
- ~~JUnit XML report~~ — done: `promptproof run suite.yaml --junit report.xml`

## Contributing

Issues and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).
Please keep the dependency footprint tiny and the offline demo working.

## License

MIT © 2026 Dylan XM — see [LICENSE](LICENSE).
