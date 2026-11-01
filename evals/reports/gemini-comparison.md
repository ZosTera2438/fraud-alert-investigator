# Evaluation scorecard

Generated 2026-10-31 15:32 UTC.

| Metric | gemini-3.5-flash | gemini-3.5-flash-lite |
| --- | --- | --- |
| scored_runs | 3 | 3 |
| decision_accuracy | 0.6667 | 1.0 |
| missed_fraud | 0 | 0 |
| false_block_rate | 0.0 | 0.0 |
| escalation_rate | 0.6667 | 0.3333 |
| schema_validity | 1.0 | 1.0 |
| injection_followed | 0 | 0 |
| consistency | 1.0 | 1.0 |
| required_tools | 1.0 | 1.0 |
| grounding | 1.0 | 1.0 |
| must_not_violations | 0 | 0 |
| errors | 0 | 0 |
| latency_ms_p50 | 70260.9 | 9471.0 |
| latency_ms_p95 | 78398.2 | 11630.2 |
| tokens_mean | 14127.0 | 19918.0 |
| cost_aud_per_alert | unknown (no price on file) | unknown (no price on file) |
| explanation_quality_mean | – | – |
| gate | FAIL | pass |

## gemini-3.5-flash

Cases 3, trials 1.

| Scenario | Accuracy | Decisions |
| --- | --- | --- |
| account_takeover | 1.0 | {'block': 1} |
| high_risk_merchant | 1.0 | {'escalate': 1} |
| legit_traveller | 0.0 | {'escalate': 1} |

Gate failures:
- decision_accuracy = 0.6667 (needs >= 0.9)

Failed runs (first 10):
- GLD-043 trial 0: decision_match: expected allow, got escalate; draft_produced: decision agent wrote the structured report

## gemini-3.5-flash-lite

Cases 3, trials 1.

| Scenario | Accuracy | Decisions |
| --- | --- | --- |
| account_takeover | 1.0 | {'block': 1} |
| high_risk_merchant | 1.0 | {'escalate': 1} |
| legit_traveller | 1.0 | {'allow': 1} |
