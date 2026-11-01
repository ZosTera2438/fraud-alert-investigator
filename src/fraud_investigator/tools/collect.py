"""Collect every data source for an alert without an agent (training, tests, comparisons)."""

from __future__ import annotations

from ..schemas import Alert
from .backend import DataBackend, ToolFailure
from .evidence import EvidenceStore, render_error, render_result


def collect_all(backend: DataBackend, alert: Alert) -> EvidenceStore:
    store = EvidenceStore()
    t = alert.transaction
    calls = [
        ("get_transaction_history", lambda: backend.transaction_history(alert.customer_id, 90, alert.created_at)),
        ("get_customer_profile", lambda: backend.customer_profile(alert.customer_id)),
        ("get_merchant_profile", lambda: backend.merchant_profile(t.merchant_id)),
    ]
    if t.device_id:
        device_id = t.device_id
        calls.append(("get_device_metadata", lambda: backend.device_metadata(device_id, alert.customer_id)))
    for name, fn in calls:
        try:
            store.add_text(name, {}, render_result(fn()))
        except ToolFailure as exc:
            store.add_text(name, {}, render_error(exc.error))
    return store
