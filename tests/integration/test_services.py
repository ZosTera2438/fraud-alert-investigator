"""API, MCP transport, baselines and the evaluation gate."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fraud_investigator.baseline.raw_agent_loop import RawAgentLoop
from fraud_investigator.baseline.single_agent import SingleAgentPipeline
from fraud_investigator.evaluation import gate, load_thresholds, run_suite, scorecard
from fraud_investigator.graph import InvestigationPipeline
from fraud_investigator.settings import Settings


def test_api_investigate_and_approve(settings: Settings, cases: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from fraud_investigator.api import app as api

    api.pipeline.cache_clear()
    monkeypatch.setattr(api, "pipeline", lambda: _shared(settings))
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    client = TestClient(api.app)
    alert_id = cases["card_testing"][2]["alert_id"]

    assert client.get("/health").json()["model_provider"] == "scripted"
    run = client.post(f"/alerts/{alert_id}/investigate").json()
    assert run["status"] == "awaiting_approval"
    assert [p["alert_id"] for p in client.get("/approvals").json()] == [alert_id]
    done = client.post(f"/runs/{alert_id}/approval", json={"approved": True, "approver": "ana"}).json()
    assert done["report"]["recommendation"] == "block" and done["case_id"]
    assert client.get(f"/runs/{alert_id}").json()["status"] == "completed"
    assert any(c["alert_id"] == alert_id for c in client.get("/cases").json())
    assert client.get(f"/audit/{alert_id}").json()
    assert client.post("/alerts/ALT-999999/investigate").status_code == 404
    assert client.post(f"/runs/{alert_id}/approval", json={"approved": True, "approver": "ana"}).status_code == 409


_PIPELINES: dict[str, InvestigationPipeline] = {}


def _shared(settings: Settings) -> InvestigationPipeline:
    return _PIPELINES.setdefault(str(settings.database_path), InvestigationPipeline(settings))


def test_tools_over_mcp_match_local_tools(settings: Settings, cases: dict) -> None:
    alert_id = cases["first_party_fraud"][0]["alert_id"]
    local = InvestigationPipeline(settings).start(alert_id)
    settings.tools.transport = "mcp"
    remote = InvestigationPipeline(settings).start(alert_id)
    assert remote.report.recommendation == local.report.recommendation == "escalate"
    assert remote.context.evidence.known_ids() == local.context.evidence.known_ids()


@pytest.mark.parametrize("cls", [SingleAgentPipeline, RawAgentLoop])
def test_baselines_reach_the_same_decisions(settings: Settings, cases: dict, cls: type) -> None:
    for scenario in ("card_testing", "legit_traveller", "device_timeout"):
        case = cases[scenario][3]
        pipeline = cls(settings)
        outcome = pipeline.start(case["alert_id"])
        while outcome.status == "awaiting_approval":
            outcome = pipeline.resume(case["alert_id"], {"approved": True, "approver": "ana"})
        assert outcome.report.recommendation == case["expected_decision"], (cls.__name__, scenario)


def test_eval_suite_passes_and_gate_catches_regression(settings: Settings, cases: dict) -> None:
    sample = [items[0] for items in cases.values()]
    good = scorecard(run_suite(sample, settings=settings, trials=1, source_db=Path(settings.database_path)), "scripted")
    assert gate(good, load_thresholds()) == []
    golden = [items[0] for name, items in cases.items() if items[0]["case_id"].startswith("GLD")]
    lazy = scorecard(
        run_suite(
            golden, settings=settings, trials=1, source_db=Path(settings.database_path), behaviour_override="lazy"
        ),
        "lazy",
    )
    failures = gate(lazy, load_thresholds())
    assert any(f.startswith("required_tools") for f in failures)
