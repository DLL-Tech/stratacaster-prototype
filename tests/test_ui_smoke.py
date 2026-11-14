"""Smoke test: the review interface renders and enforces domains for different acting users."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "app" / "streamlit_app.py")


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATACASTER_VAR_DIR", str(tmp_path / "ui-var"))
    monkeypatch.setenv("STRATACASTER_LLM_MODE", "mock")
    monkeypatch.setenv("STRATACASTER_DATASET", "guide")
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    yield at
    at.session_state.env.close()


def test_renders_without_exceptions(app):
    assert not app.exception
    assert "PROTOTYPE OUTPUT" in app.sidebar.markdown[0].value


def test_solutions_user_sees_denial_for_delivery_store(app):
    app.sidebar.selectbox[0].select("SYN-SOLUTIONS-USER-001").run()
    assert not app.exception
    assert any("Denied by the Delivery store" in e.value for e in app.error)


def test_roster_dataset_renders_and_loads(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATACASTER_VAR_DIR", str(tmp_path / "ui-roster"))
    monkeypatch.setenv("STRATACASTER_LLM_MODE", "mock")
    monkeypatch.setenv("STRATACASTER_DATASET", "roster")
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    try:
        assert not at.exception
        at.sidebar.selectbox[0].select("SIM-EMP-002").run()  # Mission Technical Director = FC-001 operator
        next(b for b in at.sidebar.button if b.label == "Load synthetic dataset").click().run()
        assert not at.exception
        env = at.session_state.env
        assert len(env.delivery_store.list_inputs(env.principal("SIM-EMP-002"))) == 61  # TC-10 is FC-002's
        at.sidebar.selectbox[0].select("SIM-EMP-005").run()  # Rowan Bennett, employee reviewer
        assert not at.exception
        inputs_box = next(s for s in at.selectbox if s.label == "Input")
        assert sorted(inputs_box.options) == ["SIM-MSR-202610-005", "SIM-MSR-TEST-01"]
        at.sidebar.selectbox[0].select("SIM-EMP-002").run()  # a manager has a role selector
        role_box = next(s for s in at.sidebar.selectbox if s.label == "Acting in role")
        assert role_box.value == "operator"
        role_box.select("employee").run()
        assert not at.exception
        assert next(s for s in at.selectbox if s.label == "Input").options == ["SIM-MSR-202610-002"]
    finally:
        at.session_state.env.close()


def test_people_filter_narrows_the_user_list(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATACASTER_VAR_DIR", str(tmp_path / "ui-filter"))
    monkeypatch.setenv("STRATACASTER_LLM_MODE", "mock")
    monkeypatch.setenv("STRATACASTER_DATASET", "roster")
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    def ids():  # option labels start with the user ID
        return sorted(o.lstrip("★ ").split(" · ")[0] for o in at.sidebar.selectbox[0].options)

    try:
        everyone = len(ids())
        next(m for m in at.sidebar.multiselect if m.label == "Role").select("tto").run()
        assert not at.exception
        leads = ids()
        assert 0 < len(leads) < everyone and "SIM-EMP-002" in leads and "SIM-EMP-005" not in leads
        next(m for m in at.sidebar.multiselect if m.label == "Contract").select("SIM-FC-001").run()
        assert ids() == ["SIM-EMP-002", "SIM-EMP-003", "SIM-EMP-004"]
        next(m for m in at.sidebar.multiselect if m.label == "Task order (TTO)").select("SIM-TTO-1-02").run()
        assert ids() == ["SIM-EMP-003"]
    finally:
        at.session_state.env.close()


def test_employee_checks_responds_and_submits(tmp_path, monkeypatch):
    """PR-30 in the interface: check the draft, see feedback, respond, then submit; nothing is stored before."""
    monkeypatch.setenv("STRATACASTER_VAR_DIR", str(tmp_path / "ui-feedback"))
    monkeypatch.setenv("STRATACASTER_LLM_MODE", "mock")
    monkeypatch.setenv("STRATACASTER_DATASET", "roster")
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    try:
        at.sidebar.selectbox[0].select("SIM-EMP-005").run()
        env = at.session_state.env
        rowan = env.principal("SIM-EMP-005", "employee")
        at.text_area(key="bullet1").input("I reran checks on the systems that were missed and added them to the "
                                          "patching queue.")
        at.text_area(key="bullet2").input("I organized the team picnic.")
        next(b for b in at.button if b.label == "Check against my REQ").click().run()
        assert not at.exception
        assert any("Potential new task" in w.value for w in at.warning)
        assert any(c.value.startswith("Preliminary, free check") for c in at.caption)  # default: free local check
        assert env.delivery_store.list_inputs(rowan) == []  # nothing stored by the check
        at.text_area(key="bullet2").input("I organized the team picnic and the holiday party.").run()
        assert any("edited the draft" in w.value for w in at.warning)
        assert next(b for b in at.button if b.label == "Submit MSR with my responses").disabled
        next(b for b in at.button if b.label == "Check against my REQ").click().run()
        next(b for b in at.button if b.label == "Submit MSR with my responses").click().run()
        assert not at.exception
        inputs = env.delivery_store.list_inputs(rowan)
        assert len(inputs) == 1
        a = env.delivery.assessment(rowan, inputs[0].input_id)
        assert a["employee_feedback"]["confirm_new"] == 1 and a["employee_feedback"]["check_engine"] == "local"
        assert any(s.get("suggested_task") for s in a["statements"])  # wording written at submission
    finally:
        at.session_state.env.close()


def test_presentation_mode_announces_who_is_acting(tmp_path, monkeypatch):
    """With Presentation mode on, selecting a person (and role) writes an 'acting' event for the People Monitor."""
    from stratacaster.presentation import read_feed

    monkeypatch.setenv("STRATACASTER_VAR_DIR", str(tmp_path / "ui-acting"))
    monkeypatch.setenv("STRATACASTER_LLM_MODE", "mock")
    monkeypatch.setenv("STRATACASTER_DATASET", "roster")
    monkeypatch.delenv("STRATACASTER_PRESENTATION", raising=False)
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    try:
        feed_file = tmp_path / "ui-acting" / "presentation_feed.jsonl"
        assert not feed_file.exists()  # off by default
        next(t for t in at.sidebar.toggle if t.label == "Presentation mode").set_value(True).run()
        at.sidebar.selectbox[0].select("SIM-EMP-002").run()
        next(s for s in at.sidebar.selectbox if s.label == "Acting in role").select("tto").run()
        assert not at.exception
        events = [(r["actor"], r["role"]) for r in read_feed(feed_file, 0)[0] if r["kind"] == "acting"]
        assert ("SIM-EMP-002", "tto") in events and len(events) >= 2  # the first user, then Maya as TTO lead
    finally:
        at.session_state.env.close()


def test_presentation_mode_survives_a_reset(tmp_path, monkeypatch):
    """Regression: resetting the environment turned Presentation mode off, so selecting Rowan never reached the
    People Monitor. The toggle now persists and the selection is announced again after the reset."""
    from stratacaster.presentation import read_feed

    monkeypatch.setenv("STRATACASTER_VAR_DIR", str(tmp_path / "ui-reset"))
    monkeypatch.setenv("STRATACASTER_LLM_MODE", "mock")
    monkeypatch.setenv("STRATACASTER_DATASET", "roster")
    monkeypatch.delenv("STRATACASTER_PRESENTATION", raising=False)
    at = AppTest.from_file(APP, default_timeout=90)
    at.run()
    try:
        next(t for t in at.sidebar.toggle if t.label == "Presentation mode").set_value(True).run()
        next(b for b in at.sidebar.button if b.label.startswith("Reset environment")).click().run()
        assert next(t for t in at.sidebar.toggle if t.label == "Presentation mode").value is True
        at.sidebar.selectbox[0].select("SIM-EMP-005").run()
        assert not at.exception
        feed = read_feed(tmp_path / "ui-reset" / "presentation_feed.jsonl", 0)[0]
        assert ("SIM-EMP-005", "employee") in [(r["actor"], r.get("role")) for r in feed if r["kind"] == "acting"]
    finally:
        at.session_state.env.close()


def test_load_dataset_as_delivery_user(app):
    app.sidebar.selectbox[0].select("SYN-DELIVERY-OPS-001").run()
    next(b for b in app.sidebar.button if b.label == "Load synthetic dataset").click().run()
    assert not app.exception
    assert len(app.session_state.env.delivery_store.list_inputs(app.session_state.env.principal("SYN-DELIVERY-OPS-001"))) == 9

