"""Analyst console: pick an alert, run the investigation, review evidence, approve or reject a block.

    uv run --extra ui streamlit run ui/streamlit_app.py
Needs the API running (FRAUD_API_URL, default http://localhost:8000).
"""

from __future__ import annotations

import os

import httpx
import streamlit as st

API = os.environ.get("FRAUD_API_URL", "http://localhost:8000")
client = httpx.Client(base_url=API, timeout=180)

st.set_page_config(page_title="Fraud alert console", layout="wide")
st.title("Fraud alert console")

try:
    health = client.get("/health").json()
    st.caption(f"Model: {health['model_provider']} ({health['model_id']}) · tools over {health['tool_transport']}")
except httpx.HTTPError:
    st.error(f"API not reachable at {API}. Start it with `uv run uvicorn fraud_investigator.api.app:app`.")
    st.stop()

pending = client.get("/approvals").json()
with st.sidebar:
    st.header("Waiting for approval")
    if not pending:
        st.write("Nothing waiting.")
    for p in pending:
        req = p.get("request") or {}
        if st.button(
            f"{p['alert_id']} · {req.get('decision', '?')} · score {p.get('risk_score')}", key=f"p-{p['alert_id']}"
        ):
            st.session_state["alert_id"] = p["alert_id"]

alerts = client.get("/alerts").json()
# Options are alert IDs (stable); the label shows the status, which changes after an approval. Keying the
# widget on labels would reset the selection to the first alert whenever a status changed.
labels = {a["id"]: f"{a['id']} · {a['trigger_rule']} · {a['amount']:.2f} AUD · {a['status']}" for a in alerts}
ids = list(labels)
if st.session_state.get("alert_id") in labels:
    st.session_state["alert_select"] = st.session_state.pop("alert_id")
alert_id = st.selectbox("Alert", ids, format_func=labels.__getitem__, key="alert_select")

col_run, col_load = st.columns(2)
run = None
if col_run.button("Investigate", type="primary"):
    with st.spinner("Investigating..."):
        run = client.post(f"/alerts/{alert_id}/investigate").json()
elif (resp := client.get(f"/runs/{alert_id}")).status_code == 200:
    run = resp.json()
if col_load.button("Refresh"):
    st.rerun()

if not run:
    st.info("No investigation yet for this alert.")
    st.stop()

risk = run.get("risk") or {}
m1, m2, m3, m4 = st.columns(4)
m1.metric("Risk score", risk.get("score", "–"))
m2.metric("Band", risk.get("band", "–"))
m3.metric("Status", run["status"])
cost = run["metrics"].get("cost_aud")
m4.metric("Cost (AUD)", "unknown" if cost is None else f"{cost:.4f}")

if run["status"] == "awaiting_approval":
    req = run["pending_approval"] or {}
    st.warning(f"The agent recommends **{req.get('decision')}**. An analyst must approve before it takes effect.")
    st.write(req.get("rationale"))
    approver = st.text_input("Your name", value="analyst")
    note = st.text_input("Note")
    a, r = st.columns(2)
    if a.button("Approve"):
        client.post(f"/runs/{alert_id}/approval", json={"approved": True, "approver": approver, "note": note})
        st.rerun()
    if r.button("Reject (escalate instead)"):
        client.post(f"/runs/{alert_id}/approval", json={"approved": False, "approver": approver, "note": note})
        st.rerun()

if report := run.get("report"):
    st.subheader(f"Recommendation: {report['recommendation']} ({report['confidence']} confidence)")
    st.write(report["analyst_summary"])
    st.caption(f"Decision source: {report['decision_source']} · case {run.get('case_id')}")
    for n in report.get("notes", []):
        st.caption(f"Note: {n}")
    left, right = st.columns(2)
    left.markdown("**Key findings**")
    for f in report["key_findings"]:
        left.write(f"- {f['text']}" + (f" `{', '.join(f['evidence_ids'])}`" if f["evidence_ids"] else ""))
    right.markdown("**Mitigating factors**")
    for f in report["mitigating_factors"]:
        right.write(f"- {f['text']}" + (f" `{', '.join(f['evidence_ids'])}`" if f["evidence_ids"] else ""))

st.subheader("Score breakdown")
st.dataframe([s for s in risk.get("signals", [])], use_container_width=True)
st.subheader("Tool calls")
st.dataframe(run["tool_calls"], use_container_width=True)
with st.expander("Audit log"):
    st.dataframe(client.get(f"/audit/{alert_id}").json(), use_container_width=True)
with st.expander("Metrics"):
    st.json(run["metrics"])
