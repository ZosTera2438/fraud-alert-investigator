"""Evidence store and the wire format tools use to return data to the model.

Tool results reach the model as `<tool_data>` blocks. Any `<` inside the JSON is
escaped, so text in a record (for example an injected merchant description)
cannot close the block and pose as instructions. The EvidenceHook parses the
same blocks back into typed records, which is what the scoring engine reads.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from ..schemas import (
    CustomerProfile,
    DeviceMetadata,
    EvidenceMeta,
    MerchantProfile,
    PlaybookResult,
    Source,
    ToolError,
    TransactionHistory,
)

SOURCE_MODELS: dict[str, type[EvidenceMeta]] = {
    "transaction_history": TransactionHistory,
    "merchant_profile": MerchantProfile,
    "device_metadata": DeviceMetadata,
    "customer_profile": CustomerProfile,
    "fraud_playbook": PlaybookResult,
}
TOOL_SOURCES: dict[str, Source] = {
    "get_transaction_history": "transaction_history",
    "get_merchant_profile": "merchant_profile",
    "get_device_metadata": "device_metadata",
    "get_customer_profile": "customer_profile",
    "search_fraud_playbook": "fraud_playbook",
}
_DATA_BLOCK = re.compile(
    r'<tool_data source="(?P<source>[a-z_]+)" evidence_id="(?P<eid>[A-Za-z0-9_-]+)">(?P<body>.*?)</tool_data>', re.S
)
_ERROR_BLOCK = re.compile(r"<tool_error>(?P<body>.*?)</tool_error>", re.S)


def _safe_json(data: Any) -> str:
    return json.dumps(data, default=str, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e")


def render_result(result: EvidenceMeta) -> str:
    body = _safe_json(result.model_dump(mode="json"))
    return f'<tool_data source="{result.source}" evidence_id="{result.evidence_id}">{body}</tool_data>'


def render_error(error: ToolError) -> str:
    return f"<tool_error>{_safe_json(error.model_dump(mode='json'))}</tool_error>"


class EvidenceRecord(BaseModel):
    evidence_id: str
    source: Source
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: str = "ok"  # ok | error
    payload: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def typed(self) -> EvidenceMeta | None:
        if self.status != "ok" or self.payload is None:
            return None
        return SOURCE_MODELS[self.source].model_validate(self.payload)


class EvidenceStore(BaseModel):
    records: dict[str, EvidenceRecord] = Field(default_factory=dict)

    def add_text(self, tool: str, arguments: dict[str, Any], text: str) -> list[EvidenceRecord]:
        """Parse a tool's text output and store every data or error block it contains."""
        added: list[EvidenceRecord] = []
        for m in _DATA_BLOCK.finditer(text):
            source = m.group("source")
            if source not in SOURCE_MODELS:
                continue
            try:
                payload = SOURCE_MODELS[source].model_validate_json(m.group("body"))
            except ValidationError:
                continue
            record = EvidenceRecord(
                evidence_id=payload.evidence_id,
                source=payload.source,
                tool=tool,
                arguments=arguments,
                payload=payload.model_dump(mode="json"),
            )
            self.records[record.evidence_id] = record
            added.append(record)
        for m in _ERROR_BLOCK.finditer(text):
            try:
                error = ToolError.model_validate_json(m.group("body"))
            except ValidationError:
                continue
            source = error.source or TOOL_SOURCES.get(tool)
            if source is None:
                continue
            eid = f"ERR-{source}-{len(self.records) + 1}"
            record = EvidenceRecord(
                evidence_id=eid,
                source=source,
                tool=tool,
                arguments=arguments,
                status="error",
                error=error.model_dump(mode="json"),
            )
            self.records[eid] = record
            added.append(record)
        return added

    def ok(self, source: Source) -> list[EvidenceRecord]:
        return [r for r in self.records.values() if r.source == source and r.status == "ok"]

    def failed(self, source: Source) -> list[EvidenceRecord]:
        return [r for r in self.records.values() if r.source == source and r.status == "error"]

    def known_ids(self) -> set[str]:
        return {eid for eid, r in self.records.items() if r.status == "ok"}
