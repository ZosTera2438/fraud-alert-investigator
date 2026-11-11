"""Run the evaluation sets and enforce the release gate.

    uv run python evals/run_evals.py                      # scripted model, golden + red team, 3 trials
    uv run python evals/run_evals.py --provider gemini    # live Gemini (needs GEMINI_API_KEY)
    uv run python evals/run_evals.py --model cloudflare:@cf/openai/gpt-oss-120b --per-scenario 1 --trials 1
    uv run python evals/run_evals.py --model gemini:gemini-2.5-flash --model gemini:gemini-2.5-flash-lite
        --workers 4 --judge --report-name gemini-comparison    # compare two Gemini models
    uv run python evals/run_evals.py --behaviour lazy     # inject a regression: the gate must fail
    uv run python evals/run_evals.py --record             # record live responses for replay in CI

Exit code 1 when any threshold in evals/thresholds.yaml is missed (with --gate).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime

from fraud_investigator.evaluation import (
    gate,
    judge_explanations,
    load_cases,
    load_thresholds,
    render_markdown,
    results_json,
    run_suite,
    scorecard,
)
from fraud_investigator.settings import get_settings, load_settings, project_root

ROOT = project_root()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--provider",
        choices=["scripted", "cloudflare", "gemini", "replay"],
        action="append",
        help="repeat to compare providers; default from settings",
    )
    parser.add_argument(
        "--model",
        action="append",
        default=[],
        metavar="PROVIDER:MODEL_ID",
        help="repeat to compare models, e.g. gemini:gemini-2.5-flash",
    )
    parser.add_argument("--workers", type=int, default=1, help="cases run in parallel within a trial")
    parser.add_argument("--per-scenario", type=int, help="keep the first N cases of each scenario")
    parser.add_argument(
        "--allow-fallback",
        action="store_true",
        help="let a live model fall back to another (off by default so results stay comparable)",
    )
    parser.add_argument("--judge-model", help="model id for the explanation judge (default: settings aux_model)")
    parser.add_argument("--report-name", default="latest", help="markdown file name under evals/reports")
    parser.add_argument("--cases", help="comma separated case IDs to run, e.g. GLD-007,GLD-019")
    parser.add_argument("--sets", default="golden,red_team")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--scenario")
    parser.add_argument(
        "--behaviour",
        choices=["diligent", "lazy", "injection_follower", "always_allow"],
        help="force a scripted behaviour for every case",
    )
    parser.add_argument("--gate", action="store_true", help="exit 1 if a threshold is missed")
    parser.add_argument("--judge", action="store_true", help="add the model-graded explanation score")
    parser.add_argument("--record", action="store_true", help="record live model responses for replay")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    targets = [tuple(m.split(":", 1)) for m in args.model] + [(p, None) for p in args.provider or []]
    if not targets:
        targets = [(get_settings().model.provider, None)]
    cases = [
        c
        for s in args.sets.split(",")
        for c in load_cases(ROOT / "evals" / "cases" / f"{s}.jsonl", args.limit, args.scenario)
    ]
    if args.cases:
        wanted = set(args.cases.split(","))
        cases = [c for c in cases if c["case_id"] in wanted]
    if args.per_scenario:
        seen: dict[str, int] = {}
        cases = [
            c
            for c in cases
            if (
                seen.__setitem__(c["scenario"], seen.get(c["scenario"], 0) + 1)
                or seen[c["scenario"]] <= args.per_scenario
            )
        ]
    thresholds = load_thresholds()
    cards, gate_failures = {}, {}
    for provider, model_id in targets:
        os.environ["FRAUD_MODEL_PROVIDER"] = provider
        if model_id:
            os.environ["FRAUD_MODEL_ID"] = model_id
        else:
            os.environ.pop("FRAUD_MODEL_ID", None)
        if provider != "scripted" and not args.allow_fallback:
            os.environ["FRAUD_MODEL_FALLBACK"] = "none"
        settings = load_settings()
        model_factory = None
        if args.record:
            from fraud_investigator.models import RecordingModel, create_model

            directory = settings.resolve(settings.model.recordings_dir)
            model_factory = lambda behaviour, s=settings, d=directory: RecordingModel(create_model(s, behaviour), d)  # noqa: E731
        live_cases = (
            cases
            if provider == "scripted"
            else [c for c in cases if c.get("model_behaviour", "diligent") == "diligent"]
        )
        results = run_suite(
            live_cases,
            settings=settings,
            trials=args.trials,
            model_factory=model_factory,
            behaviour_override=args.behaviour,
            progress=None if args.quiet else lambda line: print(line, flush=True),
            workers=args.workers,
        )
        label = settings.model_id + (f"-{args.behaviour}" if args.behaviour else "")
        card = scorecard(results, label)
        if args.judge:
            card.update(judge_explanations(results, settings, args.judge_model))
        cards[label], gate_failures[label] = card, gate(card, thresholds)

        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        out = ROOT / "evals" / "reports"
        out.mkdir(parents=True, exist_ok=True)
        safe = label.replace(":", "_").replace("/", "_")
        (out / f"scorecard-{safe}-{stamp}.json").write_text(json.dumps(card, indent=2), encoding="utf-8")
        (out / f"results-{safe}-{stamp}.json").write_text(
            json.dumps(results_json(results), indent=1, default=str), encoding="utf-8"
        )

    markdown = render_markdown(cards, gate_failures)
    (ROOT / "evals" / "reports" / f"{args.report_name}.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    failed = {k: v for k, v in gate_failures.items() if v}
    if failed:
        print("RELEASE GATE FAILED:", json.dumps(failed, indent=1))
        return 1 if args.gate else 0
    print("Release gate passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
