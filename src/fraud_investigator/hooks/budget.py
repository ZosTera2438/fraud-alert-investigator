"""BudgetHook: caps tool calls and tokens per alert, across both agents.

A breach cancels further data calls and model calls and is recorded on the run,
which limits the policy guard to escalate. `submit_decision` is exempt so the
decision agent can still record the escalation.
"""

from __future__ import annotations

from typing import Any

from strands.hooks import (
    AfterInvocationEvent,
    BeforeInvocationEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)

from ..state import RunContext

EXEMPT_TOOLS = {"submit_decision", "ReportDraft"}


class BudgetHook(HookProvider):
    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self._tokens_at_start = 0

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self.on_start)
        registry.add_callback(BeforeToolCallEvent, self.on_tool)
        registry.add_callback(BeforeModelCallEvent, self.on_model)
        registry.add_callback(AfterInvocationEvent, self.on_end)

    @staticmethod
    def _agent_tokens(event: Any) -> int:
        usage = event.agent.event_loop_metrics.accumulated_usage
        return int(usage.get("totalTokens", 0))

    def on_start(self, event: BeforeInvocationEvent) -> None:
        self._tokens_at_start = self._agent_tokens(event)

    def on_end(self, event: AfterInvocationEvent) -> None:
        self.ctx.budget.tokens_used += self._agent_tokens(event) - self._tokens_at_start
        self._tokens_at_start = self._agent_tokens(event)

    def _breach(self, reason: str) -> str:
        if not self.ctx.budget.breached:
            self.ctx.budget.breached = reason
        return (
            f"BUDGET_EXHAUSTED: {reason}. Do not call more data tools. Summarise what you have; "
            "the alert will be escalated."
        )

    def on_tool(self, event: BeforeToolCallEvent) -> None:
        if event.tool_use["name"] in EXEMPT_TOOLS:
            return
        b = self.ctx.budget
        if b.tool_calls_used >= b.max_tool_calls:
            event.cancel_tool = self._breach(f"tool call limit {b.max_tool_calls} reached")
            return
        b.tool_calls_used += 1

    def on_model(self, event: BeforeModelCallEvent) -> None:
        b = self.ctx.budget
        used = b.tokens_used + self._agent_tokens(event) - self._tokens_at_start
        if used >= b.max_total_tokens:
            event.cancel = self._breach(f"token limit {b.max_total_tokens} reached ({used} used)")
