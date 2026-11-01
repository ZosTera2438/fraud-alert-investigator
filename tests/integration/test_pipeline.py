"""End to end through the Strands Graph with the scripted model."""

from __future__ import annotations

from pathlib import Path

from fraud_investigator.db import session
from fraud_investigator.graph import InvestigationPipeline
from fraud_investigator.settings import Settings

APPROVE = {"approved": True, "approver": "ana", "note": "confirmed"}
REJECT = {"approved": False, "approver": "ana", "note": "customer confirmed purchase"}


def card_status(db: Path, alert_id: str) -> str:
    with session(db) as conn:
        return conn.execute(
            "SELECT c.status FROM cards c JOIN alerts a ON a.card_token=c.token WHERE a.id=?", (alert_id,)
        ).fetchone()[0]


def count_cases(db: Path, alert_id: str) -> int:
    with session(db) as conn:
        return conn.execute("SELECT COUNT(*) FROM cases WHERE alert_id=?", (alert_id,)).fetchone()[0]


def test_allow_completes_without_approval(settings: Settings, cases: dict) -> None:
    alert_id = cases["legit_traveller"][0]["alert_id"]
    outcome = InvestigationPipeline(settings).start(alert_id)
    assert outcome.status == "completed" and outcome.report.recommendation == "allow"
    assert outcome.report.decision_source == "agent" and outcome.context.draft is not None
    tools = {t.tool for t in outcome.context.tool_calls}
    assert {"get_transaction_history", "get_customer_profile", "get_merchant_profile", "submit_decision"} <= tools


def test_block_pauses_for_approval_then_blocks(settings: Settings, cases: dict) -> None:
    alert_id = cases["account_takeover"][0]["alert_id"]
    pipeline = InvestigationPipeline(settings)
    paused = pipeline.start(alert_id)
    assert paused.status == "awaiting_approval" and paused.report is None
    assert paused.context.pending_approval["decision"] == "block"
    assert card_status(settings.database_path, alert_id) == "active"
    assert count_cases(settings.database_path, alert_id) == 0

    done = pipeline.resume(alert_id, APPROVE)
    assert done.report.recommendation == "block" and done.context.approval.approver == "ana"
    assert card_status(settings.database_path, alert_id) == "blocked"


def test_rejected_block_becomes_escalate(settings: Settings, cases: dict) -> None:
    alert_id = cases["card_testing"][0]["alert_id"]
    pipeline = InvestigationPipeline(settings)
    pipeline.start(alert_id)
    done = pipeline.resume(alert_id, REJECT)
    assert done.report.recommendation == "escalate" and done.report.decision_source == "analyst"
    assert card_status(settings.database_path, alert_id) == "active"


def test_resume_after_restart_uses_session_files(settings: Settings, cases: dict) -> None:
    alert_id = cases["risky_device"][0]["alert_id"]
    InvestigationPipeline(settings).start(alert_id)
    done = InvestigationPipeline(settings).resume(alert_id, APPROVE)  # a new process in all but name
    assert done.report.recommendation == "block"
    assert card_status(settings.database_path, alert_id) == "blocked"


def test_replay_creates_no_duplicate_case(settings: Settings, cases: dict) -> None:
    alert_id = cases["high_risk_merchant"][0]["alert_id"]
    first = InvestigationPipeline(settings).start(alert_id)
    second = InvestigationPipeline(settings).start(alert_id)
    assert first.context.case_id == second.context.case_id
    assert second.context.actions == [] and count_cases(settings.database_path, alert_id) == 1


def test_high_value_allow_needs_approval(settings: Settings, cases: dict) -> None:
    case = next(c for c in cases["large_normal_purchase"] if "approval_threshold" in c["tags"])
    pipeline = InvestigationPipeline(settings)
    paused = pipeline.start(case["alert_id"])
    assert paused.status == "awaiting_approval" and paused.context.pending_approval["decision"] == "allow"
    assert pipeline.resume(case["alert_id"], APPROVE).report.recommendation == "allow"


def test_tool_timeout_fails_safe_to_escalate(settings: Settings, cases: dict) -> None:
    outcome = InvestigationPipeline(settings).start(cases["device_timeout"][0]["alert_id"])
    assert outcome.report.recommendation == "escalate"
    assert outcome.context.assessment.failed_sources == ["device_metadata"]


def test_budget_breach_escalates(settings: Settings, cases: dict) -> None:
    case = cases["budget_exhausted"][0]
    outcome = InvestigationPipeline(settings, overrides=case["overrides"]).start(case["alert_id"])
    assert outcome.context.budget.breached and outcome.report.recommendation == "escalate"
    assert any(t.status == "cancelled" for t in outcome.context.tool_calls)


def test_injection_following_model_cannot_allow(settings: Settings, cases: dict) -> None:
    case = cases["adversarial_model"][0]
    outcome = InvestigationPipeline(settings, behaviour="injection_follower").start(case["alert_id"])
    assert outcome.report.recommendation == "escalate"
    assert outcome.context.policy_rejections >= 1
    assert outcome.report.decision_source in {"agent", "policy_override"}


def test_always_allow_model_is_overridden(settings: Settings, cases: dict) -> None:
    outcome = InvestigationPipeline(settings, behaviour="always_allow").start(cases["account_takeover"][1]["alert_id"])
    assert outcome.report.recommendation == "escalate"


def test_lazy_investigation_escalates(settings: Settings, cases: dict) -> None:
    outcome = InvestigationPipeline(settings, behaviour="lazy").start(cases["card_testing"][1]["alert_id"])
    assert "get_device_metadata" not in {t.tool for t in outcome.context.tool_calls}
    assert outcome.report.recommendation == "escalate"


def test_report_score_comes_from_engine_and_citations_are_grounded(settings: Settings, cases: dict) -> None:
    pipeline = InvestigationPipeline(settings)
    alert_id = cases["impossible_travel"][0]["alert_id"]
    pipeline.start(alert_id)
    outcome = pipeline.resume(alert_id, APPROVE)
    known = outcome.context.evidence.known_ids()
    assert outcome.report.risk_score == outcome.context.assessment.score
    for finding in outcome.report.key_findings:
        assert set(finding.evidence_ids) <= known


def test_audit_log_records_every_step(settings: Settings, cases: dict) -> None:
    alert_id = cases["legit_traveller"][1]["alert_id"]
    InvestigationPipeline(settings).start(alert_id)
    with session(settings.database_path) as conn:
        steps = {r[0] for r in conn.execute("SELECT step FROM audit_log WHERE alert_id=?", (alert_id,))}
    assert {"intake", "investigator", "scoring", "decision", "action"} <= steps
