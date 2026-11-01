"""FastAPI service: investigate alerts, list pending approvals, approve or reject, read cases.

uv run uvicorn fraud_investigator.api.app:app --reload
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from ..db import session
from ..graph import InvestigationPipeline
from ..settings import get_settings
from ..state import RunContext

app = FastAPI(title="Fraud Alert Investigator", version="0.1.0")


@lru_cache(maxsize=1)
def pipeline() -> InvestigationPipeline:
    return InvestigationPipeline(get_settings())


class ApprovalIn(BaseModel):
    approved: bool
    approver: str = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=500)


def _run_view(ctx: RunContext) -> dict[str, Any]:
    return {
        "alert_id": ctx.alert_id,
        "status": ctx.status,
        "case_id": ctx.case_id,
        "risk": ctx.assessment.model_dump() if ctx.assessment else None,
        "pending_approval": ctx.pending_approval,
        "report": ctx.report.model_dump(mode="json") if ctx.report else None,
        "tool_calls": [t.model_dump() for t in ctx.tool_calls],
        "evidence": {eid: {"source": r.source, "status": r.status} for eid, r in ctx.evidence.records.items()},
        "approval": ctx.approval.model_dump(mode="json") if ctx.approval else None,
        "actions": ctx.actions,
        "metrics": ctx.metrics,
        "errors": ctx.errors,
    }


@app.get("/health")
def health() -> dict[str, Any]:
    s = get_settings()
    return {
        "status": "ok",
        "model_provider": s.model.provider,
        "model_id": s.model_id,
        "tool_transport": s.tools.transport,
    }


@app.get("/alerts")
def list_alerts(status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    return pipeline().backend.list_alerts(status, limit)


@app.get("/alerts/{alert_id}")
def get_alert(alert_id: str) -> dict[str, Any]:
    try:
        return pipeline().backend.load_alert(alert_id)
    except KeyError as exc:
        raise HTTPException(404, f"alert {alert_id} not found") from exc


@app.post("/alerts/{alert_id}/investigate")
def investigate(alert_id: str) -> dict[str, Any]:
    try:
        outcome = pipeline().start(alert_id)
    except KeyError as exc:
        raise HTTPException(404, f"alert {alert_id} not found") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _run_view(outcome.context)


@app.get("/runs/{alert_id}")
def get_run(alert_id: str) -> dict[str, Any]:
    ctx = RunContext.load(pipeline().runs_dir, alert_id)
    if ctx is None:
        raise HTTPException(404, f"no run for {alert_id}")
    return _run_view(ctx)


@app.get("/approvals")
def pending_approvals() -> list[dict[str, Any]]:
    p = pipeline()
    pending = []
    for path in sorted(p.runs_dir.glob("*.json")) if p.runs_dir.exists() else []:
        ctx = RunContext.load(p.runs_dir, path.stem)
        if ctx and ctx.status == "awaiting_approval":
            pending.append(
                {
                    "alert_id": ctx.alert_id,
                    "request": ctx.pending_approval,
                    "risk_score": ctx.assessment.score if ctx.assessment else None,
                }
            )
    return pending


@app.post("/runs/{alert_id}/approval")
def answer_approval(alert_id: str, body: ApprovalIn) -> dict[str, Any]:
    try:
        outcome = pipeline().resume(alert_id, body.model_dump())
    except KeyError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _run_view(outcome.context)


@app.get("/cases")
def list_cases(limit: int = 100) -> list[dict[str, Any]]:
    with session(pipeline().db_path) as conn:
        rows = conn.execute(
            "SELECT id, alert_id, decision, risk_score, approver, created_at FROM cases "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/audit/{alert_id}")
def audit(alert_id: str) -> list[dict[str, Any]]:
    with session(pipeline().db_path) as conn:
        rows = conn.execute(
            "SELECT step, tool, arguments_hash, result_status, detail, timestamp FROM audit_log "
            "WHERE alert_id=? ORDER BY id",
            (alert_id,),
        ).fetchall()
    return [dict(r) for r in rows]
