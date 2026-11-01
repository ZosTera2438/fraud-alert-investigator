"""Strands Graph wiring and the run lifecycle (start, pause for approval, resume).

    intake (code) -> investigator (agent) -> scoring (code) -> decision (agent) -> action (code)

Fixed order, auditable. The policy guard's single retry happens inside the
decision node (the hook returns the rejection to the model, which corrects it
once; a second rejection becomes escalate), so the graph itself stays acyclic.
Any failure ends in escalate with a reason, never allow.
"""

from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opentelemetry import trace
from strands.agent.agent_result import AgentResult
from strands.models.model import Model
from strands.multiagent import GraphBuilder
from strands.multiagent.base import Status
from strands.multiagent.graph import Graph
from strands.session.file_session_manager import FileSessionManager
from strands.telemetry.metrics import EventLoopMetrics

from ._async import run_sync
from .actions import ActionService
from .agents.decision import build_decision_agent, decision_context
from .agents.investigator import build_investigator, investigator_task
from .db import session
from .hooks import INTERRUPT_NAME, redact, write_audit
from .models import FallbackModel, create_model, model_id_of
from .observability import cost_aud, setup_tracing
from .schemas import Alert, Finding, InvestigationReport, ReportDraft
from .scoring.engine import ScoringEngine
from .scripted_model import Behaviour
from .settings import Settings, get_settings
from .state import Budget, RunContext
from .tools.backend import DataBackend

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("fraud_investigator")


class CodeNode:
    """A deterministic graph node. Satisfies Strands' AgentBase protocol so the Graph can run it."""

    def __init__(self, name: str, fn: Callable[[], str]) -> None:
        self.name = name
        self.fn = fn

    async def invoke_async(self, prompt: Any = None, **kwargs: Any) -> AgentResult:
        with tracer.start_as_current_span(f"node.{self.name}"):
            text = self.fn()
        return AgentResult(
            stop_reason="end_turn",
            message={"role": "assistant", "content": [{"text": text}]},
            metrics=EventLoopMetrics(),
            state={},
        )

    def __call__(self, prompt: Any = None, **kwargs: Any) -> AgentResult:
        return run_sync(self.invoke_async(prompt, **kwargs))

    async def stream_async(self, prompt: Any = None, **kwargs: Any):  # type: ignore[no-untyped-def]
        yield {"result": await self.invoke_async(prompt, **kwargs)}


@dataclass
class RunOutcome:
    alert_id: str
    status: str
    report: InvestigationReport | None
    context: RunContext
    interrupts: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        r = self.report
        return {
            "alert_id": self.alert_id,
            "status": self.status,
            "decision": r.recommendation if r else None,
            "risk_score": r.risk_score if r else None,
            "case_id": self.context.case_id,
            "interrupts": self.interrupts,
            "metrics": self.context.metrics,
        }


def mask_alert(alert: Alert) -> dict[str, Any]:
    data = alert.model_dump(mode="json")
    token = data["card_token"]
    data["card_token"] = f"tok_****{token[-9:]}" if len(token) > 9 else "tok_****"
    return {k: (redact(v) if isinstance(v, str) else v) for k, v in data.items()}


class InvestigationPipeline:
    """Runs one alert through the graph. Keeps paused runs so an analyst can resume them."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        db_path: Path | None = None,
        model_factory: Callable[[], Model] | None = None,
        behaviour: Behaviour = "diligent",
        overrides: dict[str, Any] | None = None,
        tools: list[Any] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.db_path = db_path or self.settings.database_path
        t = self.settings.tools
        self.backend = DataBackend(
            self.db_path,
            timeout_seconds=t.timeout_seconds,
            retry_attempts=t.retry_attempts,
            max_text_chars=t.max_text_chars,
        )
        self.model_factory = model_factory or (lambda: create_model(self.settings, behaviour))
        self.overrides = overrides or {}
        self.engine = ScoringEngine()
        self.actions = ActionService(self.db_path, self.settings.policy.version)
        self.runs_dir = self.settings.resolve(self.settings.paths.runs)
        self.sessions_dir = self.settings.resolve(self.settings.paths.sessions)
        self._tools = tools
        self._paused: dict[str, tuple[Graph, RunContext, list[Any], Model]] = {}
        setup_tracing(self.settings)

    # ---------- public API ----------
    def start(self, alert_id: str) -> RunOutcome:
        ctx = self._intake_context(alert_id)
        model = self.model_factory()
        ctx.model_id = model_id_of(model)
        self._paused.pop(alert_id, None)
        shutil.rmtree(self.sessions_dir / f"session_{self._session_id(alert_id)}", ignore_errors=True)
        graph = self._build_graph(ctx, model)
        started = time.perf_counter()
        with tracer.start_as_current_span("investigate_alert", attributes={"fraud.alert_id": alert_id}) as span:
            outcome = self._drive(graph, ctx, model, investigator_task(ctx), started)
            span.set_attribute("fraud.status", outcome.status)
            if outcome.report:
                span.set_attribute("fraud.decision", outcome.report.recommendation)
                span.set_attribute("fraud.risk_score", outcome.report.risk_score)
        return outcome

    def resume(self, alert_id: str, approval: dict[str, Any]) -> RunOutcome:
        """Resume a paused run with the analyst's answer.

        Paused runs live in memory; after a restart the graph is rebuilt and its state restored
        from the Strands session files, so the approval still lands on the same interrupt.
        """
        if alert_id in self._paused:
            graph, ctx, _, model = self._paused.pop(alert_id)
        else:
            loaded = RunContext.load(self.runs_dir, alert_id)
            if loaded is None or loaded.status != "awaiting_approval":
                raise KeyError(f"no paused run for {alert_id}")
            ctx, model = loaded, self.model_factory()
            ctx.metrics["usage_base"] = {k: ctx.metrics.get(k, 0) for k in ("input_tokens", "output_tokens")}
            graph = self._build_graph(ctx, model)
        responses = [{"interruptResponse": {"interruptId": i, "response": approval}} for i in ctx.interrupt_ids]
        write_audit(
            self.db_path,
            alert_id,
            "approval",
            "answered",
            detail=f"approved={approval.get('approved')} by {approval.get('approver')}",
        )
        started = time.perf_counter()
        with tracer.start_as_current_span(
            "resume_alert", attributes={"fraud.alert_id": alert_id, "fraud.approved": bool(approval.get("approved"))}
        ):
            return self._drive(graph, ctx, model, responses, started)

    def paused(self) -> dict[str, dict[str, Any]]:
        return {aid: ctx.pending_approval or {} for aid, (_, ctx, _, _) in self._paused.items()}

    # ---------- internals ----------
    def _intake_context(self, alert_id: str) -> RunContext:
        alert = Alert.model_validate(self.backend.load_alert(alert_id))
        with session(self.db_path) as conn:
            card = conn.execute("SELECT customer_id FROM cards WHERE token=?", (alert.card_token,)).fetchone()
        if card is None or card["customer_id"] != alert.customer_id:
            raise ValueError(f"card token does not belong to customer {alert.customer_id}")
        b = self.settings.budgets
        budget = Budget(
            max_tool_calls=int(self.overrides.get("max_tool_calls", b.max_tool_calls)),
            max_total_tokens=int(self.overrides.get("max_total_tokens", b.max_total_tokens)),
        )
        return RunContext(
            alert=alert, masked_alert=mask_alert(alert), budget=budget, policy_version=self.settings.policy.version
        )

    def _build_graph(self, ctx: RunContext, model: Model) -> Graph:
        tools = self._tools if self._tools is not None else self._make_tools()
        builder = GraphBuilder()
        builder.add_node(CodeNode("intake", lambda: self._intake(ctx)), "intake")
        builder.add_node(build_investigator(ctx, model, tools, self.db_path), "investigator")
        builder.add_node(CodeNode("scoring", lambda: self._score(ctx)), "scoring")
        builder.add_node(build_decision_agent(ctx, model, self.settings, self.db_path), "decision")
        builder.add_node(CodeNode("action", lambda: self._act(ctx)), "action")
        builder.add_edge("intake", "investigator")
        builder.add_edge("investigator", "scoring")
        builder.add_edge("scoring", "decision")
        builder.add_edge("decision", "action")
        builder.set_entry_point("intake")
        builder.set_max_node_executions(8)
        builder.set_execution_timeout(300)
        builder.set_graph_id("investigation")
        builder.set_session_manager(
            FileSessionManager(session_id=self._session_id(ctx.alert_id), storage_dir=str(self.sessions_dir))
        )
        return builder.build()

    @staticmethod
    def _session_id(alert_id: str) -> str:
        return f"alert-{alert_id}"

    def _make_tools(self) -> list[Any]:
        if self.settings.tools.transport == "mcp":
            from .mcp_server.client import mcp_tool_provider

            return [mcp_tool_provider(self.settings, self.db_path)]
        from .tools.readonly import make_readonly_tools

        return make_readonly_tools(self.backend)

    def _drive(self, graph: Graph, ctx: RunContext, model: Model, task: Any, started: float) -> RunOutcome:
        try:
            result = graph(task)
        except Exception as exc:  # the graph raises on node failure; fail safe is escalate
            logger.exception("graph failed for %s", ctx.alert_id)
            return self._fail_safe(ctx, model, f"graph error: {type(exc).__name__}: {exc}", started)
        self._record_usage(ctx, graph, model, started)

        if result.status == Status.INTERRUPTED:
            interrupts = list(result.interrupts or [])
            ctx.status = "awaiting_approval"
            ctx.interrupt_ids = [i.id for i in interrupts if i.name == INTERRUPT_NAME]
            self._paused[ctx.alert_id] = (graph, ctx, interrupts, model)
            ctx.save(self.runs_dir)
            write_audit(self.db_path, ctx.alert_id, "approval", "requested", detail=str(ctx.pending_approval)[:300])
            return RunOutcome(
                ctx.alert_id,
                "awaiting_approval",
                None,
                ctx,
                [{"id": i.id, "name": i.name, "reason": i.reason} for i in interrupts],
            )
        if result.status != Status.COMPLETED or ctx.report is None:
            failed = [n for n, r in result.results.items() if r.status == Status.FAILED]
            return self._fail_safe(ctx, model, f"graph status {result.status.value}; failed nodes {failed}", started)
        ctx.save(self.runs_dir)
        return RunOutcome(ctx.alert_id, ctx.status, ctx.report, ctx)

    def _record_usage(self, ctx: RunContext, graph: Graph, model: Model, started: float) -> None:
        # Agents keep cumulative usage across a pause, so the totals are recomputed, not incremented.
        # A run restored after a restart has fresh agents, so it adds to the usage stored before the restart.
        m = ctx.metrics
        base = m.get("usage_base", {})
        for key, usage_key in (("input_tokens", "inputTokens"), ("output_tokens", "outputTokens")):
            fresh = sum(
                int(getattr(n.executor, "event_loop_metrics", EventLoopMetrics()).accumulated_usage.get(usage_key, 0))
                for n in graph.nodes.values()
            )
            m[key] = int(base.get(key, 0)) + fresh
        m["total_tokens"] = m["input_tokens"] + m["output_tokens"]
        m["latency_ms"] = round(m.get("latency_ms", 0) + (time.perf_counter() - started) * 1000, 1)
        m["tool_calls"] = len(ctx.tool_calls)
        m["policy_rejections"] = ctx.policy_rejections
        m["budget_breached"] = ctx.budget.breached
        m["model_id"] = ctx.model_id
        m["model_fallback"] = isinstance(model, FallbackModel) and model.fell_back
        m["cost_aud"] = cost_aud(self.settings, ctx.model_id, m["input_tokens"], m["output_tokens"])

    # ---------- node bodies ----------
    def _intake(self, ctx: RunContext) -> str:
        write_audit(self.db_path, ctx.alert_id, "intake", "ok", detail=f"trigger={ctx.alert.trigger_rule}")
        ctx.save(self.runs_dir)
        return f"Alert {ctx.alert_id} validated; card number masked; investigation started."

    def _score(self, ctx: RunContext) -> str:
        ctx.assessment = self.engine.score(ctx.alert, ctx.evidence)
        a = ctx.assessment
        write_audit(
            self.db_path,
            ctx.alert_id,
            "scoring",
            "ok",
            detail=f"score={a.score} band={a.band} incomplete={a.incomplete} breached={ctx.budget.breached}",
        )
        ctx.save(self.runs_dir)
        return decision_context(ctx)

    def _act(self, ctx: RunContext) -> str:
        report = self._finalise(ctx)
        approver = ctx.approval.approver if ctx.approval and ctx.approval.approved else None
        result = self.actions.execute(report, ctx.alert.card_token, approver)
        ctx.report, ctx.case_id, ctx.actions, ctx.status = report, result.case_id, result.actions, "completed"
        ctx.save(self.runs_dir)
        return f"Case {result.case_id}: {report.recommendation} (replayed={result.replayed})."

    def _finalise(self, ctx: RunContext, fail_reason: str | None = None) -> InvestigationReport:
        """Assemble the report: score from the engine, words from the agent, decision from the guarded receipt."""
        if ctx.assessment is None:
            ctx.assessment = self.engine.score(ctx.alert, ctx.evidence)
        a = ctx.assessment
        notes: list[str] = []
        source = "agent"
        receipt = ctx.receipt
        if fail_reason or receipt is None:
            decision, source = "escalate", "fail_safe"
            notes.append(fail_reason or "decision agent did not submit a decision")
        else:
            decision = receipt.decision
            if ctx.override:
                source = "policy_override"
            if ctx.approval and not ctx.approval.approved:
                source = "analyst"
            elif ctx.approval and ctx.approval.approved:
                notes.append(f"approved by {ctx.approval.approver}")
        if decision == "block" and not (ctx.approval and ctx.approval.approved):
            decision, source = "escalate", "fail_safe"
            notes.append("block without analyst approval was downgraded to escalate")
        if ctx.budget.breached:
            notes.append(f"budget breached: {ctx.budget.breached}")
        notes.extend(f"policy rejection: {n}" for n in ctx.policy_notes)

        draft = ctx.draft or self._default_draft(ctx, decision)
        if draft.recommendation != decision:
            notes.append(f"report draft said {draft.recommendation}; recorded decision is {decision}")
        known = ctx.evidence.known_ids()

        def grounded(findings: list[Finding]) -> list[Finding]:
            return [Finding(text=f.text, evidence_ids=[e for e in f.evidence_ids if e in known]) for f in findings]

        return InvestigationReport(
            alert_id=ctx.alert_id,
            risk_score=a.score,
            score_breakdown=a.signals,
            recommendation=decision,
            confidence="low" if a.incomplete else draft.confidence,
            key_findings=grounded(draft.key_findings),
            mitigating_factors=grounded(draft.mitigating_factors),
            analyst_summary=draft.analyst_summary,
            decision_source=source,
            notes=notes,  # type: ignore[arg-type]
        )

    @staticmethod
    def _default_draft(ctx: RunContext, decision: str) -> ReportDraft:
        a = ctx.assessment
        assert a is not None
        fired = [s for s in a.signals if s.fired]
        return ReportDraft(
            recommendation=decision,  # type: ignore[arg-type]
            confidence="low",
            key_findings=[
                Finding(text=f"{s.name}: {s.detail}"[:400], evidence_ids=[s.evidence_id] if s.evidence_id else [])
                for s in fired
                if s.kind != "mitigation"
            ],
            mitigating_factors=[
                Finding(text=f"{s.name}: {s.detail}"[:400], evidence_ids=[s.evidence_id] if s.evidence_id else [])
                for s in fired
                if s.kind == "mitigation"
            ],
            analyst_summary=(
                f"Automatic summary: risk score {a.score} ({a.band} band); recommendation {decision}. "
                "The decision agent did not produce a report."
            )[:800],
        )

    def _fail_safe(self, ctx: RunContext, model: Model, reason: str, started: float) -> RunOutcome:
        ctx.errors.append(reason)
        write_audit(self.db_path, ctx.alert_id, "fail_safe", "escalate", detail=reason[:300])
        report = self._finalise(ctx, fail_reason=reason)
        result = self.actions.execute(report, ctx.alert.card_token, approver=None)
        ctx.report, ctx.case_id, ctx.actions, ctx.status = report, result.case_id, result.actions, "completed"
        ctx.metrics.setdefault("latency_ms", round((time.perf_counter() - started) * 1000, 1))
        ctx.metrics["fail_safe"] = reason
        ctx.save(self.runs_dir)
        return RunOutcome(ctx.alert_id, "completed", report, ctx)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
