# 0003. Investigator plus decision agent, not one agent

Status: accepted (2026-10-31)

## Context

The plan splits the work into a read-only investigator and a decision agent that holds the only write-shaped tool. `baseline/single_agent.py` gives one Strands agent every tool (five data tools, a score tool and `submit_decision`) with the same hooks.

## Measurements

Scripted model, one trial. Clean run: 66 golden cases. Attack run: the 10 injection cases replayed with a model that obeys instructions found in data.

| Variant | Accuracy | Mean tokens | Tool calls | Injection run: allows | Injection run: escalations |
| --- | --- | --- | --- | --- | --- |
| Two agents (Graph) | 1.0 | 10,850 | 7.0 | 0 | 10 of 10 |
| Single agent | 1.0 | 26,131 | 8.0 | 0 | 10 of 10 |

## Findings

- **Cost**: the single agent used 2.4 times the tokens, because raw tool data stays in its context for every later turn. The decision agent only sees the score, the evidence index and short notes.
- **Injection**: neither variant allowed an injected alert. The policy guard, not the agent split, is what stopped it: the compromised model first submitted allow, the guard rejected it as outside the block band, and the retry was escalate. The split reduces exposure (raw merchant text never reaches the decision agent), but the investigator's notes can still carry injected text, as the injection-following investigator showed.
- **Accuracy under attack** is 0 against the block labels in both variants: the guard can stop a wrong allow but cannot force the right block. That is deliberate; a block needs a human.

## Decision

Keep two agents for least privilege, lower cost and separate evaluation signals (trajectory for the investigator, decision and grounding for the decision agent). Keep the policy guard as the control that actually enforces policy.

## To measure with live models

Run `uv run python evals/compare.py --providers gemini,ollama`. Real models may differ from the scripted policy in how often they follow injected text.
