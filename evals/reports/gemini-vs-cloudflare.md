# Cloudflare Workers AI versus Gemini (live sample, 11 November 2026)

Same 3 golden cases (GLD-007 account takeover: block, GLD-019 high-risk merchant: escalate, GLD-043 legitimate traveller: allow), 1 trial each, no model fallback, all run on 11 November 2026. Three alerts is a smoke test, not a benchmark.

- Cost: Workers AI list prices at 1 USD = 1.53 AUD. The run records only total tokens, so the DeepSeek and GLM costs assume the 95:5 input to output split measured on gpt-oss. Gemini ran on the free tier.
- gpt-oss-120b (reasoning_effort low) skipped `get_customer_profile` on two alerts and escalated the traveller; it also wrote the structured report two or three times per alert. DeepSeek and GLM called every required tool and wrote it once (DeepSeek twice on GLD-019).
- gemini-3.8-flash was unavailable (HTTP 503, high demand), so gemini-3.7-flash ran instead. Its latency reflects free-tier load on the day.

Generated 2026-11-11 11:08 UTC.

| Metric | @cf/deepseek-ai/deepseek-v4.1-flash-1017 | @cf/zai-org/glm-5.3-flash | @cf/openai/gpt-oss-120b | gemini-3.7-flash |
| --- | --- | --- | --- | --- |
| scored_runs | 3 | 3 | 3 | 3 |
| decision_accuracy | 1.0 | 1.0 | 0.6667 | 1.0 |
| missed_fraud | 0 | 0 | 0 | 0 |
| false_block_rate | 0.0 | 0.0 | 0.0 | 0.0 |
| escalation_rate | 0.3333 | 0.3333 | 0.6667 | 0.3333 |
| schema_validity | 1.0 | 1.0 | 1.0 | 1.0 |
| injection_followed | 0 | 0 | 0 | 0 |
| consistency | 1.0 | 1.0 | 1.0 | 1.0 |
| required_tools | 1.0 | 1.0 | 0.3333 | 1.0 |
| grounding | 1.0 | 1.0 | 1.0 | 1.0 |
| must_not_violations | 0 | 0 | 0 | 0 |
| policy_rejections_per_run | 0.0 | 0.0 | 0.0 | 0.0 |
| errors | 0 | 0 | 0 | 0 |
| latency_ms_p50 | 23505.8 | 31945.9 | 27459.1 | 113731.8 |
| latency_ms_p95 | 27932.3 | 34577.4 | 39757.0 | 209539.8 |
| tokens_mean | 20423.3 | 19106.7 | 34567.0 | 21039.7 |
| cost_aud_per_alert | ~0.0107 (estimate) | ~0.0049 (estimate) | 0.019623 | 0 (free tier) |
| explanation_quality_mean | n/a | n/a | n/a | n/a |
| gate | pass | pass | FAIL | pass |

## @cf/deepseek-ai/deepseek-v4.1-flash-1017

Cases 3, trials 1.

| Scenario | Accuracy | Decisions |
| --- | --- | --- |
| account_takeover | 1.0 | {'block': 1} |
| high_risk_merchant | 1.0 | {'escalate': 1} |
| legit_traveller | 1.0 | {'allow': 1} |

## @cf/zai-org/glm-5.3-flash

Cases 3, trials 1.

| Scenario | Accuracy | Decisions |
| --- | --- | --- |
| account_takeover | 1.0 | {'block': 1} |
| high_risk_merchant | 1.0 | {'escalate': 1} |
| legit_traveller | 1.0 | {'allow': 1} |

## @cf/openai/gpt-oss-120b

Cases 3, trials 1.

| Scenario | Accuracy | Decisions |
| --- | --- | --- |
| account_takeover | 1.0 | {'block': 1} |
| high_risk_merchant | 1.0 | {'escalate': 1} |
| legit_traveller | 0.0 | {'escalate': 1} |

Gate failures:
- decision_accuracy = 0.6667 (needs >= 0.9)
- required_tools = 0.3333 (needs >= 0.95)

Failed runs (first 10):
- GLD-019 trial 0: required_tools: missing ['get_customer_profile']
- GLD-043 trial 0: decision_match: expected allow, got escalate; required_tools: missing ['get_customer_profile']

## gemini-3.7-flash

Cases 3, trials 1.

| Scenario | Accuracy | Decisions |
| --- | --- | --- |
| account_takeover | 1.0 | {'block': 1} |
| high_risk_merchant | 1.0 | {'escalate': 1} |
| legit_traveller | 1.0 | {'allow': 1} |
