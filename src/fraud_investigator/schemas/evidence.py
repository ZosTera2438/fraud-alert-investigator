"""Typed results returned by the read-only tools.

Every result carries an evidence_id, source and retrieved_at so the report can
cite it and the scoring engine can find the raw data it was computed from.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from .alert import Channel

Source = Literal["transaction_history", "merchant_profile", "device_metadata", "customer_profile", "fraud_playbook"]


class EvidenceMeta(BaseModel):
    evidence_id: str
    source: Source
    retrieved_at: datetime


class Transaction(BaseModel):
    transaction_id: str
    merchant_id: str
    amount: float
    currency: str
    channel: Channel
    device_id: str | None
    country: str
    latitude: float | None
    longitude: float | None
    timestamp: datetime


class TransactionHistory(EvidenceMeta):
    source: Source = "transaction_history"
    customer_id: str
    days: int
    as_of: datetime
    transactions: list[Transaction]


class MerchantProfile(EvidenceMeta):
    source: Source = "merchant_profile"
    merchant_id: str
    name: str
    description: str
    description_truncated: bool = False
    category_code: str
    country: str
    registered_date: datetime
    chargeback_rate: float = Field(ge=0, le=1)


class DeviceMetadata(EvidenceMeta):
    source: Source = "device_metadata"
    device_id: str
    first_seen: datetime
    os: str
    emulator: bool
    rooted: bool
    proxy_or_vpn: bool
    linked_customer_count: int
    linked_to_customer: bool


class CustomerProfile(EvidenceMeta):
    source: Source = "customer_profile"
    customer_id: str
    masked_email: str
    home_country: str
    home_latitude: float
    home_longitude: float
    account_opened: datetime
    email_changed_at: datetime | None
    phone_changed_at: datetime | None
    password_changed_at: datetime | None
    travel_notice_country: str | None
    travel_notice_start: datetime | None
    travel_notice_end: datetime | None
    disputes_90d: int
    disputes_delivered_90d: int


class PlaybookPassage(BaseModel):
    passage_id: str
    title: str
    text: str
    score: float


class PlaybookResult(EvidenceMeta):
    source: Source = "fraud_playbook"
    query: str
    passages: list[PlaybookPassage]


class ToolError(BaseModel):
    """Error the model can act on. `retryable` tells it whether calling again can help."""

    error: Literal["not_found", "timeout", "invalid_input", "malformed_data", "unavailable", "budget"]
    message: str
    retryable: bool = False
    source: Source | None = None
