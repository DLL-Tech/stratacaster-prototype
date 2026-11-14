"""Bulk review: a TTO lead or PMO reviewer selects all, some, or none, and each item keeps its own checks (PR-25)."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from stratacaster.environment import Environment
from stratacaster.llm import MockLLM

PREP, APPROVE, PUBLISH = "2026-10-23T16:00:00Z", "2026-10-23T16:15:00Z", "2026-10-23T16:20:00Z"


@pytest.fixture
def renv(tmp_path):
    e = Environment(var_dir=tmp_path / "var", llm=MockLLM(), dataset="roster")
    e.load_all_cases()
    e.clock.set(PREP)
    for c in e.msr_cases():  # every employee accepts (submits) their MSR, as in the demo
        rv = c["scripted_review"]
        if rv and rv["decision"] == "accepted":
            e.delivery.employee_review(e.principal(rv["reviewer_id"], "employee"), c["input"]["input_id"], 1,
                                       "accepted")
    yield e
    e.close()


def maya(env):
    return env.principal("SIM-EMP-002", "tto")


def test_tto_queue_holds_only_the_leads_tto_with_input_and_alignments(renv):
    q = renv.delivery.tto_review_queue(maya(renv))
    assert q and {r["tto"] for r in q} == {"SIM-TTO-1-01"}
    rowan = next(r for r in q if r["input_id"] == "SIM-MSR-202610-005")
    assert rowan["msr_input"] and rowan["req_alignment"].startswith("SIM-REQ-1-01-")
    assert rowan["employee"] == "Rowan Bennett (SIM-EMP-005)"
    assert any(r["own_msr"] for r in q)  # Maya's own MSR is in her TTO and in her queue


def test_bulk_tto_all_some_none(renv):
    q = renv.delivery.tto_review_queue(maya(renv))
    items = [(r["input_id"], r["version"]) for r in q]
    none = renv.delivery.bulk_tto_review(maya(renv), [], "approved")
    assert not none.ok and none.data["results"] == []
    some = renv.delivery.bulk_tto_review(maya(renv), items[:3], "approved")
    assert some.ok and "3 of 3" in some.reason
    rest = renv.delivery.bulk_tto_review(maya(renv), items[3:], "returned", "please tighten wording")
    assert rest.ok and len(renv.delivery.tto_review_queue(maya(renv))) == 0


def test_bulk_tto_refuses_items_outside_the_leads_authority_per_item(renv):
    own = renv.delivery.tto_review_queue(maya(renv))[0]
    out = renv.delivery.bulk_tto_review(maya(renv), [(own["input_id"], 1), ("SIM-MSR-202610-030", 1)], "approved")
    assert not out.ok and "1 of 2" in out.reason
    bad = next(r for r in out.data["results"] if not r["ok"])
    assert bad["input_id"] == "SIM-MSR-202610-030" and "own TTO" in bad["reason"]


def test_bulk_pmo_approve_then_publish_keeps_the_gates(renv):
    for lead, role in (("SIM-EMP-002", "tto"), ("SIM-EMP-003", "tto"), ("SIM-EMP-004", "tto"),
                       ("SIM-EMP-251", "pmo"), ("SIM-EMP-001", "pmo")):  # PMO peers review the PMO staff MSRs
        p = renv.principal(lead, role)
        renv.delivery.bulk_tto_review(p, [(r["input_id"], r["version"]) for r in renv.delivery.tto_review_queue(p)],
                                      "approved")
    avery = renv.principal("SIM-EMP-001", "pmo")
    q = renv.delivery.pmo_queue(avery)
    assert all(r["employee"] != "Avery Bennett (SIM-EMP-001)" for r in q)  # own-MSR records go to a teammate
    assert q and all(r["status"] == "awaiting approval" for r in q)
    row = q[0]
    assert row["msr_input"] and row["aligned_task"] and row["solutions_receives"].startswith("Employee ")
    renv.clock.set(APPROVE)
    a = renv.delivery.bulk_pmo_approve(avery, [(r["record_id"], r["version"]) for r in q])
    blocked = [r for r in q if r["blocking"]]
    assert len([x for x in a.data["results"] if x["ok"]]) == len(q) - len(blocked) and blocked  # TC-01/02 refused
    q2 = renv.delivery.pmo_queue(avery)
    approved = [r for r in q2 if r["status"] == "approved, not published"]
    assert len(approved) == len(q) - len(blocked)
    renv.clock.set(PUBLISH)
    pub = renv.delivery.bulk_publish(avery, [(r["record_id"], r["version"]) for r in approved])
    assert pub.ok and all(x["ok"] for x in pub.data["results"])
    assert len(renv.delivery.pmo_queue(avery)) == len(blocked)  # only the blocked drafts remain


def test_bulk_publish_needs_approval_first(renv):
    q = renv.delivery.pmo_queue(renv.principal("SIM-EMP-001", "pmo"))
    out = renv.delivery.bulk_publish(renv.principal("SIM-EMP-001", "pmo"), [(q[0]["record_id"], q[0]["version"])])
    assert not out.ok and out.data["results"][0]["reason"] == "not approved yet"


def test_bulk_sections_render(tmp_path, monkeypatch):
    app = str(Path(__file__).resolve().parent.parent / "app" / "streamlit_app.py")
    monkeypatch.setenv("STRATACASTER_VAR_DIR", str(tmp_path / "ui-bulk"))
    monkeypatch.setenv("STRATACASTER_LLM_MODE", "mock")
    monkeypatch.setenv("STRATACASTER_DATASET", "roster")
    at = AppTest.from_file(app, default_timeout=120)
    at.run()
    try:
        env = at.session_state.env
        env.load_all_cases()
        env.delivery.employee_review(env.principal("SIM-EMP-005", "employee"), "SIM-MSR-202610-005", 1, "accepted")
        at.sidebar.selectbox[0].select("SIM-EMP-002").run()
        next(s for s in at.sidebar.selectbox if s.label == "Acting in role").select("tto").run()
        assert not at.exception
        md = " ".join(m.value for m in at.markdown)
        assert "Bulk TTO lead review" in md and "SIM-TTO-1-01" in md
        next(b for b in at.button if b.label == "Select all").click().run()
        next(b for b in at.button if b.label.startswith("Approved selected")).click().run()
        assert not at.exception
        assert env.delivery.tto_status("SIM-MSR-202610-005", 1) == "approved"
        at.sidebar.selectbox[0].select("SIM-EMP-001").run()
        next(s for s in at.sidebar.selectbox if s.label == "Acting in role").select("pmo").run()
        assert not at.exception and "Bulk PMO review" in " ".join(m.value for m in at.markdown)
    finally:
        at.session_state.env.close()
