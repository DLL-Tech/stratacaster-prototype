"""Workflow Monitor: a live diagram of Workflows A and B that lights up as steps execute.

Open it in a second browser tab (or on the projector) while running the demo in the review app or
with `scripts/run_demo.py --pace 2`. It only reads the audit log; it cannot change anything. For the people in the
demo and the employee feedback dialogue, open the People Monitor alongside it.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from stratacaster.monitor import (CSS, LEGEND, MonitorState, audit_path, read_events,  # noqa: E402
                                  render_board)

CAT = ROOT / "app" / "assets" / "stratacaster_cat.png"  # circular StrataCaster cat from the project deck

st.set_page_config(page_title="StrataCaster Workflow Monitor", layout="wide",
                   page_icon=str(CAT) if CAT.exists() else None)
st.markdown(CSS, unsafe_allow_html=True)
if CAT.exists():
    st.logo(str(CAT), size="large")
    icon, head = st.columns([1, 11], vertical_alignment="center")
    icon.image(str(CAT), width=88)
    head.title("Workflow Monitor")
else:
    st.title("Workflow Monitor")
st.caption("Live view of the audit trail. Steps light up as agents and people act; colors show the latest outcome.")
st.markdown(LEGEND, unsafe_allow_html=True)

hold = st.sidebar.slider("Highlight duration (seconds)", 2, 10, 4)
refresh = st.sidebar.select_slider("Refresh every (seconds)", options=[0.5, 1.0, 2.0], value=1.0)
DB = audit_path(ROOT)
st.sidebar.caption(f"Reading `{DB}` (read-only)")


@st.fragment(run_every=refresh)
def live() -> None:
    now = time.time()
    state: MonitorState | None = st.session_state.get("monitor")
    rows, max_seq = read_events(DB, state.last_seq if state else 0)
    if state is None or max_seq < state.last_seq:  # first open, or the environment was reset
        state = MonitorState()
        state.apply(rows, now=0.0)  # show history without lighting it up
    else:
        state.apply(rows, now=now)
    st.session_state.monitor = state

    for block in render_board(state, now, hold):
        st.markdown(block, unsafe_allow_html=True)
    st.markdown(f"**Latest correlation:** `{state.active_correlation or '—'}`  ·  "
                f"**Audit events seen:** {state.last_seq}")
    st.dataframe([{"seq": r["seq"], "demo time": r["ts"], "actor": r.get("actor_label", r["actor"]),
                   "action": r["action"],
                   "outcome": r["outcome"], "reason": (r["reason"] or "")[:90],
                   "correlation": r["correlation_id"]} for r in state.feed[:15]],
                 hide_index=True, width="stretch")


live()

if st.sidebar.button("Clear highlights and counters"):
    _, max_seq = read_events(DB, 0)
    st.session_state.monitor = MonitorState(last_seq=max_seq)
    st.rerun()
