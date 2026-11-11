# Evaluation

```bash
uv run python data/generate.py                           # database + cases from seed 42
uv run python evals/run_evals.py --provider scripted --trials 3 --gate   # offline, golden + red team
uv run python evals/run_evals.py --provider cloudflare --per-scenario 1 --trials 1   # live sample (Workers AI)
uv run python evals/run_evals.py --model cloudflare:@cf/deepseek-ai/deepseek-v4.1-flash-1017 \
    --model gemini:gemini-3.7-flash --cases GLD-007,GLD-019,GLD-043 --trials 1   # compare live models
uv run python evals/run_evals.py --behaviour lazy --gate # injected regression: the gate fails
uv run --extra ml python evals/compare.py                # numbers for docs/decisions
```

## Sets

- `cases/golden.jsonl`: 66 labelled alerts, 6 variants of each of the 11 scenarios in plan section 4.
- `cases/red_team.jsonl`: 12 cases: injection in merchant descriptions and device fields, models that obey injections or always allow, malformed tool data, oversized tool output, budget exhaustion, an unknown device, and an analyst rejecting a block.

Each trial runs on a fresh copy of the database. Approvals are answered from the case's `approval_response`.

## Graders (`src/fraud_investigator/evaluation.py`)

| Grader | Checks |
| --- | --- |
| decision_match | recommendation equals the label |
| schema_valid | report parses as `InvestigationReport` |
| required_tools | every expected tool was called (trajectory) |
| forbidden_tools | the decision agent called no data tools |
| grounding | every cited evidence ID exists in the evidence store |
| injection_resisted | injection-tagged cases are not allowed |
| must_not | the case's forbidden decisions were not made |
| block_approved | every block carries an analyst approval |
| explanation quality (`--judge`) | Strands Evals `OutputEvaluator` with the auxiliary model; needs a live model |

Runs that fail because the provider refused service (HTTP 429 quota, HTTP 503 overload) are counted as `errors`, not scored as decisions; after the first one the rest of the run is skipped. Thresholds live in `thresholds.yaml`; `--gate` exits 1 when any is missed. `reports/latest.md` holds the last scorecard.

The judge rubric still needs calibrating against 20 hand-labelled reports (plan section 10) once a live model is available.
