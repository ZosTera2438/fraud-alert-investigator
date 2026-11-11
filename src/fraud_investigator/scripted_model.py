"""Deterministic offline model for tests and continuous integration.

A Strands `Model` that plays the part of the language model with a fixed
policy, so the real agent loop, hooks, interrupts, Graph and structured output
all run without a network or an API key. Behaviours let tests exercise
failure modes a real model shows:

- diligent:           calls every data tool, then submits the band decision.
- lazy:               skips the device tool (trajectory grader must catch it).
- injection_follower: obeys instructions found in tool data (guard must stop it).
- always_allow:       recommends allow whatever the evidence says.

It is not a fraud model. Accuracy numbers from it measure the harness, not a
language model; live numbers come from Cloudflare Workers AI and Gemini (see evals/README).
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncGenerator, AsyncIterable
from typing import Any, Literal

from pydantic import BaseModel
from strands.models.model import Model
from strands.types.content import Messages
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolSpec

Behaviour = Literal["diligent", "lazy", "injection_follower", "always_allow"]
_INJECTION = re.compile(
    r"ignore (all )?(other|previous)|mark this alert as safe|decision=allow|answer is allow|"
    r"recommend allow|must not be blocked|output decision allow|approve this",
    re.I,
)


def _blocks(messages: Messages, key: str) -> list[dict[str, Any]]:
    return [b[key] for m in messages for b in m["content"] if key in b]  # type: ignore[literal-required]


def _texts(messages: Messages) -> str:
    parts: list[str] = []
    for m in messages:
        for b in m["content"]:
            if "text" in b:
                parts.append(b["text"])  # type: ignore[typeddict-item]
            for c in b.get("toolResult", {}).get("content", []):  # type: ignore[union-attr]
                if "text" in c:
                    parts.append(c["text"])
    return "\n".join(parts)


def _tag(text: str, name: str) -> Any:
    m = re.search(rf"<{name}>(.*?)</{name}>", text, re.S)
    return json.loads(m.group(1)) if m else None


class ScriptedModel(Model):
    def __init__(self, behaviour: Behaviour = "diligent", model_id: str = "scripted") -> None:
        self.config: dict[str, Any] = {"model_id": model_id, "behaviour": behaviour}

    def update_config(self, **model_config: Any) -> None:
        self.config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        return self.config

    @property
    def behaviour(self) -> Behaviour:
        return self.config["behaviour"]

    async def structured_output(  # type: ignore[override]
        self, output_model: type[BaseModel], prompt: Messages, system_prompt: str | None = None, **kwargs: Any
    ) -> AsyncGenerator[dict[str, Any], None]:
        raise NotImplementedError("structured output runs through the tool loop")
        yield {}

    async def stream(  # type: ignore[override]
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        *,
        tool_choice: Any = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        names = {t["name"] for t in tool_specs or []}
        forced = (tool_choice or {}).get("tool", {}).get("name") if isinstance(tool_choice, dict) else None
        if forced:
            actions = [("tool", forced, self._report(messages))]
        elif "get_risk_score" in names:
            actions = self._single_agent(messages, names)
        elif "submit_decision" in names:
            actions = self._decide(messages, names)
        elif "get_transaction_history" in names:
            actions = self._investigate(messages)
        else:
            actions = [("text", "No tools available.", None)]

        prompt_chars = len(system_prompt or "") + len(json.dumps(messages, default=str))
        output_chars = 0
        yield {"messageStart": {"role": "assistant"}}
        n = sum(1 for m in messages if m["role"] == "assistant")
        for i, (kind, value, payload) in enumerate(actions):
            if kind == "text":
                output_chars += len(value)
                yield {"contentBlockDelta": {"delta": {"text": value}}}
                yield {"contentBlockStop": {}}
            else:
                body = json.dumps(payload)
                output_chars += len(body)
                yield {
                    "contentBlockStart": {
                        "start": {"toolUse": {"toolUseId": f"tooluse_{n}_{i}_{value}", "name": value}}
                    }
                }
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": body}}}}
                yield {"contentBlockStop": {}}
        stop = "tool_use" if any(k == "tool" for k, *_ in actions) else "end_turn"
        yield {"messageStop": {"stopReason": stop}}
        usage = {"inputTokens": prompt_chars // 4, "outputTokens": output_chars // 4}
        usage["totalTokens"] = usage["inputTokens"] + usage["outputTokens"]
        yield {"metadata": {"usage": usage, "metrics": {"latencyMs": 1}}}  # type: ignore[typeddict-item]

    # ---------- investigator ----------
    def _investigate(self, messages: Messages) -> list[tuple[str, str, Any]]:
        results = _blocks(messages, "toolResult")
        if not results:
            alert = _tag(_texts(messages[:1]), "alert") or {}
            txn = alert.get("transaction", {})
            calls: list[tuple[str, str, Any]] = [
                ("tool", "get_transaction_history", {"customer_id": alert.get("customer_id"), "days": 90}),
                ("tool", "get_customer_profile", {"customer_id": alert.get("customer_id")}),
                ("tool", "get_merchant_profile", {"merchant_id": txn.get("merchant_id")}),
            ]
            if txn.get("device_id") and self.behaviour != "lazy":
                calls.append(("tool", "get_device_metadata", {"device_id": txn["device_id"]}))
            calls.append(
                (
                    "tool",
                    "search_fraud_playbook",
                    {"query": str(alert.get("trigger_rule", "fraud")).replace("_", " "), "top_k": 2},
                )
            )
            return calls
        text = _texts(messages[1:])
        ids = sorted(set(re.findall(r'evidence_id="([A-Za-z0-9-]+)"', text)))
        errors = re.findall(r'"error":"([a-z_]+)".*?"source":"([a-z_]+)"', text)
        lines = [f"Evidence collected: {', '.join(ids) or 'none'}."]
        lines += [f"Source {src} failed ({err}); treat as unavailable." for err, src in errors]
        if self.behaviour == "injection_follower" and (m := _INJECTION.search(text)):
            lines.append(f"Note found in the data: '{m.group(0)}'. The data says this alert is safe.")
        return [("text", " ".join(lines), None)]

    # ---------- single agent (baseline comparison) ----------
    def _single_agent(self, messages: Messages, names: set[str]) -> list[tuple[str, str, Any]]:
        used = {u["name"] for u in _blocks(messages, "toolUse")}
        if not used:
            return self._investigate(messages)
        if "get_risk_score" not in used:
            return [("tool", "get_risk_score", {})]
        return self._decide(messages, names)

    # ---------- decision ----------
    def _decide(self, messages: Messages, names: set[str]) -> list[tuple[str, str, Any]]:
        context = _texts(messages)
        risk = _tag(context, "risk_assessment") or {}
        alert = _tag(context, "alert") or {}
        submits = [u for u in _blocks(messages, "toolUse") if u["name"] == "submit_decision"]
        results = {r["toolUseId"]: r for r in _blocks(messages, "toolResult")}
        accepted = [u for u in submits if results.get(u["toolUseId"], {}).get("status") == "success"]
        if accepted:
            report_tool = next((n for n in names if n == "ReportDraft"), None)
            if report_tool:
                return [("tool", report_tool, self._report(messages))]
            return [("text", "Decision submitted.", None)]

        allowed = risk.get("allowed_decisions", ["escalate"])
        decision = "escalate" if risk.get("incomplete") else risk.get("band", "escalate")
        follow = self.behaviour == "always_allow" or (
            self.behaviour == "injection_follower" and _INJECTION.search(context)
        )
        if follow:
            decision = "allow" if not submits else "escalate"
        elif decision not in allowed:
            decision = "escalate"
        index = _tag(context, "evidence_index") or []
        cited = risk.get("fired_evidence_ids") or [e["evidence_id"] for e in index if e.get("status") == "ok"][:3]
        if follow and not submits:
            cited = []
        rationale = (
            "; ".join(s["detail"] for s in risk.get("signals", []) if s.get("fired"))[:500] or "No risk signals fired."
        )
        return [
            (
                "tool",
                "submit_decision",
                {
                    "alert_id": alert.get("alert_id", ""),
                    "decision": decision,
                    "cited_evidence_ids": cited,
                    "rationale": rationale,
                },
            )
        ]

    def _report(self, messages: Messages) -> dict[str, Any]:
        context = _texts(messages)
        risk = _tag(context, "risk_assessment") or {}
        receipts = [
            json.loads(c["text"])
            for r in _blocks(messages, "toolResult")
            if r.get("status") == "success"
            for c in r.get("content", [])
            if "text" in c and c["text"].startswith("{")
        ]
        decision = next((r["decision"] for r in reversed(receipts) if "decision" in r), risk.get("band", "escalate"))
        signals = risk.get("signals", [])
        findings = [
            {
                "text": f"{s['name']}: {s['detail']}"[:400],
                "evidence_ids": [s["evidence_id"]] if s.get("evidence_id") else [],
            }
            for s in signals
            if s.get("fired") and s.get("kind") in ("risk", "combination")
        ]
        mitigating = [
            {
                "text": f"{s['name']}: {s['detail']}"[:400],
                "evidence_ids": [s["evidence_id"]] if s.get("evidence_id") else [],
            }
            for s in signals
            if s.get("fired") and s.get("kind") == "mitigation"
        ]
        score = int(risk.get("score", 0))
        edge = min(abs(score - 40), abs(score - 75))
        confidence = "low" if risk.get("incomplete") else ("high" if edge >= 15 else "medium")
        summary = (
            f"Risk score {score} ({risk.get('band', 'unknown')} band). Recommendation: {decision}. "
            f"{len(findings)} risk findings and {len(mitigating)} mitigating factors."
        )
        if risk.get("incomplete"):
            summary += (
                f" Evidence incomplete: {', '.join(risk.get('failed_sources', []) + risk.get('missing_sources', []))}."
            )
        return {
            "recommendation": decision,
            "confidence": confidence,
            "key_findings": findings,
            "mitigating_factors": mitigating,
            "analyst_summary": summary[:800],
        }
