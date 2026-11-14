"""People Monitor: the people in the demo, what each is doing, and the employee feedback dialogue.

Open it next to the Workflow Monitor (two browser windows, side by side) while the demo runs. It reads the audit log
and the demo's presentation feed, both read-only, so it cannot change anything. The feedback dialogue appears only
when the presentation feed is on: `scripts/run_demo.py` turns it on, and the review app has a Presentation mode
toggle.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from stratacaster.monitor import (CSS, LEGEND, MonitorState, audit_path, feed_path,  # noqa: E402
                                  load_reference, read_events, render_people_board)
from stratacaster.presentation import read_feed  # noqa: E402

CAT = ROOT / "app" / "assets" / "stratacaster_cat.png"

st.set_page_config(page_title="StrataCaster People Monitor", layout="wide",
                   page_icon=str(CAT) if CAT.exists() else None)
st.markdown(CSS, unsafe_allow_html=True)
if CAT.exists():
    st.logo(str(CAT), size="large")
    icon, head = st.columns([1, 9], vertical_alignment="center")
    icon.image(str(CAT), width=72)
    head.title("People Monitor")
else:
    st.title("People Monitor")
st.caption("One card per authority level: who is acting in it and what they are doing. Actions come from the audit "
           "trail. With Presentation mode on in the review app, a card also switches as soon as someone is selected "
           "in that role, and the employee's feedback dialogue appears (the check itself is never recorded).")
st.markdown(LEGEND, unsafe_allow_html=True)

hold = st.sidebar.slider("Highlight duration (seconds)", 2, 10, 4, key="people_hold")
refresh = st.sidebar.select_slider("Refresh every (seconds)", options=[0.5, 1.0, 2.0], value=1.0,
                                   key="people_refresh")
DB, FEED = audit_path(ROOT), feed_path(ROOT)
st.sidebar.caption(f"Reading `{DB}` and the presentation feed (read-only)")


@st.cache_data
def reference() -> dict:
    return load_reference(ROOT)


@st.fragment(run_every=refresh)
def live() -> None:
    now = time.time()
    ref = reference()
    title = (lambda t: f"{t} {ref.get('tasks', {}).get(t, '')}".strip())
    state: MonitorState | None = st.session_state.get("people_monitor")
    rows, max_seq = read_events(DB, state.last_seq if state else 0)
    feed, feed_len = read_feed(FEED, state.feed_pos if state else 0)
    if state is not None and (max_seq < state.last_seq or feed_len < state.feed_pos):  # the environment was reset
        state = None
        rows, _ = read_events(DB, 0)
        feed, _ = read_feed(FEED, 0)
    if state is None:  # show history without lighting it up
        state = MonitorState()
        state.apply(rows, now=0.0, tasks_title=title)
        state.apply_feed(feed, now=0.0)
    else:
        state.apply(rows, now=now, tasks_title=title)
        state.apply_feed(feed, now=now)
    st.session_state.people_monitor = state
    if not state.selections:
        st.info("Cards follow actions from the audit trail. To switch a card as soon as someone is selected in the "
                "review app, turn on **Presentation mode** in the review app's sidebar.")
    for block in render_people_board(state, now, hold, ref):
        st.markdown(block, unsafe_allow_html=True)


live()

if st.sidebar.button("Clear highlights and counters", key="people_clear"):
    _, max_seq = read_events(DB, 0)
    _, feed_len = read_feed(FEED, 0)
    st.session_state.people_monitor = MonitorState(last_seq=max_seq, feed_pos=feed_len)
    st.rerun()
