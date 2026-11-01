"""Command line entry point.

fraud-investigator generate                      # build data/fraud.db and the eval cases
fraud-investigator list
fraud-investigator run ALT-000001                # prompts for approval when the agent wants to block
fraud-investigator run ALT-000001 --approve      # answer the approval automatically
fraud-investigator approve ALT-000001 --approver me [--reject]   # resume a paused run
fraud-investigator hello                         # one-line check that the configured model answers
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .settings import get_settings, project_root


def _print_outcome(outcome) -> None:  # type: ignore[no-untyped-def]
    print(json.dumps(outcome.summary(), indent=2, default=str))
    if outcome.report:
        r = outcome.report
        print(
            f"\n{r.recommendation.upper()} (score {r.risk_score}, {r.confidence} confidence, from {r.decision_source})"
        )
        print(r.analyst_summary)
        for n in r.notes:
            print(f"  note: {n}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fraud-investigator", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--seed", type=int, default=42)
    sub.add_parser("list")
    sub.add_parser("hello")
    r = sub.add_parser("run")
    r.add_argument("alert_id")
    r.add_argument("--approve", action="store_true")
    r.add_argument("--reject", action="store_true")
    r.add_argument("--approver", default="cli.analyst")
    a = sub.add_parser("approve")
    a.add_argument("alert_id")
    a.add_argument("--approver", default="cli.analyst")
    a.add_argument("--reject", action="store_true")
    a.add_argument("--note", default="")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    if args.command == "generate":
        from .datagen import generate

        print(generate(get_settings().database_path, project_root() / "evals" / "cases", seed=args.seed))
        return 0
    if args.command == "hello":
        from strands import Agent

        from .models import create_model

        agent = Agent(model=create_model(), callback_handler=None)
        print(agent("Reply with the single word: ready"))
        return 0

    from .graph import InvestigationPipeline

    pipeline = InvestigationPipeline()
    if args.command == "list":
        for alert in pipeline.backend.list_alerts():
            print(f"{alert['id']}  {alert['trigger_rule']:32s} {alert['amount']:>9.2f} AUD  {alert['status']}")
        return 0
    if args.command == "approve":
        _print_outcome(
            pipeline.resume(args.alert_id, {"approved": not args.reject, "approver": args.approver, "note": args.note})
        )
        return 0

    outcome = pipeline.start(args.alert_id)
    while outcome.status == "awaiting_approval":
        request = outcome.context.pending_approval or {}
        print(json.dumps(request, indent=2))
        if args.approve or args.reject:
            approved = args.approve
        else:
            try:
                approved = input("Approve? [y/N] ").strip().lower() in {"y", "yes"}
            except EOFError:  # no interactive input: leave the run paused on disk
                print(f"Paused. Resume with: fraud-investigator approve {args.alert_id} --approver <name> [--reject]")
                return 0
        outcome = pipeline.resume(args.alert_id, {"approved": approved, "approver": args.approver})
    _print_outcome(outcome)
    return 0


if __name__ == "__main__":
    sys.exit(main())
