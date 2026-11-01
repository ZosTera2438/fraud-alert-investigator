"""Alert input contract."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Channel = Literal["card_present", "card_not_present"]


class AlertTransaction(BaseModel):
    transaction_id: str
    amount: float = Field(gt=0)
    currency: str = "AUD"
    merchant_id: str
    channel: Channel
    device_id: str | None = None
    # Country the transaction came from: IP country for card-not-present,
    # terminal country for card-present.
    country: str = Field(min_length=2, max_length=2)
    latitude: float | None = None
    longitude: float | None = None
    timestamp: datetime


class Alert(BaseModel):
    alert_id: str = Field(pattern=r"^ALT-[0-9A-Z-]+$")
    created_at: datetime
    customer_id: str
    card_token: str
    transaction: AlertTransaction
    trigger_rule: str
