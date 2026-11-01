"""Single-agent variant for the architecture comparison (ADR 0003).

One Strands agent holds every tool: the five read-only tools, a score tool and
`submit_decision`, with the same hooks as the two-agent graph. The difference
under test is that one context sees raw tool data *and* holds the write tool.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from strands import Agent, tool
from strands.models.model import Model

from ..actions import ActionService
from ..agents import load_prompt
from ..graph import InvestigationPipeline, RunOutcome
from ..hooks import (
    AlertScopeHook,
    ApprovalHook,
    AuditHook,
    BudgetHook,
    EvidenceHook,
    PolicyGuardHook,
    RedactionHook,
)
from ..schemas import ReportDraft
from ..state import RunContext
from ..tools.decision import make_decision_tool
from ..tools.readonly import make_readonly_tools

SYSTEM_PROMPT = (
    load_prompt("investigator").split("## Rules")[0]
    + "\nAfter collecting evidence, call `get_risk_score`, then follow these decision instructions:\n\n"
    + load_prompt("decision")
)


class SingleAgentPipeline(InvestigationPipeline):
    def start(self, alert_id: str) -> RunOutcome:
        ctx = self._intake_context(alert_id)
        model = self.model_factory()
        agent = self._build_agent(ctx, model)
        started = time.perf_counter()
        result = agent(f"Fraud alert {alert_id}:\n<alert>{json.dumps(ctx.masked_alert)}</alert>")
        self._paused[alert_id] = (agent, ctx, list(result.interrupts or []), model)  # type: ignore[assignment]
        return self._finish(agent, ctx, result, started)

    def resume(self, alert_id: str, approval: dict[str, Any]) -> RunOutcome:
        agent, ctx, interrupts, _ = self._paused[alert_id]
        started = time.perf_counter()
        result = agent([{"interruptResponse": {"interruptId": i.id, "response": approval}} for i in interrupts])  # type: ignore[operator]
        self._paused[alert_id] = (agent, ctx, list(result.interrupts or []), _)
        return self._finish(agent, ctx, result, started)  # type: ignore[arg-type]

    def _finish(self, agent: Agent, ctx: RunContext, result: Any, started: float) -> RunOutcome:
        usage = agent.event_loop_metrics.accumulated_usage
        ctx.metrics.update(
            input_tokens=usage.get("inputTokens", 0),
            output_tokens=usage.get("outputTokens", 0),
            total_tokens=usage.get("totalTokens", 0),
            tool_calls=len(ctx.tool_calls),
            latency_ms=round(ctx.metrics.get("latency_ms", 0) + (time.perf_counter() - started) * 1000, 1),
        )
        if result.stop_reason == "interrupt":
            ctx.status = "awaiting_approval"
            return RunOutcome(ctx.alert_id, "awaiting_approval", None, ctx)
        if isinstance(result.structured_output, ReportDraft):
            ctx.draft = result.structured_output
        self._paused.pop(ctx.alert_id, None)
        report = self._finalise(ctx)
        approver = ctx.approval.approver if ctx.approval and ctx.approval.approved else None
        action = ActionService(self.db_path, self.settings.policy.version).execute(
            report, ctx.alert.card_token, approver
        )
        ctx.report, ctx.case_id, ctx.status = report, action.case_id, "completed"
        return RunOutcome(ctx.alert_id, "completed", report, ctx)

    def _build_agent(self, ctx: RunContext, model: Model) -> Agent:
        engine = self.engine

        @tool
        def get_risk_score() -> str:
            """Compute the deterministic risk score from the evidence collected so far."""
            from ..agents.decision import decision_context

            ctx.assessment = engine.score(ctx.alert, ctx.evidence)
            return decision_context(ctx)

        tools: list[Callable[..., Any]] = [*make_readonly_tools(self.backend), get_risk_score, make_decision_tool(ctx)]
        return Agent(
            name="single_agent",
            model=model,
            system_prompt=SYSTEM_PROMPT,
            tools=tools,
            structured_output_model=ReportDraft,
            callback_handler=None,
            hooks=[
                RedactionHook(),
                AlertScopeHook(ctx),
                BudgetHook(ctx),
                PolicyGuardHook(ctx, self.settings.policy.max_policy_retries),
                ApprovalHook(ctx, self.settings.policy.approval_amount_threshold),
                EvidenceHook(ctx, "single_agent"),
                AuditHook(ctx, self.db_path, "single_agent"),
            ],
        )
