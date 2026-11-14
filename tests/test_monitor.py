"""Workflow Monitor: event-to-step mapping, the page itself, and reset while files are open."""
from pathlib import Path

from streamlit.testing.v1 import AppTest

from helpers import OPS, PMO1, SOL1, publish_inputs, query
from stratacaster.environment import Environment
from stratacaster.llm import MockLLM
from stratacaster.monitor import DENIED, ESCALATED, OK, MonitorState, read_events

PAGE = str(Path(__file__).resolve().parent.parent / "app" / "pages" / "1_Workflow_Monitor.py")


def _state(env) -> MonitorState:
    rows, _ = read_events(env.var_dir / "audit.db", 0)
    s = MonitorState()
    s.apply(rows, now=100.0)
    return s


def test_full_run_lights_every_step(env):
    publish_inputs(env, "SYN-MSR-001", "SYN-MSR-006")
    query(env, query_id="Q-MON")
    env.delivery.revoke(env.principal(PMO1), "SYN-EVID-002", 1, "Fictional revocation.")
    try:
        env.delivery_store.list_inputs(env.principal(SOL1))
    except Exception:
        pass
    s = _state(env)
    for step in ("A2", "A3", "A5", "B1", "B2"):
        assert s.steps[step].status == OK and s.steps[step].count > 0, step
    assert s.steps["A1"].status == DENIED  # the last case loaded (12) is rejected by design
    assert s.steps["A4"].status == ESCALATED  # latest PMO event was the revocation
    assert s.steps["B3"].status == ESCALATED  # conflict between the two records
    assert s.steps["B4"].status == ESCALATED and "evidence-conflict" in s.steps["B4"].detail
    assert s.steps["CHG"].status == ESCALATED
    assert s.steps["BND"].status == DENIED and SOL1 in s.steps["BND"].detail
    assert s.steps["B5"].count == 0


def test_denials_show_red(env):
    env.load_all_cases()  # three cases are rejected at load
    env.clock.set("2026-10-16T16:00:00Z")
    env.delivery.prepare_candidates(env.principal(OPS), "SYN-MSR-009")  # unreviewed
    query(env, include_raw_sources=True)
    s = _state(env)
    assert s.steps["A3"].status == DENIED
    assert s.steps["B1"].status == DENIED
    assert s.steps["A1"].count == 12


def test_incremental_reads_only_new_events(env):
    env.load_all_cases()
    rows, max_seq = read_events(env.var_dir / "audit.db", 0)
    assert len(rows) == max_seq > 0
    assert read_events(env.var_dir / "audit.db", max_seq)[0] == []


def test_missing_audit_log_is_empty(tmp_path):
    assert read_events(tmp_path / "nope.db", 0) == ([], 0)


def test_monitor_page_renders(env, monkeypatch):
    publish_inputs(env, "SYN-MSR-001")
    monkeypatch.setenv("STRATACASTER_VAR_DIR", str(env.var_dir))
    at = AppTest.from_file(PAGE, default_timeout=60)
    at.run()
    assert not at.exception
    html = " ".join(m.value for m in at.markdown)
    assert "Workflow A" in html and "Workflow B" in html and "REQ assessment" in html and "TTO lead review" in html


def test_presentation_feed_is_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("STRATACASTER_PRESENTATION", raising=False)
    env = Environment(var_dir=tmp_path / "var", llm=MockLLM(), dataset="roster")
    try:
        assert not env.presentation.enabled
        env.clock.set("2026-10-23T16:00:00Z")
        pv = env.delivery.preview_msr(env.principal("SIM-EMP-005", "employee"), "2026-11", ["I organized the picnic."])
        env.presentation.check("SIM-EMP-005", "", "2026-11", pv.data, env.registry)
        assert not (env.var_dir / "presentation_feed.jsonl").exists()
    finally:
        env.close()


def test_dialogue_demo_shows_people_tasks_and_the_dialogue(tmp_path, monkeypatch):
    """The demo with dialogue mirrors the unrecorded check to the monitor, and the people row shows who did what."""
    from stratacaster.demo import run_roster_demo
    from stratacaster.monitor import load_reference, render_board, render_people_board
    from stratacaster.presentation import read_feed

    monkeypatch.setenv("STRATACASTER_FEEDBACK_MODE", "local")
    env = Environment(var_dir=tmp_path / "var", llm=MockLLM(), dataset="roster")
    try:
        summary = run_roster_demo(env, say=lambda *_: None, dialogue=True)
        assert set(summary["dialogues"]) == {"SIM-EMP-005", "SIM-EMP-006"}
        assert summary["dialogues"]["SIM-EMP-005"]["not_intended"] == 1
        feed, n = read_feed(env.var_dir / "presentation_feed.jsonl", 0)
        kinds = [r["kind"] for r in feed]
        assert kinds.count("check") == 3 and "edit" in kinds and kinds.count("submitted") == 2
        rows, _ = read_events(env.var_dir / "audit.db", 0)
        ref = load_reference(Path(__file__).resolve().parent.parent)
        s = MonitorState()
        s.apply(rows, now=100.0, tasks_title=lambda t: t)
        s.apply_feed(feed, now=100.0)
        assert s.steps["A0"].count == 3
        assert s.dialogue["actor"] == "SIM-EMP-006" and s.dialogue["stage"] == "Submitted"
        for uid in ("SIM-EMP-005", "SIM-EMP-006", "SIM-EMP-002", "SIM-EMP-001", "SIM-EMP-210"):
            assert s.people[uid].count > 0, uid
        assert s.people["SIM-EMP-005"].tasks  # REQ tasks from Rowan's latest assessed MSR
        roles = s.roles
        assert roles["tto"].tallies["SIM-EMP-002"]["approved"] > 0  # Maya's decisions as TTO lead
        assert roles["solutions"].current == "SIM-EMP-210"  # Amara asked and acknowledged
        assert roles["agent"].current and roles["operator"].count > 0 and roles["pmo"].count > 0
        assert "SIM-EMP-005" in roles["employee"].recent and "SIM-EMP-006" in roles["employee"].recent
        people = " ".join(render_people_board(s, 100.0, 4, ref))
        for label in ("Employee", "TTO lead", "PMO reviewer", "Delivery operator", "Solutions requester", "Agents"):
            assert label in people, label
        # the Employee card names the employee's TTO lead (Leila, SIM-TTO-1-01 -> Maya Bennett)
        assert roles["employee"].current == "SIM-EMP-006"
        assert ref["reviewer"]["SIM-EMP-006"]["lead"] == "SIM-EMP-002"
        assert "TTO lead:</b> Maya Bennett (SIM-EMP-002, SIM-TTO-1-01)" in people
        pmo_staff = next(u for u, v in ref["reviewer"].items() if v["pmo_id"])
        assert ref["reviewer"][pmo_staff]["lead"] is None  # contract PMO staff: a PMO teammate reviews instead
        assert "Employee dialogue" in people and "Actions, latest first" in people and s.people_log[0]["ts"]
        board = " ".join(render_board(s, 100.0, 4))  # the Workflow Monitor is unchanged: no people or dialogue
        assert "Workflow A" in board and "Who is acting" not in board and "Feedback check" not in board
    finally:
        env.close()


def test_people_monitor_page_renders(tmp_path, monkeypatch):
    from stratacaster.demo import run_roster_demo

    monkeypatch.setenv("STRATACASTER_FEEDBACK_MODE", "local")
    env = Environment(var_dir=tmp_path / "var", llm=MockLLM(), dataset="roster")
    try:
        run_roster_demo(env, say=lambda *_: None, dialogue=True)
    finally:
        env.close()
    monkeypatch.setenv("STRATACASTER_VAR_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("STRATACASTER_DATASET", "roster")
    at = AppTest.from_file(str(Path(PAGE).with_name("2_People_Monitor.py")), default_timeout=60)
    at.run()
    assert not at.exception
    html = " ".join(m.value for m in at.markdown)
    assert "Who is acting, by authority level" in html and "Leila Bennett" in html and "Employee dialogue" in html


def test_selecting_someone_in_the_app_switches_their_role_card(tmp_path):
    """Presentation feed 'acting' events: a card shows the person just selected for that role, even before they act;
    several people in one role are listed, latest first."""
    from stratacaster.monitor import render_people_board
    from stratacaster.presentation import PresentationFeed, read_feed

    feed = PresentationFeed(tmp_path / "feed.jsonl", enabled=True)
    feed.acting("SIM-EMP-002", "2026-10-23T16:00:00Z", "tto")
    feed.acting("SIM-EMP-003", "2026-10-23T16:01:00Z", "tto")  # a second session, another TTO lead
    feed.acting("SIM-EMP-005", "2026-10-23T16:02:00Z", "employee")
    rows, _ = read_feed(tmp_path / "feed.jsonl", 0)
    s = MonitorState()
    s.apply_feed(rows, now=100.0)
    assert s.roles["tto"].current == "SIM-EMP-003" and "SIM-EMP-002" in s.roles["tto"].recent
    assert s.roles["employee"].current == "SIM-EMP-005" and s.roles["tto"].count == 0  # selections are not actions
    ref = {"users": {"SIM-EMP-002": ("Maya Bennett", "Mission Technical Director"),
                     "SIM-EMP-003": ("Elias Bennett", "Mission Technical Director")}}
    html = " ".join(render_people_board(s, 100.0, 4, ref))
    assert "Elias Bennett" in html and "Also active in this role: Maya Bennett" in html


def test_reset_succeeds_while_another_environment_holds_files(tmp_path):
    var = tmp_path / "var"
    holder = Environment(var_dir=var, llm=MockLLM(), dataset="guide")
    holder.load_all_cases()
    fresh = Environment(var_dir=var, llm=MockLLM(), reset=True, dataset="guide")  # files may be open in `holder`
    try:
        assert fresh.delivery_store.list_inputs(fresh.principal(OPS)) == []
        assert fresh.audit.entries() == []
    finally:
        fresh.close()
        holder.close()
