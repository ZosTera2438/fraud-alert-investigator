"""Scoring output, decision contracts and the final investigation report."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Decision = Literal["allow", "escalate", "block"]
Confidence = Literal["low", "medium", "high"]


class SignalResult(BaseModel):
    name: str
    kind: Literal["risk", "combination", "mitigation", "model"] = "risk"
    fired: bool
    points: int
    evidence_id: str | None = None
    detail: str = ""


class RiskAssessment(BaseModel):
    """Deterministic score computed from raw tool results."""

    score: int = Field(ge=0, le=100)
    band: Decision
    signals: list[SignalResult]
    rule_score: int
    model_probability: float | None = None
    incomplete: bool = False
    missing_sources: list[str] = Field(default_factory=list)
    failed_sources: list[str] = Field(default_factory=list)
    scoring_version: str

    @property
    def allowed_decisions(self) -> set[Decision]:
        """Escalate is always allowed (the safe direction); incomplete evidence allows only escalate."""
        if self.incomplete:
            return {"escalate"}
        return {self.band, "escalate"}

    @property
    def fired_evidence_ids(self) -> list[str]:
        return sorted({s.evidence_id for s in self.signals if s.fired and s.evidence_id})


class Finding(BaseModel):
    text: str = Field(max_length=400)
    evidence_ids: list[str] = Field(default_factory=list)


class ReportDraft(BaseModel):
    """What the decision agent writes. It has no score field: the model never sets the score."""

    recommendation: Decision
    confidence: Confidence
    key_findings: list[Finding] = Field(default_factory=list)
    mitigating_factors: list[Finding] = Field(default_factory=list)
    analyst_summary: str = Field(max_length=800)


class DecisionReceipt(BaseModel):
    alert_id: str
    decision: Decision
    accepted: bool
    message: str
    cited_evidence_ids: list[str]
    rationale: str
    overridden_by: str | None = None
    approval: ApprovalRecord | None = None


class ApprovalRecord(BaseModel):
    approved: bool
    approver: str
    note: str = ""
    decided_at: datetime | None = None


class InvestigationReport(BaseModel):
    alert_id: str
    risk_score: int = Field(ge=0, le=100)
    score_breakdown: list[SignalResult]
    recommendation: Decision
    confidence: Confidence
    key_findings: list[Finding]
    mitigating_factors: list[Finding]
    analyst_summary: str = Field(max_length=800)
    decision_source: Literal["agent", "policy_override", "fail_safe", "analyst"] = "agent"
    notes: list[str] = Field(default_factory=list)


DecisionReceipt.model_rebuild()
