"""ApprovalHook: human approval through a Strands interrupt.

Every block, and every allow above the amount threshold, pauses the run before
`submit_decision` executes. The caller sees `stop_reason == "interrupt"`,
collects the analyst's answer and resumes with an `interruptResponse`. A
rejected block (or a rejected high-value allow) becomes escalate.

Escalate never needs approval: it already hands the alert to an analyst.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry

from ..schemas import ApprovalRecord
from ..state import RunContext

INTERRUPT_NAME = "analyst_approval"


def needs_approval(ctx: RunContext, decision: str, threshold: float) -> bool:
    return decision == "block" or (decision == "allow" and ctx.alert.transaction.amount > threshold)


def parse_response(response: Any) -> ApprovalRecord:
    if isinstance(response, dict):
        return ApprovalRecord(
            approved=bool(response.get("approved")),
            approver=str(response.get("approver", "unknown")),
            note=str(response.get("note", "")),
            decided_at=datetime.now(UTC),
        )
    approved = str(response).strip().lower() in {"approve", "approved", "yes", "y", "a"}
    return ApprovalRecord(approved=approved, approver="unknown", decided_at=datetime.now(UTC))


class ApprovalHook(HookProvider):
    def __init__(self, ctx: RunContext, amount_threshold: float) -> None:
        self.ctx = ctx
        self.amount_threshold = amount_threshold

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.on_tool, order=20)

    def on_tool(self, event: BeforeToolCallEvent) -> None:
        if event.tool_use["name"] != "submit_decision" or event.cancel_tool:
            return
        args: dict[str, Any] = event.tool_use["input"]  # type: ignore[assignment]
        decision = args.get("decision", "")
        if not needs_approval(self.ctx, decision, self.amount_threshold):
            return
        assessment = self.ctx.assessment
        reason = {
            "alert_id": self.ctx.alert_id,
            "decision": decision,
            "risk_score": assessment.score if assessment else None,
            "amount": self.ctx.alert.transaction.amount,
            "currency": self.ctx.alert.transaction.currency,
            "cited_evidence_ids": args.get("cited_evidence_ids", []),
            "rationale": str(args.get("rationale", ""))[:500],
            "question": "Approve this decision? Answer {approved: bool, approver: str, note: str}.",
        }
        self.ctx.pending_approval = reason
        response = event.interrupt(INTERRUPT_NAME, reason=reason)  # raises until the analyst answers
        record = parse_response(response)
        self.ctx.approval = record
        self.ctx.pending_approval = None
        if not record.approved:
            args.update(
                {
                    "decision": "escalate",
                    "rationale": f"Analyst {record.approver} rejected '{decision}': {record.note or 'no note'}. "
                    f"Original rationale: {str(args.get('rationale', ''))[:300]}",
                }
            )
