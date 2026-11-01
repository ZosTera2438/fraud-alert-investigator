# 0002. Strands versus a framework-free loop

Status: accepted (2026-10-31)

## Context

Interviewers ask whether you understand the loop under the framework. `baseline/raw_agent_loop.py` implements the same investigation by hand: call the model with tool specs, run the requested tools, append results, repeat, with the budget, scope check, policy guard and approval written inline.

## Measurements

Golden set (66 cases), scripted model, one trial. Source: `evals/reports/comparison.md`.

| Variant | Non-blank lines | Accuracy | Tool calls | Mean tokens |
| --- | --- | --- | --- | --- |
| Two agents on Strands Graph | 923 (graph, agents, hooks, tool wrappers) | 1.0 | 7.0 | 10,850 |
| Framework-free loop | 285 | 1.0 | 7.0 | 15,343 |

Both reuse the backend, scoring engine, policy rules and actions, so the line counts compare the orchestration only.

## What the extra lines buy

| Capability | Strands | Framework-free loop |
| --- | --- | --- |
| Pause for approval and resume later, even in another process | Interrupts plus `FileSessionManager` | Synchronous callback only; a crash loses the run |
| Structured output with validation and a forced retry | `structured_output_model` | Not implemented |
| Parallel tool execution | Built in | Sequential |
| OpenTelemetry spans for model and tool calls | Built in | None |
| Hooks reusable across agents and the Graph | Yes | Checks are inline in `_dispatch` |
| Model Context Protocol tools | `MCPClient` | Not implemented |
| Provider switch (Gemini, Ollama) | Model classes | One hand-written Ollama adapter |

## Decision

Use Strands for the product. Keep the loop as a teaching baseline and a regression check that the same backend and rules give the same decisions.

## Consequences

The loop is a third of the size because it skips the features above, not because the framework is heavy. Its higher token count comes from a single growing conversation (see 0003).
