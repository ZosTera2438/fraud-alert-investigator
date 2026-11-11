# Comparisons

Generated 2026-11-11T03:39 UTC with the scripted model unless noted.

## Architectures (golden set, plus injection cases with an injection-following model)

| variant | loc | accuracy | escalation_rate | tool_calls_mean | tokens_mean | latency_ms_p50 | injection_follower_accuracy | injection_follower_allows | injection_follower_escalations |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| two agents (Strands Graph) | 923 | 1.0 | 0.3636 | 7.0 | 10850.1 | 120.3 | 0.0 | 0 | 10 |
| single agent (Strands) | 558 | 1.0 | 0.3636 | 8.0 | 26130.8 | 51.3 | 0.0 | 0 | 10 |
| framework-free loop | 285 | 1.0 | 0.3636 | 7.0 | 15343.4 | 8.6 | 0.0 | 0 | 10 |

## Rules only versus rules plus model

| variant | accuracy | false_blocks | missed_fraud | top_features |
| --- | --- | --- | --- | --- |
| rules only | 1.0 | 0 | 0 |  |
| rules 70% + model 30% | 0.7273 | 0 | 0 | {'geo_mismatch': 0.6944, 'trusted_device': 0.3056, 'amount_anomaly': 0.0} |

## Live providers

Not part of this run (pass `--providers cloudflare,gemini`). The live sample is in [gemini-vs-cloudflare.md](gemini-vs-cloudflare.md).
