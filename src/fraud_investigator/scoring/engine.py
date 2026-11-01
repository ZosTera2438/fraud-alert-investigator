"""Deterministic risk scoring.

Reads raw tool results from the evidence store, never the investigator's
summary, so a misread or injected text cannot move the score. Evidence is only
used when it matches the alert's own customer, merchant and device.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from ..datagen import haversine_km
from ..schemas import (
    Alert,
    CustomerProfile,
    Decision,
    DeviceMetadata,
    MerchantProfile,
    RiskAssessment,
    SignalResult,
    TransactionHistory,
)
from ..settings import project_root
from ..tools.evidence import EvidenceStore


class ScoringConfig(BaseModel):
    version: str
    bands: dict[str, int]
    thresholds: dict[str, float]
    signals: dict[str, int]
    combinations: dict[str, dict[str, Any]]
    mitigations: dict[str, int]
    ml_blend: dict[str, Any] = {}

    @classmethod
    def load(cls, path: Path | None = None) -> ScoringConfig:
        path = path or project_root() / "config" / "scoring.yaml"
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))

    def band(self, score: int) -> Decision:
        if score >= self.bands["block_from"]:
            return "block"
        if score >= self.bands["allow_below"]:
            return "escalate"
        return "allow"


class Evidence(BaseModel):
    """The raw records the engine scores, resolved for one alert."""

    history: TransactionHistory | None = None
    merchant: MerchantProfile | None = None
    device: DeviceMetadata | None = None
    customer: CustomerProfile | None = None
    missing: list[str] = []
    failed: list[str] = []


def resolve_evidence(alert: Alert, store: EvidenceStore) -> Evidence:
    txn = alert.transaction

    def pick(source: str, match: Callable[[Any], bool]) -> Any:
        candidates = [r.typed() for r in store.ok(source)]  # type: ignore[arg-type]
        candidates = [c for c in candidates if c is not None and match(c)]
        # Prefer the widest history window, otherwise the latest record.
        candidates.sort(key=lambda c: (getattr(c, "days", 0), c.retrieved_at))
        return candidates[-1] if candidates else None

    ev = Evidence(
        history=pick("transaction_history", lambda h: h.customer_id == alert.customer_id),
        merchant=pick("merchant_profile", lambda m: m.merchant_id == txn.merchant_id),
        device=pick("device_metadata", lambda d: d.device_id == txn.device_id) if txn.device_id else None,
        customer=pick("customer_profile", lambda c: c.customer_id == alert.customer_id),
    )
    required = {"transaction_history": ev.history, "merchant_profile": ev.merchant, "customer_profile": ev.customer}
    if txn.device_id:
        required["device_metadata"] = ev.device
    for source, value in required.items():
        if value is None:
            (ev.failed if store.failed(source) else ev.missing).append(source)  # type: ignore[arg-type]
    return ev


class ScoringEngine:
    def __init__(self, config: ScoringConfig | None = None) -> None:
        self.config = config or ScoringConfig.load()
        self._ml = None
        if self.config.ml_blend.get("enabled"):
            from .ml import BlendModel

            self._ml = BlendModel.load(project_root() / self.config.ml_blend["model_path"])

    def score(self, alert: Alert, store: EvidenceStore) -> RiskAssessment:
        ev = resolve_evidence(alert, store)
        signals = self.signals(alert, ev)
        fired = {s.name for s in signals if s.fired and s.kind == "risk"}
        for name, combo in self.config.combinations.items():
            hit = all(n in fired for n in combo["all_of"])
            signals.append(
                SignalResult(
                    name=name,
                    kind="combination",
                    fired=hit,
                    points=combo["points"] if hit else 0,
                    detail=" + ".join(combo["all_of"]),
                )
            )
        rule_score = max(0, min(100, sum(s.points for s in signals if s.fired)))
        score, probability = rule_score, None
        if self._ml is not None:
            probability = self._ml.probability(signals)
            weight = float(self.config.ml_blend.get("weight", 0.3))
            score = round((1 - weight) * rule_score + weight * 100 * probability)
            signals.append(
                SignalResult(
                    name="ml_blend",
                    kind="model",
                    fired=True,
                    points=score - rule_score,
                    detail=f"probability={probability:.3f}, weight={weight}",
                )
            )
        incomplete = bool(ev.missing or ev.failed)
        return RiskAssessment(
            score=score,
            band=self.config.band(score),
            signals=signals,
            rule_score=rule_score,
            model_probability=probability,
            incomplete=incomplete,
            missing_sources=ev.missing,
            failed_sources=ev.failed,
            scoring_version=self.config.version,
        )

    # ---------- individual signals ----------
    def signals(self, alert: Alert, ev: Evidence) -> list[SignalResult]:
        th, w, mit = self.config.thresholds, self.config.signals, self.config.mitigations
        txn = alert.transaction
        t = txn.timestamp
        out: list[SignalResult] = []

        def add(name: str, fired: bool, evidence_id: str | None, detail: str, kind: str = "risk") -> None:
            points = (w[name] if kind == "risk" else -mit[name]) if fired else 0
            out.append(
                SignalResult(
                    name=name,
                    kind=kind,  # type: ignore[arg-type]
                    fired=fired,
                    points=points,
                    evidence_id=evidence_id,
                    detail=detail,
                )
            )

        if ev.history:
            hid = ev.history.evidence_id
            prior = [
                x
                for x in ev.history.transactions
                if x.transaction_id != txn.transaction_id and x.timestamp <= t and x.timestamp >= t - timedelta(days=90)
            ]
            if len(prior) >= th["amount_min_history"]:
                amounts = [x.amount for x in prior]
                mean, std = statistics.fmean(amounts), statistics.pstdev(amounts)
                limit = mean + th["amount_sigma"] * std
                add(
                    "amount_anomaly",
                    txn.amount > limit,
                    hid,
                    f"amount {txn.amount:.2f} vs limit {limit:.2f} (mean {mean:.2f}, sd {std:.2f}, n={len(prior)})",
                )
            else:
                add("amount_anomaly", False, hid, f"not evaluated: only {len(prior)} prior transactions")

            window = [
                x
                for x in ev.history.transactions
                if t - timedelta(minutes=th["velocity_window_minutes"]) <= x.timestamp <= t
            ]
            if not any(x.transaction_id == txn.transaction_id for x in window):
                window.append(txn)  # type: ignore[arg-type]
            add(
                "velocity",
                len(window) > th["velocity_count"],
                hid,
                f"{len(window)} transactions in {int(th['velocity_window_minutes'])} minutes",
            )
            small = [x for x in window if x.amount <= th["card_testing_max_amount"] and x.channel == "card_not_present"]
            add(
                "card_testing",
                len(small) >= th["card_testing_count"],
                hid,
                f"{len(small)} card-not-present charges of {th['card_testing_max_amount']:.2f} or less in the window",
            )

            if txn.channel == "card_present" and txn.latitude is not None and txn.longitude is not None:
                located = [x for x in prior if x.latitude is not None and x.longitude is not None and x.timestamp < t]
                if located:
                    last = max(located, key=lambda x: x.timestamp)
                    km = haversine_km(last.latitude, last.longitude, txn.latitude, txn.longitude)  # type: ignore[arg-type]
                    hours = (t - last.timestamp).total_seconds() / 3600
                    speed = km / hours if hours > 0 else (math.inf if km > 50 else 0.0)
                    add(
                        "impossible_travel",
                        speed > th["impossible_travel_kmh"],
                        hid,
                        f"{km:.0f} km in {hours * 60:.0f} minutes ({speed:.0f} km/h)",
                    )
                else:
                    add("impossible_travel", False, hid, "no earlier located transaction")
            else:
                add("impossible_travel", False, hid, "not evaluated: card not present")

            cutoff = t - timedelta(days=7)
            at_merchant = [x for x in prior if x.merchant_id == txn.merchant_id and x.timestamp < cutoff]
            add(
                "known_merchant",
                len(at_merchant) >= th["known_merchant_min_txns"],
                hid,
                f"{len(at_merchant)} earlier transactions at this merchant",
                kind="mitigation",
            )

        if ev.device:
            did = ev.device.evidence_id
            age_h = (t - ev.device.first_seen).total_seconds() / 3600
            add("new_device", age_h <= th["new_device_hours"], did, f"device first seen {age_h:.1f} hours before")
            flags = [
                n
                for n, v in (
                    ("emulator", ev.device.emulator),
                    ("rooted", ev.device.rooted),
                    ("proxy_or_vpn", ev.device.proxy_or_vpn),
                )
                if v
            ]
            add("device_risk", bool(flags), did, ", ".join(flags) or "no risk flags")
            trusted = age_h >= th["trusted_device_days"] * 24 and ev.device.linked_to_customer
            add(
                "trusted_device",
                trusted,
                did,
                f"device age {age_h / 24:.0f} days, linked to customer: {ev.device.linked_to_customer}",
                kind="mitigation",
            )

        if ev.customer:
            cid = ev.customer.evidence_id
            c = ev.customer
            notice = (
                c.travel_notice_country == txn.country
                and c.travel_notice_start is not None
                and c.travel_notice_end is not None
                and c.travel_notice_start <= t <= c.travel_notice_end
            )
            add(
                "geo_mismatch",
                txn.country != c.home_country and not notice,
                cid,
                f"transaction country {txn.country}, home {c.home_country}",
            )
            add(
                "travel_notice",
                notice and txn.country != c.home_country,
                cid,
                f"travel notice for {c.travel_notice_country or 'none'}",
                kind="mitigation",
            )
            changes = [
                (n, v)
                for n, v in (
                    ("email", c.email_changed_at),
                    ("phone", c.phone_changed_at),
                    ("password", c.password_changed_at),
                )
                if v is not None and timedelta(0) <= t - v <= timedelta(hours=th["account_change_hours"])
            ]
            add(
                "account_change",
                bool(changes),
                cid,
                ", ".join(f"{n} changed {(t - v).total_seconds() / 3600:.0f}h before" for n, v in changes) or "none",
            )
            add(
                "dispute_history",
                c.disputes_delivered_90d >= th["dispute_count_90d"],
                cid,
                f"{c.disputes_delivered_90d} disputes on delivered goods in 90 days",
            )

        if ev.merchant:
            m = ev.merchant
            age_days = (t - m.registered_date).days
            risky = m.chargeback_rate > th["merchant_chargeback_rate"] or age_days < th["merchant_new_days"]
            add(
                "merchant_risk",
                risky,
                m.evidence_id,
                f"chargeback rate {m.chargeback_rate:.1%}, registered {age_days} days before",
            )
        return out


def as_of(alert: Alert) -> datetime:
    return alert.created_at
