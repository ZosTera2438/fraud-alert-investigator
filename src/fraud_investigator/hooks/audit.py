"""Hooks that observe tool calls: alert scoping, evidence capture and the audit log."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry

from ..db import session
from ..state import RunContext, ToolCallRecord
from ..tools.evidence import TOOL_SOURCES


def _result_text(result: Any) -> str:
    return "\n".join(c.get("text", "") for c in (result or {}).get("content", []) if isinstance(c, dict))


class AlertScopeHook(HookProvider):
    """Fills alert-scoped arguments and keeps data tools on the alert's own customer.

    The model never has to pass timestamps, and an injected instruction cannot
    make it read another customer's data.
    """

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.on_tool)

    def on_tool(self, event: BeforeToolCallEvent) -> None:
        name = event.tool_use["name"]
        if name not in TOOL_SOURCES:
            return
        args: dict[str, Any] = event.tool_use["input"]  # type: ignore[assignment]
        if not isinstance(args, dict):
            return
        customer = self.ctx.alert.customer_id
        if "customer_id" in args and args["customer_id"] != customer and name != "get_device_metadata":
            event.cancel_tool = (
                f"OUT_OF_SCOPE: this investigation may only read customer {customer}. Do not query other customers."
            )
            return
        if name == "get_transaction_history":
            args["as_of"] = self.ctx.alert.created_at.isoformat()
        if name == "get_device_metadata":
            args["customer_id"] = customer


class EvidenceHook(HookProvider):
    """Parses every tool result into the evidence store and the run's trajectory."""

    def __init__(self, ctx: RunContext, agent_name: str) -> None:
        self.ctx = ctx
        self.agent_name = agent_name

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self.on_result)

    def on_result(self, event: AfterToolCallEvent) -> None:
        name = event.tool_use["name"]
        args = dict(event.tool_use["input"]) if isinstance(event.tool_use["input"], dict) else {}
        status = "cancelled" if event.cancel_message else event.result.get("status", "error")
        ids: list[str] = []
        if name in TOOL_SOURCES and not event.cancel_message:
            records = self.ctx.evidence.add_text(name, args, _result_text(event.result))
            ids = [r.evidence_id for r in records]
            if any(r.status == "error" for r in records):
                status = "error"
        self.ctx.tool_calls.append(
            ToolCallRecord(
                agent=self.agent_name,
                tool=name,
                arguments=args,
                status=status,
                duration_ms=round(event.duration * 1000, 2) if event.duration is not None else None,
                evidence_ids=ids,
            )
        )


class AuditHook(HookProvider):
    """Writes one audit log row per tool call: who called what, with a hash of the arguments."""

    def __init__(self, ctx: RunContext, db_path: Path, step: str) -> None:
        self.ctx = ctx
        self.db_path = db_path
        self.step = step

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self.on_result)

    def on_result(self, event: AfterToolCallEvent) -> None:
        args = event.tool_use["input"]
        digest = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:16]
        status = "cancelled" if event.cancel_message else event.result.get("status", "error")
        write_audit(
            self.db_path,
            self.ctx.alert_id,
            self.step,
            status,
            tool=event.tool_use["name"],
            arguments_hash=digest,
            detail=(event.cancel_message or "")[:300],
        )


def write_audit(
    db_path: Path,
    alert_id: str,
    step: str,
    status: str,
    *,
    tool: str | None = None,
    arguments_hash: str | None = None,
    detail: str = "",
) -> None:
    with session(db_path) as conn:
        conn.execute(
            "INSERT INTO audit_log (alert_id, step, tool, arguments_hash, result_status, detail, timestamp) "
            "VALUES (?,?,?,?,?,?,?)",
            (alert_id, step, tool, arguments_hash, status, detail, datetime.now(UTC).isoformat()),
        )
