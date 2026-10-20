import os
import uuid
import requests
import streamlit as st

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")

st.set_page_config(page_title="SentinelSQL Console", page_icon="🛡️", layout="wide")
st.title("🛡️ SentinelSQL — Autonomous Text-to-SQL & HITL Console")

if "thread_id" not in st.session_state:
    st.session_state.thread_id = f"session_{uuid.uuid4().hex[:8]}"
if "messages" not in st.session_state:
    st.session_state.messages = []
if "pending_interrupt" not in st.session_state:
    st.session_state.pending_interrupt = None

# Sidebar: Thread Controls & Live Schema
with st.sidebar:
    st.header("⚙️ Session & Schema")
    st.code(f"Thread ID: {st.session_state.thread_id}")
    if st.button("🔄 New Conversation Thread"):
        st.session_state.thread_id = f"session_{uuid.uuid4().hex[:8]}"
        st.session_state.messages = []
        st.session_state.pending_interrupt = None
        st.rerun()

    st.divider()
    st.subheader("📊 Live PostgreSQL Schema")
    try:
        schema_res = requests.get(f"{API_BASE_URL}/api/v1/schema", timeout=5)
        if schema_res.status_code == 200:
            st.markdown(schema_res.json().get("schema", ""))
    except Exception:
        st.warning("Backend API connecting...")

# Render Chat History
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("sql"):
            st.code(msg["sql"], language="sql")

# Render Human-in-the-Loop Interception Card if Paused
if st.session_state.pending_interrupt:
    details = st.session_state.pending_interrupt
    st.warning(f"⚠️ **{details.get('alert')}**")
    st.caption(f"**Agent Reasoning:** {details.get('reasoning')}")

    edited_sql = st.text_area(
        "Review or Edit Proposed SQL Before Execution:",
        value=details.get("proposed_sql", ""),
        height=100,
    )

    col1, col2, col3 = st.columns(3)
    action_taken = None
    payload = {"thread_id": st.session_state.thread_id}

    if col1.button("✅ Approve & Execute", use_container_width=True):
        action_taken = "approve"
    if col2.button("✏️ Execute Edited SQL", use_container_width=True):
        action_taken = "edit"
        payload["modified_sql"] = edited_sql
    if col3.button("🛑 Reject Query", use_container_width=True):
        action_taken = "reject"

    if action_taken:
        payload["action"] = action_taken
        with st.spinner("Resuming LangGraph checkpoint..."):
            res = requests.post(f"{API_BASE_URL}/api/v1/approve", json=payload).json()
            st.session_state.pending_interrupt = None
            st.session_state.messages.append({
                "role": "assistant",
                "content": res.get("answer", "Completed."),
                "sql": res.get("sql_executed"),
            })
            st.rerun()

# Chat Input
if prompt := st.chat_input(
    "Ask a question (e.g., 'Total Electronics spend in Bengaluru' or 'Delete all cancelled orders')...",
    disabled=st.session_state.pending_interrupt is not None,
):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.spinner("SentinelSQL is analyzing schema & generating SQL..."):
        res = requests.post(
            f"{API_BASE_URL}/api/v1/query",
            json={"thread_id": st.session_state.thread_id, "question": prompt},
        ).json()

        if res.get("status") == "awaiting_human_approval":
            st.session_state.pending_interrupt = res.get("interrupt_details")
            st.rerun()
        else:
            st.session_state.messages.append({
                "role": "assistant",
                "content": res.get("answer", ""),
                "sql": res.get("sql_executed"),
            })
            st.rerun()