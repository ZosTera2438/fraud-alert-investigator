# 0006. Offline scripted model for CI; Cloudflare Workers AI live; Gemini for comparison

Status: accepted (2026-10-31); revised 2026-11-11: the live model moved to Cloudflare Workers AI and Ollama was dropped. Live comparisons are small samples.

## Context

Continuous integration must run the full agent pipeline on every pull request without an API key, network access or flaky model output. Live models are compared separately. The plan paired Gemini with a local Ollama model. Ollama was dropped: it needs a local GPU-backed server, and a hosted open-weight model on Workers AI gives the same "not Google" comparison with no setup. Gemini's free tier (about 20 requests per model per day, often HTTP 503 under load) is too small to run the project on, so it is kept for comparisons only.

## Decision

- `scripted_model.py` is a Strands `Model` with a fixed policy. The real Agent loop, hooks, interrupts, Graph and structured output all run against it. Behaviours (`lazy`, `injection_follower`, `always_allow`) simulate model failures so the red-team set can test the controls. Its accuracy measures the harness and controls, not a language model.
- `RecordingModel` and `ReplayModel` capture live responses so CI can rerun them offline (`run_evals.py --record`, then `--provider replay`).
- Live runs use Cloudflare `@cf/deepseek-ai/deepseek-v4.1-flash-1017` with `@cf/zai-org/glm-5.3-flash` (another vendor) as the fallback for rate limits, outages and HTTP 5xx (see the selection below). Until 2026-11-11 the live model was `gemini-3.5-flash` with `gemini-3.5-flash-lite` as fallback. Evaluation runs switch the fallback off so each scorecard measures one model.
- The explanation judge runs on GLM-5.3 Flash, a different model family from the agent.
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

## Cloudflare Workers AI and the live model choice (11 November 2026)

The `cloudflare` provider runs open-weight models on Workers AI through its OpenAI-compatible endpoint (Strands `OpenAIModel`). The free allocation is 10,000 neurons a day.

Making it work needed three request fixes in `models.py` (`CloudflareModel`), each found from a live HTTP 400: Workers AI rejects `"tools": []`, rejects message content sent as a list of text parts once the conversation has tool turns, and requires a `content` field on assistant tool-call turns. `max_tokens` is 4096 because reasoning models spend part of it on hidden reasoning; at 2048 gpt-oss ran out mid-report and failed safe to escalate.

Selection: 22 Workers AI text models support tool calling. Five were screened with a one-tool agent plus structured output (gpt-oss-120b, DeepSeek V4.1 Flash, GLM-5.3 Flash, Gemma 4 26B, Qwen 3.8 27B; all passed). The three most capable at or below gpt-oss's price then ran the same three alerts as the October Gemini sample, against gemini-3.7-flash on the free tier (gemini-3.8-flash returned HTTP 503 for high demand). Larger models (Kimi K2.6, GLM-5.3, DeepSeek V4 Pro) cost about four times as much per token and would spend the daily allocation in one or two alerts.

```bash
uv run python evals/run_evals.py --model cloudflare:@cf/deepseek-ai/deepseek-v4.1-flash-1017     --model cloudflare:@cf/zai-org/glm-5.3-flash --model gemini:gemini-3.7-flash     --cases GLD-007,GLD-019,GLD-043 --trials 1
```

| Model | Correct | Required tools | Latency p50 | Tokens per alert | AUD per alert |
| --- | --- | --- | --- | --- | --- |
| DeepSeek V4.1 Flash | 3 of 3 | 3 of 3 | 23.5 s | 20,400 | about 0.011 |
| GLM-5.3 Flash | 3 of 3 | 3 of 3 | 31.9 s | 19,100 | about 0.005 |
| gpt-oss-120b (reasoning low) | 2 of 3 | 1 of 3 | 27.5 s | 34,600 | 0.020 |
| gemini-3.7-flash (free tier) | 3 of 3 | 3 of 3 | 113.7 s | 21,000 | free tier |

Full scorecard: [gemini-vs-cloudflare.md](../../evals/reports/gemini-vs-cloudflare.md). None of the four missed fraud or blocked a good customer, and every citation was grounded.

Findings:

- DeepSeek V4.1 Flash is the live default: correct on all three, every required tool, fastest, and the structured report written once (twice on one alert). GLM-5.3 Flash is equally accurate at half the price but slower, so it is the fallback.
- gpt-oss-120b with `reasoning_effort: low` skipped `get_customer_profile` on two alerts and escalated a legitimate traveller. The deterministic score and the policy guard kept that safe, but it is the least thorough investigator, and it wrote the structured report two or three times per alert, which is why it used 1.7 times the tokens.
- Gemini 3.7 Flash matched the Cloudflare models on quality but took 98 to 210 seconds per alert on the free tier.
- Three alerts is a smoke test. On the free allocation DeepSeek costs about 630 neurons per alert, so about 15 alerts a day; the 78-case, 3-trial gate run needs a paid Workers plan (about 2.50 AUD at list price).
