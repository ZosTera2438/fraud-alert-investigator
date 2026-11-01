"""Redaction, policy guard, idempotent actions and model wrappers."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from strands.types.exceptions import ModelThrottledException

from fraud_investigator.actions import ActionService
from fraud_investigator.db import session
from fraud_investigator.graph import mask_alert
from fraud_investigator.hooks.policy_guard import violations
from fraud_investigator.hooks.redaction import redact, redact_message
from fraud_investigator.models import FallbackModel, RecordingModel, ReplayModel, _request_key
from fraud_investigator.schemas import Alert, InvestigationReport
from fraud_investigator.scoring.engine import ScoringEngine
from fraud_investigator.scripted_model import ScriptedModel
from fraud_investigator.state import Budget, RunContext
from fraud_investigator.tools.backend import DataBackend
from fraud_investigator.tools.collect import collect_all


# ---------- redaction ----------
@pytest.mark.parametrize(
    ("text", "leak"),
    [
        ("card 4111 1111 1111 1234 used", "4111 1111 1111"),
        ("pan=4111111111111234", "411111111111"),
        ("contact jane.doe@example.com now", "jane.doe@"),
        ("call 0412 345 678", "0412 345 678"),
        ("call +61 2 9876 5432", "9876 5432"),
    ],
)
def test_redaction_removes_personal_data(text: str, leak: str) -> None:
    assert leak not in redact(text)


@pytest.mark.parametrize("text", ["amount 1899.00 AUD", "at 2026-10-29T09:13:41Z", "EV-TXH-1a2b3c4d", "CUS-0042"])
def test_redaction_keeps_ordinary_values(text: str) -> None:
    assert redact(text) == text


def test_redaction_rewrites_tool_results_in_place() -> None:
    message = {
        "role": "user",
        "content": [
            {"toolResult": {"toolUseId": "t", "status": "success", "content": [{"text": "email bob@example.org"}]}}
        ],
    }
    assert redact_message(message) == 1
    assert "bob@" not in message["content"][0]["toolResult"]["content"][0]["text"]


# ---------- policy guard ----------
def context_for(db: Path, alert_id: str) -> RunContext:
    backend = DataBackend(db, retry_wait_seconds=0)
    alert = Alert.model_validate(backend.load_alert(alert_id))
    ctx = RunContext(alert=alert, masked_alert=mask_alert(alert), budget=Budget(max_tool_calls=12, max_total_tokens=1))
    ctx.evidence = collect_all(backend, alert)
    ctx.assessment = ScoringEngine().score(alert, ctx.evidence)
    return ctx


def test_guard_accepts_band_decision_with_known_evidence(generated: Path, cases: dict) -> None:
    ctx = context_for(generated / "fraud.db", cases["card_testing"][0]["alert_id"])
    args = {"alert_id": ctx.alert_id, "decision": "block", "cited_evidence_ids": ctx.assessment.fired_evidence_ids}
    assert violations(ctx, args) == []
    assert violations(ctx, {**args, "decision": "escalate"}) == []  # escalate is always allowed


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"decision": "allow"}, "not allowed"),
        ({"cited_evidence_ids": []}, "cite at least one"),
        ({"cited_evidence_ids": ["EV-FAKE-1"]}, "unknown evidence"),
        ({"alert_id": "ALT-999999"}, "alert_id must be"),
    ],
)
def test_guard_rejects(generated: Path, cases: dict, change: dict, fragment: str) -> None:
    ctx = context_for(generated / "fraud.db", cases["card_testing"][0]["alert_id"])
    args = {
        "alert_id": ctx.alert_id,
        "decision": "block",
        "cited_evidence_ids": ctx.assessment.fired_evidence_ids,
        **change,
    }
    assert any(fragment in v for v in violations(ctx, args))


def test_guard_allows_only_escalate_after_budget_breach(generated: Path, cases: dict) -> None:
    ctx = context_for(generated / "fraud.db", cases["card_testing"][0]["alert_id"])
    ctx.budget.breached = "tool call limit"
    args = {"alert_id": ctx.alert_id, "decision": "block", "cited_evidence_ids": ctx.assessment.fired_evidence_ids}
    assert violations(ctx, args)


# ---------- actions ----------
def report(alert_id: str, decision: str) -> InvestigationReport:
    return InvestigationReport(
        alert_id=alert_id,
        risk_score=90,
        score_breakdown=[],
        recommendation=decision,  # type: ignore[arg-type]
        confidence="high",
        key_findings=[],
        mitigating_factors=[],
        analyst_summary="x",
    )


def test_actions_are_idempotent(db_path: Path, cases: dict) -> None:
    alert = DataBackend(db_path).load_alert(cases["card_testing"][0]["alert_id"])
    service = ActionService(db_path)
    first = service.execute(report(alert["alert_id"], "block"), alert["card_token"], approver="ana")
    second = service.execute(report(alert["alert_id"], "block"), alert["card_token"], approver="ana")
    assert not first.replayed and second.replayed and first.case_id == second.case_id
    assert any(a.startswith("block_card") for a in first.actions) and second.actions == []
    with session(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM cases WHERE alert_id=?", (alert["alert_id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT status FROM cards WHERE token=?", (alert["card_token"],)).fetchone()[0] == "blocked"


def test_block_without_approver_is_refused(db_path: Path, cases: dict) -> None:
    alert = DataBackend(db_path).load_alert(cases["card_testing"][0]["alert_id"])
    with pytest.raises(PermissionError):
        ActionService(db_path).execute(report(alert["alert_id"], "block"), alert["card_token"], approver=None)


# ---------- model wrappers ----------
class Throttled(ScriptedModel):
    async def stream(self, *args, **kwargs):  # type: ignore[override]
        raise ModelThrottledException("rate limited")
        yield {}


def test_fallback_model_switches_on_throttle() -> None:
    model = FallbackModel(Throttled(), ScriptedModel(model_id="backup"))

    async def run() -> list:
        return [e async for e in model.stream([{"role": "user", "content": [{"text": "hi"}]}], [], None)]

    events = asyncio.run(run())
    assert model.fell_back and model.get_config()["model_id"] == "backup"
    assert any("messageStop" in e for e in events)


def test_recording_then_replay_returns_the_same_events(tmp_path: Path) -> None:
    messages = [{"role": "user", "content": [{"text": '<alert>{"alert_id": "ALT-000001"}</alert>'}]}]
    specs = [{"name": "get_transaction_history", "description": "", "inputSchema": {"json": {}}}]

    async def collect(model) -> list:  # type: ignore[no-untyped-def]
        return [e async for e in model.stream(messages, specs, "sys")]

    recorded = asyncio.run(collect(RecordingModel(ScriptedModel(), tmp_path)))
    assert asyncio.run(collect(ReplayModel(tmp_path))) == recorded


def test_request_key_ignores_volatile_fields() -> None:
    a = [{"role": "user", "content": [{"text": '{"retrieved_at":"2026-01-01T00:00:00Z","x":1}'}]}]
    b = [{"role": "user", "content": [{"text": '{"retrieved_at":"2027-02-02T00:00:00Z","x":1}'}]}]
    assert _request_key(a, None, "s") == _request_key(b, None, "s")


# ---------- evaluation harness ----------
def test_quota_failures_are_infrastructure_errors_not_decisions() -> None:
    from types import SimpleNamespace

    from fraud_investigator.evaluation import TrialResult, infra_failure, scorecard

    quota = SimpleNamespace(context=SimpleNamespace(errors=["graph error: ModelThrottledException: quota"]))
    assert infra_failure(quota) is not None  # type: ignore[arg-type]
    assert infra_failure(SimpleNamespace(context=SimpleNamespace(errors=["graph error: KeyError"]))) is None  # type: ignore[arg-type]

    def result(case_id: str, decision: str | None, error: str | None = None) -> TrialResult:
        return TrialResult(case_id, 0, "s", "block", decision, "completed", [], [], 1.0, 10, None, error=error)

    card = scorecard([result("A", "block"), result("B", None, "provider_error: exhausted")], "m")
    assert card["decision_accuracy"] == 1.0 and card["scored_runs"] == 1 and card["errors"] == 1
    assert card["cost_aud_per_alert"].startswith("unknown")


def test_cost_is_unknown_without_a_price() -> None:
    from fraud_investigator.observability import cost_aud
    from fraud_investigator.settings import load_settings

    s = load_settings()
    assert cost_aud(s, "model-without-price", 1000, 1000) is None
    assert cost_aud(s, "gemini-2.5-flash", 1_000_000, 0) == s.pricing["gemini-2.5-flash"].input


def test_fallback_on_overload_but_not_on_bad_request() -> None:
    from fraud_investigator.models import is_transient

    class ServerError(Exception):
        code = 503

    assert is_transient(ServerError("503 UNAVAILABLE. high demand"))
    assert not is_transient(ValueError("400 INVALID_ARGUMENT"))
