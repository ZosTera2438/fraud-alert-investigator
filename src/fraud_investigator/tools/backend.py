"""Data access behind the read-only tools.

Plain Python, no agent framework: the same backend serves the Strands tools,
the Model Context Protocol server, the framework-free baseline and the scoring
engine tests. Every call has a timeout and retries transient failures with
exponential backoff; the final failure is a typed ToolError, never an exception
the model has to interpret.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar

from pydantic import ValidationError
from tenacity import RetryError, retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..db import session
from ..schemas import (
    CustomerProfile,
    DeviceMetadata,
    MerchantProfile,
    PlaybookResult,
    Source,
    ToolError,
    Transaction,
    TransactionHistory,
)
from .playbook import PlaybookIndex

T = TypeVar("T")
_POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="tool")

SOURCE_CODES: dict[Source, str] = {
    "transaction_history": "TXH",
    "merchant_profile": "MER",
    "device_metadata": "DEV",
    "customer_profile": "CUS",
    "fraud_playbook": "PLB",
}
_ID_PATTERN = {
    "customer_id": re.compile(r"^CUS-\d{4}$"),
    "merchant_id": re.compile(r"^MER-\d{3}$"),
    "device_id": re.compile(r"^DEV-\d{4}$"),
}


class ToolFailure(Exception):
    def __init__(self, error: ToolError) -> None:
        super().__init__(error.message)
        self.error = error


class TransientToolError(Exception):
    """Raised for failures worth retrying (timeouts, unavailable services)."""


def evidence_id(source: Source, **args: object) -> str:
    """Deterministic: the same query in the same run always gets the same ID."""
    digest = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:8]
    return f"EV-{SOURCE_CODES[source]}-{digest}"


def parse_time(value: str | datetime | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


class DataBackend:
    def __init__(
        self,
        db_path: Path | str,
        *,
        timeout_seconds: float = 5.0,
        retry_attempts: int = 3,
        retry_wait_seconds: float = 0.05,
        max_text_chars: int = 2000,
    ) -> None:
        self.db_path = Path(db_path)
        self.timeout_seconds = timeout_seconds
        self.retry_attempts = retry_attempts
        self.retry_wait_seconds = retry_wait_seconds
        self.max_text_chars = max_text_chars
        self.playbook = PlaybookIndex()

    # ---------- resilience wrapper ----------
    def _call(self, source: Source, fn: Callable[[], T]) -> T:
        @retry(
            retry=retry_if_exception_type(TransientToolError),
            stop=stop_after_attempt(self.retry_attempts),
            wait=wait_exponential(multiplier=self.retry_wait_seconds, max=2),
            reraise=False,
        )
        def attempt() -> T:
            future = _POOL.submit(fn)
            try:
                return future.result(timeout=self.timeout_seconds)
            except FutureTimeout as exc:
                raise TransientToolError(f"{source} timed out after {self.timeout_seconds}s") from exc

        try:
            return attempt()
        except RetryError as exc:
            cause = exc.last_attempt.exception()
            raise ToolFailure(
                ToolError(
                    error="timeout",
                    source=source,
                    retryable=False,
                    message=f"{cause}; failed after {self.retry_attempts} attempts. Do not retry; "
                    "record the source as unavailable.",
                )
            ) from exc
        except ValidationError as exc:
            raise ToolFailure(
                ToolError(
                    error="malformed_data",
                    source=source,
                    retryable=False,
                    message=f"{source} returned data that failed validation ({exc.error_count()} errors). "
                    "Do not retry; treat this source as unavailable.",
                )
            ) from exc

    @staticmethod
    def _check_id(field: str, value: str, source: Source) -> None:
        if not _ID_PATTERN[field].match(value or ""):
            raise ToolFailure(
                ToolError(
                    error="invalid_input",
                    source=source,
                    retryable=True,
                    message=f"{field} '{value}' is not valid; expected a value like "
                    f"{ {'customer_id': 'CUS-0042', 'merchant_id': 'MER-071', 'device_id': 'DEV-0512'}[field] }.",
                )
            )

    @staticmethod
    def _not_found(source: Source, what: str) -> ToolFailure:
        return ToolFailure(
            ToolError(error="not_found", source=source, retryable=False, message=f"{what} not found. Do not retry.")
        )

    # ---------- tools ----------
    def transaction_history(
        self, customer_id: str, days: int = 90, as_of: str | datetime | None = None
    ) -> TransactionHistory:
        self._check_id("customer_id", customer_id, "transaction_history")
        days = max(1, min(int(days), 180))
        end = parse_time(as_of) or datetime.now(UTC)
        start = end - timedelta(days=days)

        def query() -> TransactionHistory:
            with session(self.db_path) as conn:
                if not conn.execute("SELECT 1 FROM customers WHERE id=?", (customer_id,)).fetchone():
                    raise self._not_found("transaction_history", f"customer {customer_id}")
                rows = conn.execute(
                    "SELECT * FROM transactions WHERE customer_id=? AND timestamp>=? AND timestamp<=? "
                    "ORDER BY timestamp",
                    (customer_id, _iso(start), _iso(end)),
                ).fetchall()
            return TransactionHistory(
                evidence_id=evidence_id("transaction_history", customer_id=customer_id, days=days, as_of=_iso(end)),
                retrieved_at=datetime.now(UTC),
                customer_id=customer_id,
                days=days,
                as_of=end,
                transactions=[
                    Transaction(
                        transaction_id=r["id"],
                        merchant_id=r["merchant_id"],
                        amount=r["amount"],
                        currency=r["currency"],
                        channel=r["channel"],
                        device_id=r["device_id"],
                        country=r["country"],
                        latitude=r["lat"],
                        longitude=r["lon"],
                        timestamp=parse_time(r["timestamp"]),  # type: ignore[arg-type]
                    )
                    for r in rows
                ],
            )

        return self._call("transaction_history", query)

    def merchant_profile(self, merchant_id: str) -> MerchantProfile:
        self._check_id("merchant_id", merchant_id, "merchant_profile")

        def query() -> MerchantProfile:
            with session(self.db_path) as conn:
                r = conn.execute("SELECT * FROM merchants WHERE id=?", (merchant_id,)).fetchone()
            if r is None:
                raise self._not_found("merchant_profile", f"merchant {merchant_id}")
            description = r["description"]
            truncated = len(description) > self.max_text_chars
            return MerchantProfile(
                evidence_id=evidence_id("merchant_profile", merchant_id=merchant_id),
                retrieved_at=datetime.now(UTC),
                merchant_id=merchant_id,
                name=r["name"][:200],
                description=description[: self.max_text_chars],
                description_truncated=truncated,
                category_code=r["category_code"],
                country=r["country"],
                registered_date=parse_time(r["registered_date"]),  # type: ignore[arg-type]
                chargeback_rate=r["chargeback_rate"],
            )

        return self._call("merchant_profile", query)

    def device_metadata(self, device_id: str, customer_id: str | None = None) -> DeviceMetadata:
        self._check_id("device_id", device_id, "device_metadata")

        def query() -> DeviceMetadata:
            with session(self.db_path) as conn:
                r = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            if r is None:
                raise self._not_found("device_metadata", f"device {device_id}")
            if r["service_fault"] == "timeout":
                raise TransientToolError("device service timed out")
            linked = json.loads(r["linked_customers"])
            return DeviceMetadata(
                evidence_id=evidence_id("device_metadata", device_id=device_id),
                retrieved_at=datetime.now(UTC),
                device_id=device_id,
                first_seen=parse_time(r["first_seen"]),  # type: ignore[arg-type]
                os=r["os"][:200],
                emulator=bool(r["emulator"]),
                rooted=bool(r["rooted"]),
                proxy_or_vpn=bool(r["proxy_or_vpn"]),
                linked_customer_count=len(linked),
                linked_to_customer=customer_id in linked if customer_id else False,
            )

        return self._call("device_metadata", query)

    def customer_profile(self, customer_id: str) -> CustomerProfile:
        self._check_id("customer_id", customer_id, "customer_profile")

        def query() -> CustomerProfile:
            with session(self.db_path) as conn:
                r = conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
            if r is None:
                raise self._not_found("customer_profile", f"customer {customer_id}")
            return CustomerProfile(
                evidence_id=evidence_id("customer_profile", customer_id=customer_id),
                retrieved_at=datetime.now(UTC),
                customer_id=customer_id,
                masked_email=mask_email(r["email"]),
                home_country=r["home_country"],
                home_latitude=r["home_lat"],
                home_longitude=r["home_lon"],
                account_opened=parse_time(r["account_opened"]),  # type: ignore[arg-type]
                email_changed_at=parse_time(r["email_changed_at"]),
                phone_changed_at=parse_time(r["phone_changed_at"]),
                password_changed_at=parse_time(r["password_changed_at"]),
                travel_notice_country=r["travel_notice_country"],
                travel_notice_start=parse_time(r["travel_notice_start"]),
                travel_notice_end=parse_time(r["travel_notice_end"]),
                disputes_90d=r["disputes_90d"],
                disputes_delivered_90d=r["disputes_delivered_90d"],
            )

        return self._call("customer_profile", query)

    def fraud_playbook(self, query: str, top_k: int = 3) -> PlaybookResult:
        query = (query or "").strip()[:300]
        if not query:
            raise ToolFailure(
                ToolError(
                    error="invalid_input", source="fraud_playbook", retryable=True, message="query must not be empty."
                )
            )
        return self._call(
            "fraud_playbook",
            lambda: PlaybookResult(
                evidence_id=evidence_id("fraud_playbook", query=query.lower(), top_k=top_k),
                retrieved_at=datetime.now(UTC),
                query=query,
                passages=self.playbook.search(query, top_k),
            ),
        )

    # ---------- reads used outside the agent ----------
    def load_alert(self, alert_id: str) -> dict:
        with session(self.db_path) as conn:
            a = conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
            if a is None:
                raise KeyError(alert_id)
            t = conn.execute("SELECT * FROM transactions WHERE id=?", (a["transaction_id"],)).fetchone()
        return {
            "alert_id": a["id"],
            "created_at": a["created_at"],
            "customer_id": a["customer_id"],
            "card_token": a["card_token"],
            "trigger_rule": a["trigger_rule"],
            "transaction": {
                "transaction_id": t["id"],
                "amount": t["amount"],
                "currency": t["currency"],
                "merchant_id": t["merchant_id"],
                "channel": t["channel"],
                "device_id": t["device_id"],
                "country": t["country"],
                "latitude": t["lat"],
                "longitude": t["lon"],
                "timestamp": t["timestamp"],
            },
        }

    def list_alerts(self, status: str | None = None, limit: int = 200) -> list[dict]:
        with session(self.db_path) as conn:
            sql = "SELECT a.*, t.amount, t.channel FROM alerts a JOIN transactions t ON t.id=a.transaction_id"
            params: tuple = ()
            if status:
                sql += " WHERE a.status=?"
                params = (status,)
            rows = conn.execute(sql + " ORDER BY a.id LIMIT ?", (*params, limit)).fetchall()
        return [dict(r) for r in rows]


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
