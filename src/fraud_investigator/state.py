"""Per-alert run state, shared by the graph nodes and hooks and persisted for resume."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .schemas import (
    Alert,
    ApprovalRecord,
    DecisionReceipt,
    InvestigationReport,
    ReportDraft,
    RiskAssessment,
)
from .tools.evidence import EvidenceStore

RunStatus = Literal["running", "awaiting_approval", "completed", "failed"]


class ToolCallRecord(BaseModel):
    agent: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: str
    duration_ms: float | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class Budget(BaseModel):
    max_tool_calls: int
    max_total_tokens: int
    tool_calls_used: int = 0
    tokens_used: int = 0
    breached: str | None = None


class RunContext(BaseModel):
    alert: Alert
    masked_alert: dict[str, Any]
    budget: Budget
    model_id: str = "scripted"
    policy_version: str = "v1"
    evidence: EvidenceStore = Field(default_factory=EvidenceStore)
    assessment: RiskAssessment | None = None
    investigator_notes: str = ""
    receipt: DecisionReceipt | None = None
    draft: ReportDraft | None = None
    report: InvestigationReport | None = None
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    policy_rejections: int = 0
    policy_notes: list[str] = Field(default_factory=list)
    override: str | None = None
    approval: ApprovalRecord | None = None
    pending_approval: dict[str, Any] | None = None
    interrupt_ids: list[str] = Field(default_factory=list)
    status: RunStatus = "running"
    case_id: str | None = None
    actions: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def alert_id(self) -> str:
        return self.alert.alert_id

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.alert_id}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)
        return path

    @classmethod
    def load(cls, directory: Path, alert_id: str) -> RunContext | None:
        path = directory / f"{alert_id}.json"
        return cls.model_validate_json(path.read_text(encoding="utf-8")) if path.exists() else None
