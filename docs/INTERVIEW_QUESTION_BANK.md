# Interview Question Bank: Fraud Alert Investigator

Questions grouped by topic. Each has the project evidence to point at and the points a strong answer covers. Section numbers refer to [PLAN.md](PLAN.md).

Difficulty: **Core** (expect in every loop), **Deep** (senior or follow-up), **Curveball** (tests judgement).

---

## 1. Project walkthrough and product judgement

**1.1 Walk me through the project in two minutes.** Core
- Evidence: runtime flow diagram, demo script (section 15).
- Strong answer: problem (alert backlog, analyst time), flow from alert to decision, what the model does versus code, human approval for block, how quality is measured, one number from the scorecard.

**1.2 Why use an agent here at all? Why not rules or a single model call?** Core
- Evidence: design decision 1 (section 3).
- Strong answer: real-time scoring stays deterministic; the agent is used after an alert, where evidence gathering varies by case (which tools, how far back, which playbook). Single call fails when the needed evidence differs per alert.

**1.3 Which features did you leave out and why?** Core
- Evidence: scope (section 2).
- Strong answer: no real-time authorisation, no real data, no model training; prioritised evaluation and safety over interface polish.

**1.4 What does success look like for the business?** Core
- Strong answer: analyst minutes per alert, missed fraud, false blocks (customer friction and complaints), escalation rate, time to decision. Missed fraud and false blocks have different costs, so they are tracked separately.

**1.5 How would you roll this out to real analysts?** Deep
- Strong answer: shadow (agent decides, analyst acts, compare), assist (agent drafts, analyst approves all), auto for low-risk allow on a small slice, widen only when metrics hold. One flag returns everything to assist.

**1.6 When would you turn the agent off?** Curveball
- Strong answer: rising missed fraud, rising disagreement with analysts, tool outage, cost spike, model provider change. Fail safe is escalate, so the queue grows but no fraud is allowed.

---

## 2. Agent fundamentals and Strands

**2.1 What is an agent loop? Write one without a framework.** Core
- Evidence: `baseline/raw_agent_loop.py` (section 12).
- Strong answer: send messages and tool schemas, if the model returns tool calls run them and append results, repeat until a final answer or the step limit. Include max steps, error handling and structured final output.

**2.2 What does Strands give you over your raw loop?** Core
- Strong answer: model provider abstraction, tool schema from type hints and docstrings, hooks, interrupts, session managers, conversation management, Graph and Swarm, OpenTelemetry tracing, structured output with Pydantic.

**2.3 How does the `@tool` decorator build the schema the model sees?** Deep
- Strong answer: function name, docstring and type hints become the tool specification. Docstring quality directly affects tool selection; vague descriptions cause wrong calls.

**2.4 How does structured output work, and what if the model returns invalid output?** Core
- Evidence: `InvestigationReport` (section 7).
- Strong answer: Pydantic model passed as `structured_output_model`; Strands forces a final structured response and validates it. On failure, one retry with the validation error, then escalate.

**2.5 How did you switch between Gemini and Ollama?** Core
- Evidence: `models.py` factory, `settings.yaml`.
- Strong answer: same agent code, provider chosen by configuration; differences in tool calling reliability measured in evaluation.

**2.6 How do you manage context window growth?** Deep
- Strong answer: compact tool results (summaries plus evidence IDs), sliding window or summarising conversation manager, cap transaction history days, store raw results outside the prompt.

**2.7 What are hooks used for in your project?** Core
- Evidence: hooks table (section 7).
- Strong answer: redaction, budget, policy guard, approval, audit. Hooks run outside the model, so policy is enforced in code.

**2.8 How does a Strands interrupt work end to end?** Deep
- Strong answer: hook calls `event.interrupt(name, reason)`, the agent stops with `stop_reason == "interrupt"`, the caller collects the analyst answer, then resumes with an `interruptResponse` keyed by interrupt ID. Session persistence lets the answer arrive hours later.

---

## 3. Tool design and Model Context Protocol

**3.1 How did you design the tools?** Core
- Evidence: tools list (section 7).
- Strong answer: few tools, one job each, typed inputs and outputs, evidence IDs on results, errors written for the model ("not found, do not retry").

**3.2 Show a tool description you rewrote after the model misused it.** Curveball
- Strong answer: a real example from testing, for example the model requesting 365 days of history; fixed with a maximum and a description of when to use longer windows.

**3.3 Why serve tools over Model Context Protocol instead of plain functions?** Deep
- Strong answer: tools become reusable by other agents and clients, separate process and permissions, standard discovery. Cost is extra latency and a process to run.

**3.4 What would you never expose over Model Context Protocol?** Deep
- Strong answer: raw card numbers, unrestricted SQL, write actions without a server-side policy check.

**3.5 When would you use Agent-to-Agent protocol instead?** Deep
- Strong answer: Model Context Protocol gives an agent a capability; Agent-to-Agent delegates a goal to another team's agent with its own reasoning (for example a chargeback agent). Inside one application, a function call is enough.

**3.6 How do you handle a tool returning a 500 halfway through?** Core
- Evidence: reliability (section 9).
- Strong answer: retry transient errors with backoff, timeout, typed error to the model, and if evidence stays missing the scoring treats it as unknown and the run escalates.

---

## 4. Orchestration and multi-agent design

**4.1 Why two agents instead of one with all tools?** Core
- Evidence: comparison 2 (section 12).
- Strong answer: different permissions (read versus decide), different prompts, separate evaluation signals. Backed by measured accuracy, cost and injection results from the single-agent baseline.

**4.2 Why a Graph rather than a Swarm?** Core
- Strong answer: order is known, audit trail matters, deterministic nodes mix with agent nodes. Swarm suits open-ended collaboration where handoffs are not known in advance.

**4.3 How do you stop infinite loops?** Core
- Evidence: `BudgetHook`, single retry edge.
- Strong answer: tool call cap, token cap, one retry edge with a counter, repeated identical tool calls detected, every limit ends in escalate.

**4.4 Where would you add a reviewer agent, and would it help?** Curveball
- Strong answer: a reviewer using the same model and prompt tends to agree with itself. A deterministic grounding check catches more for less cost; a reviewer is justified only with a different model or rubric and measured benefit.

**4.5 How would you parallelise investigation?** Deep
- Strong answer: parallel tool calls within the investigator; Graph branches for independent evidence. Python Graph runs in batches, so a slow branch delays the next batch.

---

## 5. Risk scoring and fraud domain

**5.1 Why is the risk score deterministic?** Core
- Strong answer: explainable, auditable, stable across runs, cannot be changed by injected text, and tunable by fraud analysts through configuration.

**5.2 How did you choose the weights and thresholds?** Core
- Strong answer: from common fraud signals, tuned on labelled synthetic cases; in production they would be tuned on labelled history with fraud operations, and versioned.

**5.3 What is the cost of a false positive versus a false negative here?** Core
- Strong answer: false block means a declined customer, complaint and churn; missed fraud means direct loss and chargeback. Thresholds set to keep missed fraud near zero while watching false blocks and escalation volume.

**5.4 How would you handle class imbalance and label delay?** Deep
- Strong answer: fraud is rare and labels (chargebacks) arrive weeks later. Use precision and recall at the operating threshold rather than accuracy, stratified evaluation sets, and delayed-label backfills.

**5.5 What are card testing, account takeover and first-party fraud, and how does each show up in your data?** Core
- Evidence: seeded scenarios (section 5).

**5.6 How do you explain a decision to an analyst or a regulator?** Core
- Strong answer: score breakdown with fired signals, each finding citing an evidence ID, audit log of every tool call and approval.

**5.7 How would the machine learning model and the rules work together?** Deep
- Strong answer: weighted blend with both parts reported; rules give explainability and hard stops, the model catches patterns the rules miss. Monitor drift on both.

**5.8 What happens when evidence conflicts, such as a known device with very high velocity?** Curveball
- Strong answer: score lands in the escalate band; the report lists both the risk and mitigating findings. The agent does not resolve the conflict by guessing.

---

## 6. Guardrails and security

**6.1 How do you defend against prompt injection?** Core
- Evidence: merchant description injection case, `<tool_data>` wrapping (section 7).
- Strong answer: tool data treated as untrusted, delimited, never in the system prompt; score computed from raw data in code; policy enforced in hooks; injection cases in the evaluation set with zero tolerance.

**6.2 Where does the permission check live?** Core
- Strong answer: in hooks and the action layer, outside the model. The model can ask; code decides.

**6.3 How do you protect personal data when calling Gemini?** Core
- Strong answer: synthetic data only, masking hook before model calls, card tokens, redaction before trace export. Note Gemini free-tier data use terms.

**6.4 What stops the agent from blocking a card on its own?** Core
- Strong answer: `submit_decision` is the only write path, guarded by policy guard and approval interrupt; actions are idempotent and logged.

**6.5 How would an attacker try to abuse this system?** Curveball
- Strong answer: injected merchant text, crafted transaction descriptions, flooding alerts to raise cost, forcing escalations to overload analysts. Mitigations: injection controls, budgets, rate limits, alert deduplication.

**6.6 How do you validate tool inputs the model generates?** Core
- Strong answer: Pydantic validation, allowlisted identifiers, bounded numbers (days at most 90), rejection with a typed error.

**6.7 What goes in memory and what is thrown away?** Deep
- Strong answer: per-alert session state persists until the case closes; long-term case memory (stretch) stores analyst-confirmed outcomes only, with a retention period, never the model's own unverified conclusions.

**6.8 Which OWASP risks for large language model applications apply here?** Deep
- Strong answer: prompt injection, sensitive information disclosure, excessive agency, improper output handling, unbounded consumption. Map each to a control.

---

## 7. Reliability and state

**7.1 The process dies at step 7 of 12. What happens?** Core
- Evidence: session manager, idempotency (section 9).
- Strong answer: session state reloads and the run resumes; any action already taken is found by idempotency key and not repeated.

**7.2 How do you make actions idempotent?** Core
- Strong answer: idempotency key from alert, decision and version; action layer checks the case table before acting; replay returns the existing receipt.

**7.3 What happens if Gemini is rate-limited or down?** Core
- Strong answer: retry with backoff, then fall back to Ollama with the switch recorded in the trace; if both fail, escalate.

**7.4 Which errors do you retry and which do you not?** Deep
- Strong answer: retry timeouts and 5xx; do not retry validation errors, not-found or permission errors.

**7.5 How do you keep runs reproducible for debugging?** Deep
- Strong answer: seeded data, fixtures for tools, prompt and configuration versions in the trace, recorded model responses for continuous integration.

**7.6 How do you handle an analyst who answers the approval a day later?** Deep
- Strong answer: interrupt state persisted in the session; alert marked awaiting approval; timeout policy escalates to a senior queue.

---

## 8. Evaluation

**8.1 How do you know it works?** Core
- Evidence: scorecard (section 11).
- Strong answer: labelled golden set, red-team set, deterministic graders first, model judge only for explanation quality, repeated trials, release gate.

**8.2 How do you evaluate the trajectory and not just the final answer?** Core
- Strong answer: required and forbidden tools, number of calls, grounding of cited evidence IDs, budget breaches. A correct answer reached through a forbidden path fails.

**8.3 Models are non-deterministic. How do you account for that?** Core
- Strong answer: 3 trials per case, report consistency and pass rate, low temperature, track variance across model versions.

**8.4 How do you trust a model judge?** Deep
- Strong answer: specific rubric, calibrated against hand-labelled reports, agreement measured, judge from a different model family where possible, judge used only where code cannot check.

**8.5 What metrics do you report and why those?** Core
- Strong answer: accuracy, missed fraud, false block rate, escalation rate, schema validity, injection resistance, consistency, p95 latency, cost per alert.

**8.6 How do evaluations run in continuous integration without a live model?** Deep
- Strong answer: recorded model responses replayed on pull requests for deterministic checks; nightly live runs for real model quality.

**8.7 How does a production failure become a test?** Deep
- Strong answer: inspect trace, categorise the failure, add the case to the golden or red-team set, fix, rerun the gate.

**8.8 How did Gemini and Ollama compare?** Core
- Strong answer: give measured numbers for accuracy, schema validity, latency and cost, and the decision taken from them.

---

## 9. Observability, cost and latency

**9.1 What do you trace?** Core
- Strong answer: one trace per alert with spans for graph nodes, model calls (tokens, latency, finish reason), tool calls (status, duration), hook decisions and approvals; personal data redacted.

**9.2 Median latency doubled after a release. How do you find out why?** Core
- Strong answer: compare traces before and after: step count, tool latency, retries, context size, model version.

**9.3 What stops a runaway loop from spending a lot of money?** Core
- Strong answer: tool call and token caps per alert, cost alerts, cheaper `aux_model` for side calls, deterministic code instead of model steps where the path is known.

**9.4 How would you cut cost per alert by half?** Deep
- Strong answer: smaller model for investigation, larger only for the report; compact tool outputs; cache merchant profiles; skip the agent for alerts that rules already decide with high confidence.

**9.5 What would you alert on in production?** Deep
- Strong answer: tool error rate, budget breaches, approval queue age, escalation spike, disagreement with analysts, latency percentiles, cost per alert.

**9.6 What is the latency budget and where does the time go?** Deep
- Strong answer: target from the scorecard; most time in model calls, then tools; parallel tool calls and fewer model turns cut it most.

---

## 10. Production and governance

**10.1 What is versioned together for a release?** Deep
- Strong answer: model, prompts, tool schemas and scoring configuration pinned as one release unit with its evaluation results.

**10.2 Who owns the thresholds?** Deep
- Strong answer: fraud operations owns `scoring.yaml`; engineering owns the code; changes go through review and the evaluation gate.

**10.3 How would you deploy this?** Deep
- Strong answer: containerised API and tool server, managed runtime (Cloud Run or Bedrock AgentCore), secrets manager, private network to data sources, tracing backend.

**10.4 How would you scale to 50,000 alerts a day?** Curveball
- Strong answer: queue-based workers, rules shortcut for clear cases, batching, caching, rate-limit aware model routing, cost per alert monitoring.

**10.5 How would you handle model upgrades?** Deep
- Strong answer: run the full evaluation on the new model, compare scorecards, shadow traffic, then switch with rollback.

---

## 11. Coding round prompts

1. Write an agent loop with tool calling and a step limit, no framework.
2. Implement `get_transaction_history` with Pydantic input validation and a typed error.
3. Write the impossible-travel signal (haversine distance divided by time difference).
4. Write a hook that cancels a tool call when a budget is exceeded.
5. Write a deterministic grader that checks every cited evidence ID exists in the trace.
6. Make `block_card` idempotent.

---

## 12. Behavioural

**12.1 What broke first, and how did you find it?** Core
- Strong answer: a real failure from building (for example the local model skipping the device tool), how the trace showed it, the fix, the test added.

**12.2 Tell me about a trade-off you made.** Core
- Strong answer: deterministic score over model-produced score, with the accuracy and explainability numbers behind it.

**12.3 What would you do differently?** Core
- Strong answer: one honest limitation, for example synthetic data hides real distribution shift, and the plan to address it.

**12.4 How did you decide what to build first?** Core
- Strong answer: end-to-end thin slice first, then evaluation before features, so every later change was measured.
