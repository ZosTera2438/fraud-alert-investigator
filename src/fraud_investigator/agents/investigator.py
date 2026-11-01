"""Investigator agent: read-only tools, returns an evidence bundle."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from strands import Agent
from strands.hooks import AfterInvocationEvent, HookProvider, HookRegistry
from strands.models.model import Model

from ..hooks import AlertScopeHook, AuditHook, BudgetHook, EvidenceHook, RedactionHook
from ..state import RunContext
from . import load_prompt


def investigator_task(ctx: RunContext) -> str:
    """The graph's task text: the alert only. Instructions live in each agent's system prompt."""
    return f"Fraud alert {ctx.alert_id}:\n<alert>{json.dumps(ctx.masked_alert, default=str)}</alert>"


class _CaptureNotes(HookProvider):
    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(AfterInvocationEvent, self.on_end)

    def on_end(self, event: AfterInvocationEvent) -> None:
        if event.result is not None:
            self.ctx.investigator_notes = str(event.result).strip()[:4000]


def build_investigator(ctx: RunContext, model: Model, tools: list[Any], db_path: Path) -> Agent:
    return Agent(
        name="investigator",
        agent_id="investigator",
        description="Gathers transaction, merchant, device and customer evidence for a fraud alert.",
        model=model,
        system_prompt=load_prompt("investigator"),
        tools=tools,
        hooks=[
            RedactionHook(),
            AlertScopeHook(ctx),
            BudgetHook(ctx),
            EvidenceHook(ctx, "investigator"),
            AuditHook(ctx, db_path, "investigator"),
            _CaptureNotes(ctx),
        ],
        callback_handler=None,
        trace_attributes={"fraud.alert_id": ctx.alert_id, "fraud.agent": "investigator"},
    )
