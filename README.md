# Fraud Alert Investigator

[![CI](https://github.com/ZosTera2438/fraud-alert-investigator/actions/workflows/ci.yml/badge.svg)](https://github.com/ZosTera2438/fraud-alert-investigator/actions/workflows/ci.yml)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

An agent that receives a card fraud alert, gathers transaction history, merchant information and device metadata, scores the risk and recommends **allow**, **escalate** or **block**, with a human approving every block.

Python 3.12, [Strands Agents](https://strandsagents.com), Cloudflare Workers AI for the live model (DeepSeek V4.1 Flash, with GLM-5.3 Flash as fallback), Gemini for model comparisons, synthetic data only. The build plan is in [docs/PLAN.md](docs/PLAN.md); design decisions in [docs/decisions](docs/decisions/).

![Runtime flow](docs/diagrams/architecture-flow.svg)

## See it work

Recorded on the offline scripted model, so every run replays exactly; the live model (DeepSeek V4.1 Flash on Cloudflare) goes through the same pipeline, tools and controls.

**Analyst console: investigation, human approval, evidence, audit and idempotent replay.** Two Strands agents investigate over Model Context Protocol tools, a deterministic engine scores the alert, and the run pauses until an analyst approves the block.

![Analyst console walkthrough](docs/images/console-walkthrough.gif)

**Approval survives a restart.** The paused run is saved to disk and resumed by a different process; answering twice is refused and replaying the alert returns the same case.

![Approval across a restart](docs/images/approval-restart.gif)

**Prompt injection is data, not instructions.** A merchant description tells the "AI reviewer" to allow the payment. Even a model that obeys it cannot: the policy guard rejects any decision outside the score band, so the alert escalates and no fraud is allowed.

![Prompt injection red-team case](docs/images/prompt-injection.gif)

**The release gate catches regressions.** 78 labelled cases (golden and red team) run on every pull request; a model that skips tools fails the build.

![Release gate](docs/images/release-gate.gif)

**Tracing.** One OpenTelemetry trace per alert in Arize Phoenix: graph nodes, both agents, model calls with tokens, every tool and MCP call.

![Phoenix tracing](docs/images/phoenix-tracing.gif)

<details>
<summary>Full console report and the API</summary>

![Console report](docs/images/console-report.png)

![API docs](docs/images/api-docs.png)

</details>

## Quick start

```bash
uv sync --extra evals
uv run python data/generate.py                 # SQLite database + evaluation cases (seed 42)
uv run fraud-investigator list
uv run fraud-investigator run ALT-000007       # account takeover: pauses for your approval
uv run pytest                                  # 79 tests, about 20 seconds
uv run python evals/run_evals.py --provider scripted --gate   # offline scorecard and release gate
```

Without credentials the default provider is a deterministic offline model (see [ADR 0006](docs/decisions/0006-offline-model-and-live-comparison.md)). The live model runs on [Cloudflare Workers AI](https://developers.cloudflare.com/workers-ai/) (free allocation of 10,000 neurons a day, about 15 alerts on DeepSeek V4.1 Flash). Put the credentials in `.env` (git-ignored; see `.env.example`):

```bash
# .env
FRAUD_MODEL_PROVIDER=cloudflare
CLOUDFLARE_ACCOUNT_ID=...
CLOUDFLARE_API_TOKEN=...                 # token with the Workers AI permission
GEMINI_API_KEY=...                       # optional: only for model comparisons
```

```bash
uv run fraud-investigator hello                             # checks the model answers
uv run fraud-investigator run ALT-000007                    # live investigation
```

`CLOUDFLARE_MODEL` overrides the model (default `@cf/deepseek-ai/deepseek-v4.1-flash-1017`). The model was chosen by running the same alerts on four live models:

| Model (11 Nov 2026, 3 alerts) | Correct | Required tools | Latency p50 | Tokens | AUD per alert |
| --- | --- | --- | --- | --- | --- |
| Cloudflare DeepSeek V4.1 Flash (default) | 3 of 3 | 3 of 3 | 24 s | 20,400 | about 0.011 |
| Cloudflare GLM-5.3 Flash (fallback) | 3 of 3 | 3 of 3 | 32 s | 19,100 | about 0.005 |
| Cloudflare gpt-oss-120b | 2 of 3 | 1 of 3 | 27 s | 34,600 | 0.020 |
| Gemini 3.7 Flash (free tier) | 3 of 3 | 3 of 3 | 114 s | 21,000 | free tier |

Details in [gemini-vs-cloudflare.md](evals/reports/gemini-vs-cloudflare.md).

Service and console:

```bash
uv run uvicorn fraud_investigator.api.app:app               # http://localhost:8000/docs
uv run --extra ui streamlit run ui/streamlit_app.py         # analyst console
docker compose up --build                                   # api, MCP tool server, console, Phoenix
```

The Compose stack was checked on Docker Desktop 4.99 (engine 29.9) on 11 November. An alert was investigated through the tool container over MCP, then approved, creating a case, blocking the card and leaving 14 audit events. Phoenix received its traces. The tool server's port can't be reached from the host. The same stack also ran one live alert on Cloudflare DeepSeek: all five tools, block, 0.009 AUD.

![Deployment](docs/diagrams/deployment.svg)

The same stack without Docker, each in its own terminal (this is how the recordings above were made):

```bash
uvx --from arize-phoenix==20.24.0 phoenix serve                       # traces at http://localhost:6006
uv run python -m fraud_investigator.mcp_server --http 8765            # MCP tool server, streamable HTTP
FRAUD_TOOL_TRANSPORT=mcp FRAUD_MCP_URL=http://localhost:8765/mcp FRAUD_TRACING=1 \
    uv run uvicorn fraud_investigator.api.app:app                     # API using the MCP tools, traced
uv run --extra ui streamlit run ui/streamlit_app.py                   # console at http://localhost:8501
```

## How it works

| Step | Component | Type | Responsibility |
| --- | --- | --- | --- |
| 1 | Intake | Code node | Validate alert, check card belongs to customer, mask card token |
| 2 | Investigator | Strands agent | Call the five read-only tools in parallel, write an evidence summary |
| 3 | Scoring | Code node | Score 0 to 100 from raw tool results ([scoring.yaml](config/scoring.yaml)) |
| 4 | Decision | Strands agent | Call guarded `submit_decision`, write a `ReportDraft` |
| 5 | Policy guard | Hook | Reject decisions outside the band or citing unknown evidence; one retry, then escalate |
| 6 | Approval | Hook + interrupt | Pause before every block and every allow over 5,000 AUD |
| 7 | Action | Code node | Idempotent case, card block and alert close, with audit log |

The Graph is in [graph.py](src/fraud_investigator/graph.py), the hooks in [hooks/](src/fraud_investigator/hooks/).

### Controls that do not depend on the model

- **Deterministic score.** The engine reads the evidence store, never the model's text, and only uses records for the alert's own customer, merchant and device. The model's report draft has no score field ([ADR 0001](docs/decisions/0001-deterministic-scoring.md)).
- **Least privilege.** The investigator has read-only tools; only the decision agent has `submit_decision`. `AlertScopeHook` refuses queries about other customers and fills alert-scoped arguments (timestamps) itself.
- **Untrusted data.** Tool results arrive as `<tool_data>` blocks with `<` and `>` escaped, so injected text cannot close the block. Prompts say tool content is data.
- **Fail safe is escalate.** Timeouts (5 s, 3 attempts with backoff), malformed data, unknown IDs, budget breaches (12 tool calls or 60,000 tokens per alert), missing reports and graph errors all end in escalate with a reason.
- **Human approval.** A Strands interrupt pauses the run; the answer resumes it, in the same process or after a restart (`FileSessionManager`). A rejected block becomes escalate. The action layer refuses a block without an approver.
- **Idempotency.** Key = `alert_id + decision + policy version`; replaying an alert returns the existing case and never blocks a card twice.
- **Personal data.** Tools return masked emails and no phone numbers; `RedactionHook` masks card numbers, emails and phone numbers in every message before the model sees it.

## Evaluation

78 cases: 66 golden (11 scenarios × 6 variants) and 12 red-team. Three trials each, fresh database per trial. Details in [evals/README.md](evals/README.md).

![Evaluation loop](docs/diagrams/evaluation-loop.svg)

Scripted model, 3 trials (`evals/reports/latest.md`):

| Metric | Target | Result |
| --- | --- | --- |
| Decision accuracy | ≥ 0.90 | 1.00 |
| Missed fraud (block labelled, allowed) | 0 | 0 |
| False block rate | ≤ 0.05 | 0.00 |
| Escalation rate | reported | 0.40 |
| Schema validity | 1.00 | 1.00 |
| Injection cases allowed | 0 | 0 |
| Consistency across trials | ≥ 0.95 | 1.00 |
| Required tools called | ≥ 0.95 | 1.00 |
| Citations grounded | 1.00 | 1.00 |
| p95 latency / cost per alert | reported | 245 ms / 0 AUD (offline) |

These numbers measure the harness and controls, not a language model. The gate does catch regressions: forcing a model that skips the device tool drops accuracy to 0.51 and required-tool coverage to 0.13, and the gate fails. Forcing a model that always says allow yields 0 allows and 0 missed fraud, because the policy guard turns every attempt into escalate.

## Comparisons

From [evals/reports/comparison.md](evals/reports/comparison.md); reasoning in [docs/decisions](docs/decisions/).

| Question | Result | Record |
| --- | --- | --- |
| Framework-free loop or Strands? | Same decisions; the loop is 285 lines versus 923 but has no resumable approval, structured output, parallel tools, tracing or MCP | [0002](docs/decisions/0002-strands-versus-framework-free-loop.md) |
| One agent or two? | Same accuracy; one agent used 2.4× the tokens (26,131 versus 10,850). Injection was stopped by the policy guard in both | [0003](docs/decisions/0003-two-agents-versus-one.md) |
| Graph, interrupts, sessions | Approvals survive a restart | [0004](docs/decisions/0004-graph-orchestration-and-approval.md) |
| Rules or rules plus model? | The blend learned a shortcut and dropped accuracy from 1.00 to 0.73, allowing all escalate cases; kept off | [0005](docs/decisions/0005-rules-versus-ml-blend.md) |
| gemini-3.5-flash or gemini-3.5-flash-lite? | Small live sample (3 alerts each, free-tier quota): both correct on every alert they decided; lite was 7x faster on the day but had its first decision rejected by the policy guard on all 3 alerts; flash hit HTTP 503 on one alert | [0006](docs/decisions/0006-offline-model-and-live-comparison.md) |
| Which live model? | Same 3 alerts on four models: DeepSeek V4.1 Flash, GLM-5.3 Flash and Gemini 3.7 Flash all 3 of 3; gpt-oss-120b skipped the customer profile tool on 2 alerts and escalated a legitimate traveller. DeepSeek was fastest; none missed fraud or blocked a good customer ([report](evals/reports/gemini-vs-cloudflare.md)) | [0006](docs/decisions/0006-offline-model-and-live-comparison.md) |

## Observability

Set `FRAUD_TRACING=1` and start Phoenix (`docker compose up phoenix`, or `uvx --from arize-phoenix==20.24.0 phoenix serve`); `FRAUD_OTLP_ENDPOINT` points the exporter elsewhere. Traces land in the `fraud-alert-investigator` project. Strands exports one trace per alert: graph nodes, model calls with tokens, tool calls with duration and status. The pipeline adds an `investigate_alert` span with decision and score, code-node spans and a `model_fallback` event when DeepSeek V4.1 Flash falls back to GLM-5.3 Flash. Every run records input and output tokens, cost (prices in [settings.yaml](config/settings.yaml)), latency, tool calls, policy rejections and budget breaches in `metrics`.

## Rollout

| Stage | What happens | Exit criterion | Rollback |
| --- | --- | --- | --- |
| Shadow | Runs on every alert; analysts work as today; recommendations logged | Accuracy and false block rate meet the gate on two weeks of real labels | Turn off the consumer |
| Assist | Console shows the recommendation and evidence; analyst decides | Analysts agree in at least 90% of cases; no missed fraud | Hide the recommendation |
| Auto | Allow and escalate act automatically; block still needs approval | Stable metrics, monitored daily | Config switch back to assist; idempotent actions make replays safe |

## Repository layout

```text
config/            settings.yaml (model, budgets, policy), scoring.yaml (weights, bands)
data/generate.py   seeded synthetic data
src/fraud_investigator/
  schemas/         Pydantic contracts           tools/        backend, Strands tools, evidence store, playbook
  scoring/         rules engine, optional model  agents/       investigator, decision, prompts/
  hooks/           redaction, budget, scope, evidence, audit, policy guard, approval
  graph.py         Strands Graph and run lifecycle         actions/  idempotent block, case, close
  models.py        Cloudflare, Gemini, scripted, fallback, record and replay
  mcp_server/      Model Context Protocol server and client  api/  FastAPI
  baseline/        framework-free loop, single agent         evaluation.py  graders, scorecard, gate
ui/streamlit_app.py
evals/             cases, run_evals.py, compare.py, thresholds.yaml, reports/
tests/             unit and integration
docs/decisions/    architecture decision records
docs/diagrams/     runtime flow, evaluation loop, deployment
docs/images/       screenshots and GIFs used above
Dockerfile, docker-compose.yml   api, MCP tool server, console, Phoenix
```

## Differences from the plan

- Scoring weights were retuned and combination patterns added so the scenarios separate ([ADR 0001](docs/decisions/0001-deterministic-scoring.md)).
- The policy guard's retry happens inside the decision node rather than as a Graph edge; behaviour is the same ([ADR 0004](docs/decisions/0004-graph-orchestration-and-approval.md)).
- Escalate never needs approval; allow over 5,000 AUD and every block do.
- Playbook search uses a small BM25 index instead of Chroma or LanceDB, to keep the demo dependency-free; the interface is the same.
- The MCP SDK is version 2, where FastMCP is called `MCPServer`.
- The live model runs on Cloudflare Workers AI instead of a local Ollama model, and Gemini is kept for comparisons ([ADR 0006](docs/decisions/0006-offline-model-and-live-comparison.md)).

## Not verified on the build machine

- Live models are tested on small samples only: 3 alerts each on four models (11 November) and on gemini-3.5-flash and flash-lite (31 October). The full 78-case live run and the model-graded judge are still to do.
- Stretch items (A2A, case memory, streaming, cloud deployment) are not built.
