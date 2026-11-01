# 0005. Rules only, with the model blend off by default

Status: accepted (2026-10-31)

## Context

The plan suggests blending a gradient boosting probability with the rule score (70 percent rules, 30 percent model).

## Method

`scoring/ml.py` trains `GradientBoostingClassifier` on the fired signal flags of 66 cases generated with seed 7 (label: should block), then scores the seed 42 golden set with full evidence. Training and evaluation data come from different seeds.

## Results

| Variant | Accuracy | False blocks | Missed fraud |
| --- | --- | --- | --- |
| Rules only | 1.0 | 0 | 0 |
| Rules 70% plus model 30% | 0.727 | 0 | 0 |

Feature importance: `geo_mismatch` 0.69, `trusted_device` 0.31, every other signal 0.

## Findings

The model learned a shortcut. In the synthetic data, a foreign transaction country without a travel notice separates block scenarios from the rest, so the model ignored the signals that actually define each fraud type. Blending its near-zero probability pulled all 18 escalate cases (high-risk merchant, conflicting signals, first-party fraud) below 40, so they would have been allowed. Block cases stayed blocked, which is why "missed fraud" (block labelled, allowed) still reads 0: that metric alone would have hidden the regression; decision accuracy caught it.

## Decision

Keep the blend switched off (`ml_blend.enabled: false`). Revisit it only with labelled outcomes from real investigations, where a model can find interactions the rules miss. The scoring report already shows the rule score and model probability separately, so a blend can run in shadow mode before it affects decisions.
