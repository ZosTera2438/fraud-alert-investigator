from __future__ import annotations

from pathlib import Path

import pytest

from fraud_investigator.schemas import Alert
from fraud_investigator.scoring.engine import ScoringConfig, ScoringEngine
from fraud_investigator.tools.backend import DataBackend
from fraud_investigator.tools.collect import collect_all
from fraud_investigator.tools.evidence import EvidenceStore

ENGINE = ScoringEngine()


def score(db: Path, alert_id: str, drop: str | None = None):
    backend = DataBackend(db, retry_wait_seconds=0)
    alert = Alert.model_validate(backend.load_alert(alert_id))
    store = collect_all(backend, alert)
    if drop:
        store.records = {k: v for k, v in store.records.items() if v.source != drop}
    return ENGINE.score(alert, store)


@pytest.mark.parametrize(
    ("score_value", "band"),
    [(0, "allow"), (39, "allow"), (40, "escalate"), (74, "escalate"), (75, "block"), (100, "block")],
)
def test_bands(score_value: int, band: str) -> None:
    assert ScoringConfig.load().band(score_value) == band


def test_every_golden_scenario_lands_in_its_band(generated: Path, cases: dict) -> None:
    db = generated / "fraud.db"
    for scenario, items in cases.items():
        if not items[0]["case_id"].startswith("GLD"):
            continue
        for case in items:
            a = score(db, case["alert_id"])
            decision = "escalate" if a.incomplete else a.band
            assert decision == case["expected_decision"], (scenario, case["alert_id"], a.score)


@pytest.mark.parametrize(
    ("scenario", "signals"),
    [
        ("card_testing", {"card_testing", "velocity", "merchant_risk", "new_device"}),
        ("account_takeover", {"account_change", "new_device", "account_takeover_pattern"}),
        ("impossible_travel", {"impossible_travel", "travel_fraud_pattern"}),
        ("risky_device", {"device_risk", "compromised_device_pattern"}),
        ("first_party_fraud", {"dispute_history"}),
        ("legit_traveller", {"travel_notice"}),
        ("large_normal_purchase", {"known_merchant", "trusted_device"}),
    ],
)
def test_expected_signals_fire(generated: Path, cases: dict, scenario: str, signals: set[str]) -> None:
    a = score(generated / "fraud.db", cases[scenario][0]["alert_id"])
    fired = {s.name for s in a.signals if s.fired}
    assert signals <= fired


def test_signals_cite_their_evidence(generated: Path, cases: dict) -> None:
    a = score(generated / "fraud.db", cases["account_takeover"][0]["alert_id"])
    for s in a.signals:
        if s.kind != "combination":
            assert s.evidence_id and s.evidence_id.startswith("EV-")


def test_missing_source_makes_assessment_incomplete(generated: Path, cases: dict) -> None:
    a = score(generated / "fraud.db", cases["card_testing"][0]["alert_id"], drop="device_metadata")
    assert a.incomplete and a.missing_sources == ["device_metadata"]
    assert a.allowed_decisions == {"escalate"}


def test_failed_source_is_reported(generated: Path, cases: dict) -> None:
    a = score(generated / "fraud.db", cases["device_timeout"][0]["alert_id"])
    assert a.incomplete and a.failed_sources == ["device_metadata"]


def test_evidence_for_another_merchant_is_ignored(generated: Path, cases: dict) -> None:
    backend = DataBackend(generated / "fraud.db")
    alert = Alert.model_validate(backend.load_alert(cases["card_testing"][0]["alert_id"]))
    store = collect_all(backend, alert)
    other = collect_all(backend, Alert.model_validate(backend.load_alert(cases["legit_traveller"][0]["alert_id"])))
    merged = EvidenceStore(
        records={
            **{k: v for k, v in store.records.items() if v.source != "merchant_profile"},
            **{k: v for k, v in other.records.items() if v.source == "merchant_profile"},
        }
    )
    assert ENGINE.score(alert, merged).missing_sources == ["merchant_profile"]


def test_score_is_capped_and_floored(generated: Path, cases: dict) -> None:
    for scenario in ("risky_device", "large_normal_purchase"):
        a = score(generated / "fraud.db", cases[scenario][0]["alert_id"])
        assert 0 <= a.score <= 100
