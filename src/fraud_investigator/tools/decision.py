"""`submit_decision`: the only write-shaped tool, given to the decision agent alone.

It records the decision on the run; the action layer executes it later. By the
time this function runs, the policy guard and approval hooks have already
checked (and possibly rewritten) the arguments.
"""

from __future__ import annotations

from typing import Any, Literal

from strands import tool

from ..schemas import DecisionReceipt
from ..state import RunContext


def make_decision_tool(ctx: RunContext) -> Any:
    @tool
    def submit_decision(
        alert_id: str,
        decision: Literal["allow", "escalate", "block"],
        cited_evidence_ids: list[str],
        rationale: str,
    ) -> str:
        """Submit the recommendation for this alert. Call exactly once, after reviewing the evidence.

        A block, or an allow above 5,000 AUD, waits for analyst approval. If the call is rejected,
        read the reason, fix the arguments and call again once.

        Args:
            alert_id: The alert ID being decided.
            decision: allow, escalate or block. It must fit the risk score band; escalate is always allowed.
            cited_evidence_ids: Evidence IDs (EV-...) from tool results that support the decision.
            rationale: Two or three sentences explaining the decision with reference to the evidence.
        """
        receipt = DecisionReceipt(
            alert_id=alert_id,
            decision=decision,
            accepted=True,
            message="Decision recorded. Now write the report.",
            cited_evidence_ids=cited_evidence_ids,
            rationale=rationale[:1000],
            overridden_by=ctx.override,
            approval=ctx.approval,
        )
        ctx.receipt = receipt
        return receipt.model_dump_json()

    return submit_decision
