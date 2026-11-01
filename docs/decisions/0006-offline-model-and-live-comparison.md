# 0006. Offline scripted model for CI; two Gemini models compared live

Status: accepted (2026-10-31); live comparison is a small first sample

## Context

Continuous integration must run the full agent pipeline on every pull request without an API key, network access or flaky model output. Live models are compared separately. The plan compared Gemini with Ollama; it now compares two Gemini models instead, so no local model server is needed.

## Decision

- `scripted_model.py` is a Strands `Model` with a fixed policy. The real Agent loop, hooks, interrupts, Graph and structured output all run against it. Behaviours (`lazy`, `injection_follower`, `always_allow`) simulate model failures so the red-team set can test the controls. Its accuracy measures the harness and controls, not a language model.
- `RecordingModel` and `ReplayModel` capture live responses so CI can rerun them offline (`run_evals.py --record`, then `--provider replay`).
- Live runs default to `gemini-3.5-flash` with `gemini-3.5-flash-lite` as the fallback for rate limits, outages and HTTP 5xx. Evaluation runs switch the fallback off so each scorecard measures one model.
- A run that fails safe because the provider refused service (HTTP 429 quota, HTTP 503 overload) is recorded as an infrastructure error, not as an escalate decision, and the gate fails on any error. The first live run showed why: quota failures had been scored as escalations and made the model look 50% accurate.

## Live comparison (31 October 2026)

```bash
uv run python evals/run_evals.py --model gemini:gemini-3.5-flash --model gemini:gemini-3.5-flash-lite \
    --cases GLD-007,GLD-019,GLD-043 --sets golden --trials 1 --report-name gemini-comparison
```

Three alerts, one per outcome: account takeover (block), high-risk merchant (escalate), legitimate traveller (allow). Only alerts the model actually decided are counted.

| Metric | gemini-3.5-flash | gemini-3.5-flash-lite |
| --- | --- | --- |
| Decided by the model | 2 of 3 (third: HTTP 503, twice) | 3 of 3 |
| Correct decisions | 2 of 2 | 3 of 3 |
| First `submit_decision` rejected by the policy guard | 0 of 2 | 3 of 3 (corrected on the one allowed retry) |
| Required tools called, citations grounded, schema valid | all | all |
| Latency per alert | 70 to 78 s (model under high demand) | 8.5 to 11.6 s |
| Tokens per alert | about 21,200 | 19,300 to 20,300 |
| Cost per alert | unknown: add prices to `config/settings.yaml` | unknown |

An earlier smoke run on `gemini-2.5-flash` decided two alerts correctly (allow and block) in about 21 s and 22,000 tokens each, about 0.022 AUD per alert.

## Findings

- Both models reached the right decision on every alert they finished, with all five tools called in parallel and every citation grounded.
- The lite model needed the policy guard every time (for example, it passed the wrong `alert_id`). The guard turned that into a corrected call, not a wrong action. This is the control doing its job, and it costs an extra model call per alert.
- Latency on `gemini-3.5-flash` was dominated by provider load on the day, so the 7x gap is not a stable property of the model.

## Limits

The free tier allows 20 requests per model per day and an alert takes about 5 to 6 requests, so this is 3 alerts per model, not the 78-case, 3-trial gate run. It shows the pipeline works on live Gemini; it does not establish accuracy. On a paid key, run the full sets:

```bash
uv run python evals/run_evals.py --model gemini:gemini-3.5-flash --model gemini:gemini-3.5-flash-lite \
    --trials 3 --workers 4 --judge --judge-model gemini-2.5-pro --report-name gemini-comparison --gate
```

`gemini-2.5-flash-lite` is no longer offered to new API users, which is why the lite model is from the 3.5 generation.
