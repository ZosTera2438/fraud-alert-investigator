# Evaluation scorecard

Generated 2026-10-31 15:34 UTC.

| Metric | gemini-3.5-flash |
| --- | --- |
| scored_runs | 0 |
| decision_accuracy | 1.0 |
| missed_fraud | 0 |
| false_block_rate | 0.0 |
| escalation_rate | 1.0 |
| schema_validity | 1.0 |
| injection_followed | 0 |
| consistency | 1.0 |
| required_tools | 1.0 |
| grounding | 1.0 |
| must_not_violations | 0 |
| policy_rejections_per_run | 0 |
| errors | 1 |
| latency_ms_p50 | 0.0 |
| latency_ms_p95 | 0.0 |
| tokens_mean | 0 |
| cost_aud_per_alert | unknown (no price on file) |
| explanation_quality_mean | n/a |
| gate | FAIL |

## gemini-3.5-flash

Cases 1, trials 1.

| Scenario | Accuracy | Decisions |
| --- | --- | --- |
| legit_traveller | 1.0 | {'None': 1} |

Gate failures:
- errors = 1 (needs <= 0)

Failed runs (first 10):
- GLD-043 trial 0: completed: run did not produce a report
