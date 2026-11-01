"""Idempotent actions: block card (mock), create case, close alert.

The idempotency key is `alert_id + decision + policy version`. Replaying a run
returns the existing case and never blocks a card twice. Every action writes an
audit log row.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..db import session
from ..hooks.audit import write_audit
from ..schemas import Decision, InvestigationReport

ALERT_STATUS: dict[str, str] = {"allow": "closed_allowed", "escalate": "escalated", "block": "closed_blocked"}


def idempotency_key(alert_id: str, decision: str, version: str) -> str:
    return hashlib.sha256(f"{alert_id}|{decision}|{version}".encode()).hexdigest()[:32]


@dataclass
class ActionResult:
    case_id: str
    idempotency_key: str
    replayed: bool
    actions: list[str] = field(default_factory=list)


class ActionService:
    def __init__(self, db_path: Path, version: str = "v1") -> None:
        self.db_path = db_path
        self.version = version

    def execute(self, report: InvestigationReport, card_token: str, approver: str | None) -> ActionResult:
        decision: Decision = report.recommendation
        if decision == "block" and not approver:
            raise PermissionError("a block needs an analyst approval")
        key = idempotency_key(report.alert_id, decision, self.version)
        now = datetime.now(UTC).isoformat()
        actions: list[str] = []
        with session(self.db_path) as conn:
            existing = conn.execute("SELECT id FROM cases WHERE idempotency_key=?", (key,)).fetchone()
            if existing:
                write_audit(self.db_path, report.alert_id, "action", "replayed", detail=f"case {existing['id']}")
                return ActionResult(existing["id"], key, replayed=True)

            case_id = f"CASE-{key[:10].upper()}"
            conn.execute(
                "INSERT INTO cases (id, alert_id, idempotency_key, decision, risk_score, approver, report_json, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    report.alert_id,
                    key,
                    decision,
                    report.risk_score,
                    approver,
                    report.model_dump_json(),
                    now,
                    now,
                ),
            )
            actions.append(f"create_case:{case_id}")

            if decision == "block":
                inserted = conn.execute(
                    "INSERT OR IGNORE INTO card_actions (idempotency_key, card_token, action, alert_id, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (key, card_token, "block", report.alert_id, now),
                ).rowcount
                if inserted:
                    conn.execute("UPDATE cards SET status='blocked' WHERE token=?", (card_token,))
                    actions.append(f"block_card:{card_token}")

            conn.execute("UPDATE alerts SET status=? WHERE id=?", (ALERT_STATUS[decision], report.alert_id))
            actions.append(f"close_alert:{ALERT_STATUS[decision]}")
        for action in actions:
            write_audit(self.db_path, report.alert_id, "action", "ok", tool=action.split(":")[0], detail=action)
        return ActionResult(case_id, key, replayed=False, actions=actions)

    def case_for(self, alert_id: str) -> dict | None:
        with session(self.db_path) as conn:
            row = conn.execute("SELECT * FROM cases WHERE alert_id=? ORDER BY created_at DESC", (alert_id,)).fetchone()
        return dict(row) if row else None
