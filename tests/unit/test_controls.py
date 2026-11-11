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


def test_cloudflare_provider_uses_openai_compatible_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    from strands.models.openai import OpenAIModel

    from fraud_investigator.models import build_provider
    from fraud_investigator.settings import load_settings

    monkeypatch.setenv("FRAUD_MODEL_PROVIDER", "cloudflare")
    monkeypatch.setenv("FRAUD_MODEL_ID", "@cf/meta/llama-3.3-70b-instruct-fp8-fast")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct123")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "testing")  # building the client needs no network
    s = load_settings()
    model = build_provider("cloudflare", s)

    assert isinstance(model, OpenAIModel)
    assert s.model_id == "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
    config = model.get_config()
    assert config["model_id"] == s.model_id
    assert "reasoning_effort" not in config["params"]  # only sent to gpt-oss models
    assert model.client_args["base_url"] == "https://api.cloudflare.com/client/v4/accounts/acct123/ai/v1"
    # Workers AI rejects an empty tools array, so a tool-free request must leave the field out.
    messages = [{"role": "user", "content": [{"text": "hi"}]}]
    request = model.format_request(messages, system_prompt="sys")  # type: ignore[arg-type]
    assert "tools" not in request
    assert [m["content"] for m in request["messages"]] == ["sys", "hi"]  # text parts sent as plain strings
    spec = {"name": "t", "description": "d", "inputSchema": {"json": {"type": "object", "properties": {}}}}
    assert len(model.format_request(messages, [spec])["tools"]) == 1  # type: ignore[arg-type]


def test_cloudflare_provider_needs_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    from fraud_investigator.models import build_provider
    from fraud_investigator.settings import load_settings

    s = load_settings()
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="CLOUDFLARE_ACCOUNT_ID"):
        build_provider("cloudflare", s)


def test_framework_free_chat_speaks_openai_compatible_http() -> None:
    import json

    import httpx

    from fraud_investigator.baseline.raw_agent_loop import OpenAICompatibleChat

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["body"] = str(request.url), json.loads(request.content)
        seen["auth"] = request.headers["Authorization"]
        call = {
            "id": "c2",
            "type": "function",
            "function": {"name": "get_device_metadata", "arguments": '{"device_id": "D1"}'},
        }
        message = {"role": "assistant", "content": None, "tool_calls": [call]}
        return httpx.Response(
            200, json={"choices": [{"message": message}], "usage": {"prompt_tokens": 7, "completion_tokens": 3}}
        )

    chat = OpenAICompatibleChat("https://example.test/v1/", "key", "m", transport=httpx.MockTransport(handler))
    messages = [
        {"role": "user", "content": [{"text": "investigate"}]},
        {"role": "assistant", "content": [{"toolUse": {"toolUseId": "c1", "name": "t", "input": {"a": 1}}}]},
        {"role": "user", "content": [{"toolResult": {"toolUseId": "c1", "content": [{"text": "data"}]}}]},
    ]
    tool = {"name": "get_device_metadata", "description": "d", "parameters": {"type": "object", "properties": {}}}
    turn = chat.complete("sys", messages, [tool])

    assert seen["url"] == "https://example.test/v1/chat/completions" and seen["auth"] == "Bearer key"
    sent = seen["body"]["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "tool"]
    assert sent[2]["tool_calls"][0]["function"]["arguments"] == '{"a": 1}' and sent[3]["tool_call_id"] == "c1"
    assert turn.calls[0].name == "get_device_metadata" and turn.calls[0].args == {"device_id": "D1"}
    assert (turn.input_tokens, turn.output_tokens) == (7, 3)
    chat.complete("sys", messages[:1], [])
    assert "tools" not in seen["body"]


def test_cloudflare_falls_back_to_a_second_cloudflare_model(monkeypatch: pytest.MonkeyPatch) -> None:
    from fraud_investigator.models import FallbackModel, create_model, model_id_of
    from fraud_investigator.settings import load_settings

    monkeypatch.setenv("FRAUD_MODEL_PROVIDER", "cloudflare")
    monkeypatch.setenv("FRAUD_MODEL_ID", "@cf/deepseek-ai/deepseek-v4.1-flash-1017")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct123")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "testing")
    monkeypatch.delenv("FRAUD_MODEL_FALLBACK", raising=False)
    s = load_settings()
    s.model.fallback, s.model.fallback_model_id = "cloudflare", "@cf/zai-org/glm-5.3-flash"
    model = create_model(s)

    assert isinstance(model, FallbackModel)
    assert model_id_of(model.primary) == "@cf/deepseek-ai/deepseek-v4.1-flash-1017"
    assert model_id_of(model.secondary) == "@cf/zai-org/glm-5.3-flash"
    s.model.fallback_model_id = "@cf/deepseek-ai/deepseek-v4.1-flash-1017"
    assert not isinstance(create_model(s), FallbackModel)  # same model: no point falling back


def test_tracing_registers_the_provider_globally(monkeypatch: pytest.MonkeyPatch) -> None:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    from fraud_investigator import observability
    from fraud_investigator.settings import load_settings

    registered: list = []
    monkeypatch.setattr(trace, "set_tracer_provider", registered.append)
    monkeypatch.setattr(observability, "_configured", False)
    s = load_settings()
    s.observability.enabled = True
    assert observability.setup_tracing(s)
    # Strands' spans and the pipeline's spans read the global provider; an unregistered one exports nothing.
    assert len(registered) == 1 and isinstance(registered[0], TracerProvider)
    monkeypatch.setattr(observability, "_configured", False)
