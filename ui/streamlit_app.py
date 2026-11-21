"""Minimal chat UI for the Ujjwal Rail Assistant.

Run:  streamlit run ui/streamlit_app.py      (API_URL defaults to http://localhost:8000)
The UI only talks to the public API; it sends X-Passenger-Id on every call and shows Confirm / Cancel
buttons for proposed changes. Confirming is a separate POST /actions/{id}/confirm call.
"""
import os
import uuid

import httpx
import streamlit as st

API_URL = os.getenv("API_URL", "http://localhost:8000").rstrip("/")
BADGES = {
    "answer": "🟢 answer", "clarification": "🟡 question", "confirm_needed": "🟠 needs your confirmation",
    "action_done": "✅ done", "handed_to_human": "🧑‍💼 handed to staff", "not_found": "⚪ not found",
    "refusal": "🔴 refused",
}

st.set_page_config(page_title="Ujjwal Rail Assistant", page_icon="🚆", layout="centered")


# ---------- state ----------

def reset_conversation() -> None:
    st.session_state.session_id = uuid.uuid4().hex[:12]
    st.session_state.history = []      # [{"role": "user"|"assistant", "text": str, "resp": dict|None}]
    st.session_state.resolved = {}     # action_id -> "confirmed" | "cancelled"


if "session_id" not in st.session_state:
    reset_conversation()


# ---------- API ----------

def call(method: str, path: str, passenger_id: str, json: dict | None = None) -> dict:
    try:
        r = httpx.request(method, f"{API_URL}{path}", headers={"X-Passenger-Id": passenger_id},
                          json=json, timeout=60)
    except httpx.HTTPError as e:
        return {"answer_type": "not_found", "answer": f"Can't reach the API at {API_URL}: {e}", "error": True}
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code == 401:
        return {"answer_type": "refusal", "answer": "Unknown passenger ID. Check the ID in the sidebar.", "error": True}
    if "answer" not in body:
        return {"answer_type": "not_found", "answer": f"API error {r.status_code}: {body.get('detail', body)}",
                "error": True}
    return body


# ---------- sidebar ----------

with st.sidebar:
    st.header("🚆 Ujjwal Rail")
    pid = st.text_input("Passenger ID (sent as X-Passenger-Id)", value="P001").strip()
    if st.session_state.get("pid") != pid:          # never show one passenger's chat to another
        st.session_state.pid = pid
        reset_conversation()
    as_of = st.text_input("Simulated time (optional, ISO)", placeholder="2026-12-03T10:00:00+05:30",
                          help="Leave empty to use the current time. Useful for testing cut-offs.").strip()
    if st.button("New conversation"):
        reset_conversation()
        st.rerun()
    st.caption(f"API: {API_URL}  \nSession: {st.session_state.session_id}")


def with_as_of(payload: dict) -> dict:
    return {**payload, "as_of": as_of} if as_of else payload


# ---------- rendering ----------

def render_assistant(resp: dict, idx: int) -> None:
    st.caption(BADGES.get(resp.get("answer_type"), resp.get("answer_type", "")))
    st.markdown(resp.get("answer", "").replace("\n", "  \n"))

    if resp.get("sources"):
        st.caption("Sources: " + ", ".join(f"{s['doc_id']} § {s['section']}" if s.get("section") else s["doc_id"]
                                           for s in resp["sources"]))
    h = resp.get("handed_to_human")
    if h:
        st.info(f"Reference **{h['handoff_id']}**, sent to the **{h['desk']}** desk.")

    pa = resp.get("pending_action")
    if resp.get("answer_type") == "confirm_needed" and pa:
        state = st.session_state.resolved.get(pa["action_id"])
        if state:
            st.caption(f"Action {pa['action_id']}: {state}.")
        else:
            st.caption(f"Action {pa['action_id']} · expires {pa['expires_at']}")
            c1, c2 = st.columns(2)
            if c1.button("✅ Confirm", key=f"confirm-{pa['action_id']}-{idx}", type="primary"):
                out = call("POST", f"/actions/{pa['action_id']}/confirm", pid, with_as_of({}))
                st.session_state.resolved[pa["action_id"]] = "confirmed" if out.get("answer_type") == "action_done" \
                    else "not completed"
                st.session_state.history.append({"role": "assistant", "text": out["answer"], "resp": out})
                st.rerun()
            if c2.button("✖ Cancel", key=f"cancel-{pa['action_id']}-{idx}"):
                out = call("POST", f"/actions/{pa['action_id']}/cancel", pid)
                st.session_state.resolved[pa["action_id"]] = "cancelled"
                st.session_state.history.append({"role": "assistant", "text": out["answer"], "resp": out})
                st.rerun()

    if resp.get("tools") or resp.get("trace_id"):
        with st.expander("Details (tools and trace)"):
            if resp.get("trace_id"):
                st.markdown(f"Trace: [`{resp['trace_id']}`]({API_URL}/trace/{resp['trace_id']})")
            for t in resp.get("tools", []):
                st.markdown(f"**{t['tool']}**")
                st.json(t.get("output"), expanded=False)


st.title("Ujjwal Rail Assistant")
st.caption("Find trains, check seats and fares, understand the rules, get refund quotes, and book, cancel or "
           "change tickets. English, हिंदी or Hinglish.")

for i, m in enumerate(st.session_state.history):
    with st.chat_message(m["role"]):
        if m["role"] == "assistant" and m.get("resp"):
            render_assistant(m["resp"], i)
        else:
            st.markdown(m["text"])

if prompt := st.chat_input("Ask about trains, fares, refunds or your booking…"):
    st.session_state.history.append({"role": "user", "text": prompt, "resp": None})
    resp = call("POST", "/chat", pid, with_as_of({"session_id": st.session_state.session_id, "message": prompt}))
    st.session_state.history.append({"role": "assistant", "text": resp.get("answer", ""), "resp": resp})
    st.rerun()
