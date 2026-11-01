"""Decision agent: submits a guarded decision and writes the report draft."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from strands import Agent
from strands.hooks import AfterInvocationEvent, HookProvider, HookRegistry
from strands.models.model import Model

from ..hooks import ApprovalHook, AuditHook, BudgetHook, EvidenceHook, PolicyGuardHook, RedactionHook
from ..schemas import ReportDraft
from ..settings import Settings
from ..state import RunContext
from ..tools.decision import make_decision_tool
from . import load_prompt


def decision_context(ctx: RunContext) -> str:
    """What the scoring node hands to the decision agent."""
    assert ctx.assessment is not None
    a = ctx.assessment
    risk = {
        "score": a.score,
        "band": a.band,
        "rule_score": a.rule_score,
        "model_probability": a.model_probability,
        "allowed_decisions": sorted({"escalate"} if ctx.budget.breached else a.allowed_decisions),
        "incomplete": a.incomplete or bool(ctx.budget.breached),
        "missing_sources": a.missing_sources,
        "failed_sources": a.failed_sources,
        "budget_breached": ctx.budget.breached,
        "fired_evidence_ids": a.fired_evidence_ids,
        "signals": [s.model_dump() for s in a.signals if s.fired],
        "scoring_version": a.scoring_version,
    }
    index = [
        {"evidence_id": r.evidence_id, "source": r.source, "status": r.status} for r in ctx.evidence.records.values()
    ]
    return (
        f"<risk_assessment>{json.dumps(risk)}</risk_assessment>\n"
        f"<evidence_index>{json.dumps(index)}</evidence_index>\n"
        f"<investigator_notes>{ctx.investigator_notes.replace('<', '(').replace('>', ')')}</investigator_notes>\n"
        "Submit the decision for this alert, then write the report."
    )


class _CaptureDraft(HookProvider):
    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(AfterInvocationEvent, self.on_end)

    def on_end(self, event: AfterInvocationEvent) -> None:
        output = getattr(event.result, "structured_output", None) if event.result is not None else None
        if isinstance(output, ReportDraft):
            self.ctx.draft = output


def build_decision_agent(ctx: RunContext, model: Model, settings: Settings, db_path: Path) -> Agent:
    return Agent(
        name="decision",
        agent_id="decision",
        description="Recommends allow, escalate or block with cited evidence.",
        model=model,
        system_prompt=load_prompt("decision"),
        tools=[make_decision_tool(ctx)],
        structured_output_model=ReportDraft,
        hooks=[
            RedactionHook(),
            BudgetHook(ctx),
            PolicyGuardHook(ctx, settings.policy.max_policy_retries),
            ApprovalHook(ctx, settings.policy.approval_amount_threshold),
            EvidenceHook(ctx, "decision"),
            AuditHook(ctx, db_path, "decision"),
            _CaptureDraft(ctx),
        ],
        callback_handler=None,
        trace_attributes={"fraud.alert_id": ctx.alert_id, "fraud.agent": "decision"},
    )
