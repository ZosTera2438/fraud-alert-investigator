"""PolicyGuardHook: deterministic checks on every `submit_decision` call.

Rejects a decision outside the score band, a decision that cites unknown
evidence or none, and anything but escalate when evidence is incomplete or the
budget was breached. The first rejection goes back to the model with the reason
(one retry); a second rejection rewrites the call to escalate.
"""

from __future__ import annotations

from typing import Any

from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry

from ..state import RunContext


def violations(ctx: RunContext, args: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    assessment = ctx.assessment
    if assessment is None:
        return ["no risk assessment exists for this alert"]
    if args.get("alert_id") != ctx.alert_id:
        problems.append(f"alert_id must be {ctx.alert_id}")
    allowed = set(assessment.allowed_decisions)
    if ctx.budget.breached:
        allowed = {"escalate"}
    decision = args.get("decision")
    if decision not in allowed:
        reason = (
            "evidence is incomplete"
            if assessment.incomplete
            else "the budget was exhausted"
            if ctx.budget.breached
            else f"score {assessment.score} is in the {assessment.band} band"
        )
        problems.append(f"decision '{decision}' is not allowed because {reason}; allowed: {sorted(allowed)}")
    cited = args.get("cited_evidence_ids") or []
    known = ctx.evidence.known_ids()
    if not cited:
        problems.append("cite at least one evidence ID")
    unknown = sorted(set(cited) - known)
    if unknown:
        problems.append(f"unknown evidence IDs {unknown}; known IDs: {sorted(known)}")
    return problems


class PolicyGuardHook(HookProvider):
    def __init__(self, ctx: RunContext, max_retries: int = 1) -> None:
        self.ctx = ctx
        self.max_retries = max_retries

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        # Runs before the approval hook so an analyst is never asked about an invalid decision.
        registry.add_callback(BeforeToolCallEvent, self.on_tool, order=10)

    def on_tool(self, event: BeforeToolCallEvent) -> None:
        if event.tool_use["name"] != "submit_decision":
            return
        args: dict[str, Any] = event.tool_use["input"]  # type: ignore[assignment]
        problems = violations(self.ctx, args)
        if not problems:
            return
        self.ctx.policy_rejections += 1
        self.ctx.policy_notes.append("; ".join(problems))
        if self.ctx.policy_rejections <= self.max_retries:
            event.cancel_tool = (
                "POLICY_REJECTED: " + "; ".join(problems) + ". Fix these and call submit_decision again."
            )
            return
        known = self.ctx.evidence.known_ids()
        fallback_ids = self.ctx.assessment.fired_evidence_ids if self.ctx.assessment else []
        cited = [e for e in args.get("cited_evidence_ids") or [] if e in known] or fallback_ids or sorted(known)[:3]
        args.update(
            {
                "alert_id": self.ctx.alert_id,
                "decision": "escalate",
                "cited_evidence_ids": cited,
                "rationale": f"Policy guard override after {self.ctx.policy_rejections} rejected submissions: "
                + "; ".join(problems)[:400],
            }
        )
        self.ctx.override = "policy_guard"
