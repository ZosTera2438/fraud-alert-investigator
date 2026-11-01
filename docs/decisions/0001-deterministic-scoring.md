# 0001. The model never sets the risk score

Status: accepted (2026-10-31)

## Context

A fraud decision must be reproducible, explainable to an auditor, and immune to text inside the data (merchant descriptions, device fields) that tries to steer the model.

## Decision

- `scoring/engine.py` computes the score from raw tool results in the evidence store, using weights in `config/scoring.yaml`. It never reads the investigator's notes.
- Evidence counts only when it matches the alert's own customer, merchant and device (`resolve_evidence`), so a model that fetches the wrong merchant cannot change the score.
- The decision agent writes a `ReportDraft`, which has no score field. The pipeline assembles `InvestigationReport` from the engine's score, the guarded decision receipt and the draft's wording.
- Missing or failed sources mark the assessment `incomplete`; the only allowed decision is then escalate.

## Changes from the plan

The plan's example weights could not separate the 11 scenarios (for example, account takeover scored 55 with the listed signals, below the block threshold). The final rule set adds `card_testing` and `dispute_history` signals, three combination patterns (account takeover, travel fraud, compromised device) and a `known_merchant` mitigation. Bands are unchanged: under 40 allow, 40 to 74 escalate, 75 and over block.

## Consequences

- With full evidence, all 66 golden cases land in their labelled band (`tests/unit/test_scoring.py`).
- Changing a weight is a reviewed config change that must pass the evaluation gate.
- The rules are tuned on synthetic scenarios; real data would need recalibration against labelled outcomes.
