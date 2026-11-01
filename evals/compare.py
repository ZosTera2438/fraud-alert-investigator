"""Comparisons recorded in docs/decisions (section 12 of the plan).

    uv run --extra ml python evals/compare.py                    # offline: architectures, rules vs blend
    uv run python evals/compare.py --providers gemini,ollama      # adds the live model comparison

Writes evals/reports/comparison.md and comparison.json.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
from datetime import UTC, datetime
from pathlib import Path

from fraud_investigator.baseline.raw_agent_loop import RawAgentLoop
from fraud_investigator.baseline.single_agent import SingleAgentPipeline
from fraud_investigator.evaluation import load_cases, run_suite, scorecard
from fraud_investigator.graph import InvestigationPipeline
from fraud_investigator.schemas import Alert
from fraud_investigator.scoring.engine import ScoringConfig, ScoringEngine
from fraud_investigator.settings import load_settings, project_root
from fraud_investigator.tools.backend import DataBackend
from fraud_investigator.tools.collect import collect_all

ROOT = project_root()
SRC = ROOT / "src" / "fraud_investigator"


def loc(*paths: Path) -> int:
    total = 0
    for p in paths:
        for f in [p] if p.is_file() else p.rglob("*.py"):
            total += sum(
                1
                for line in f.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.strip().startswith("#")
            )
    return total


def architectures(cases: list[dict], injection_cases: list[dict]) -> list[dict]:
    rows = []
    variants = [
        (
            "two agents (Strands Graph)",
            InvestigationPipeline,
            [SRC / "graph.py", SRC / "agents", SRC / "hooks", SRC / "tools/readonly.py", SRC / "tools/decision.py"],
        ),
        (
            "single agent (Strands)",
            SingleAgentPipeline,
            [SRC / "baseline/single_agent.py", SRC / "hooks", SRC / "tools/readonly.py", SRC / "tools/decision.py"],
        ),
        ("framework-free loop", RawAgentLoop, [SRC / "baseline/raw_agent_loop.py"]),
    ]
    settings = load_settings()
    for name, cls, files in variants:
        clean = scorecard(run_suite(cases, settings=settings, trials=1, pipeline_cls=cls), name)
        attacked = run_suite(
            injection_cases, settings=settings, trials=1, pipeline_cls=cls, behaviour_override="injection_follower"
        )
        att = scorecard(attacked, name)
        rows.append(
            {
                "variant": name,
                "loc": loc(*files),
                "accuracy": clean["decision_accuracy"],
                "escalation_rate": clean["escalation_rate"],
                "tool_calls_mean": round(
                    statistics.fmean(
                        len(r.tools_called)
                        for r in run_suite(cases[:11], settings=settings, trials=1, pipeline_cls=cls)
                    ),
                    2,
                ),
                "tokens_mean": clean["tokens_mean"],
                "latency_ms_p50": clean["latency_ms_p50"],
                "injection_follower_accuracy": att["decision_accuracy"],
                "injection_follower_allows": att["injection_followed"],
                "injection_follower_escalations": sum(1 for r in attacked if r.decision == "escalate"),
            }
        )
    return rows


def rules_vs_blend(cases_path: Path) -> list[dict]:
    from fraud_investigator.scoring.ml import BlendModel

    settings = load_settings()
    backend = DataBackend(settings.database_path)
    cases = [json.loads(line) for line in cases_path.read_text(encoding="utf-8").splitlines()]
    rows = []
    for label, enabled in (("rules only", False), ("rules 70% + model 30%", True)):
        config = ScoringConfig.load()
        config.ml_blend = {**config.ml_blend, "enabled": enabled}
        try:
            engine = ScoringEngine(config)
        except (FileNotFoundError, ImportError) as exc:
            rows.append({"variant": label, "error": str(exc)})
            continue
        hits, false_blocks, missed = 0, 0, 0
        for case in cases:
            alert = Alert.model_validate(backend.load_alert(case["alert_id"]))
            a = engine.score(alert, collect_all(backend, alert))
            decision = "escalate" if a.incomplete else a.band
            hits += decision == case["expected_decision"]
            false_blocks += decision == "block" and case["expected_decision"] == "allow"
            missed += decision == "allow" and case["expected_decision"] == "block"
        row = {
            "variant": label,
            "accuracy": round(hits / len(cases), 4),
            "false_blocks": false_blocks,
            "missed_fraud": missed,
        }
        if enabled:
            row["top_features"] = dict(list(BlendModel.load(ROOT / "data" / "ml_model.pkl").importances().items())[:3])
        rows.append(row)
    return rows


def providers(names: list[str], cases: list[dict]) -> list[dict]:
    rows = []
    for provider in names:
        os.environ["FRAUD_MODEL_PROVIDER"] = provider
        settings = load_settings()
        try:
            card = scorecard(run_suite(cases, settings=settings, trials=1), settings.model_id)
        except Exception as exc:
            rows.append({"provider": provider, "error": f"{type(exc).__name__}: {exc}"[:200]})
            continue
        rows.append(
            {
                "provider": provider,
                "model": settings.model_id,
                **{
                    k: card[k]
                    for k in (
                        "decision_accuracy",
                        "schema_validity",
                        "required_tools",
                        "grounding",
                        "latency_ms_p95",
                        "tokens_mean",
                        "cost_aud_per_alert",
                        "errors",
                    )
                },
            }
        )
    os.environ.pop("FRAUD_MODEL_PROVIDER", None)
    return rows


def table(rows: list[dict]) -> str:
    if not rows:
        return "_not run_\n"
    keys = list(dict.fromkeys(k for r in rows for k in r))
    lines = ["| " + " | ".join(keys) + " |", "|" + " --- |" * len(keys)]
    lines += ["| " + " | ".join(str(r.get(k, "")) for k in keys) + " |" for r in rows]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--providers", default="", help="comma separated live providers, e.g. gemini,ollama")
    args = parser.parse_args()
    golden = load_cases(ROOT / "evals" / "cases" / "golden.jsonl")
    red = load_cases(ROOT / "evals" / "cases" / "red_team.jsonl")
    injection = [c for c in golden + red if "injection" in c.get("tags", []) and c.get("model_behaviour") == "diligent"]
    result = {
        "generated_at": datetime.now(UTC).isoformat(),
        "architectures": architectures(golden, injection),
        "rules_vs_blend": rules_vs_blend(ROOT / "evals" / "cases" / "golden.jsonl"),
        "providers": providers([p for p in args.providers.split(",") if p], golden),
    }
    out = ROOT / "evals" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    (out / "comparison.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    md = (
        f"# Comparisons\n\nGenerated {result['generated_at'][:16]} UTC with the scripted model unless noted.\n\n"
        f"## Architectures (golden set, plus injection cases with an injection-following model)\n\n"
        f"{table(result['architectures'])}\n## Rules only versus rules plus model\n\n{table(result['rules_vs_blend'])}"
        f"\n## Live providers\n\n{table(result['providers'])}"
    )
    (out / "comparison.md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
