"""Heatmaps of PMO-published task alignments (PR-24)."""
import json

import pytest
from streamlit.testing.v1 import AppTest

from stratacaster import heatmaps as hm
from stratacaster.demo import run_roster_demo
from stratacaster.environment import Environment
from stratacaster.llm import MockLLM

C = "SIM-FC-001"


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    e = Environment(var_dir=tmp_path_factory.mktemp("hm") / "var", llm=MockLLM(), dataset="roster")
    try:
        run_roster_demo(e, say=lambda *_: None)
        yield e, e.principal("SIM-EMP-001", "pmo")
    finally:
        e.close()


def test_periods_and_contracts_come_from_published_records(demo):
    env, pmo = demo
    assert hm.contracts(env, pmo) == [C]
    assert hm.periods(env, pmo, C) == ["2026-10", "2026-11"]


def test_potential_new_tasks_show_where_they_align_elsewhere(demo):
    env, pmo = demo
    rows = hm.potential_new_tasks(env, pmo, C)
    leila = {r["target"]: r["statements"] for r in rows if r["origin_req"] == "SIM-REQ-1-01-WR-001"}
    assert leila["SIM-REQ-1-02-06"] == 1 and leila["SIM-REQ-1-02-07"] == 1  # collaboration opportunity
    assert all(r["target"] == hm.NOT_ELSEWHERE for r in hm.potential_new_tasks(env, pmo, C, "2026-10"))


def test_cross_collaboration_shows_tasks_performed_outside_their_tto(demo):
    env, pmo = demo
    rows = hm.cross_collaboration(env, pmo, C)
    cross = {(r["req_task"], r["performing_tto"]) for r in rows if r["relation"] == "cross-TTO"}
    assert cross == {("SIM-REQ-1-02-06", "SIM-TTO-1-01"), ("SIM-REQ-1-02-07", "SIM-TTO-1-01"),
                     ("SIM-REQ-1-03-03", "SIM-TTO-1-01")}
    assert len(hm.cross_collaboration(env, pmo, C, cross_only=False)) > len(rows)


def test_req_task_coverage_lists_every_req_task_and_distinguishes_no_work(demo):
    env, pmo = demo
    rows = hm.req_task_coverage(env, pmo, C, "2026-10")
    cell = {(r["req_task"], r["req"]): r for r in rows}
    assert cell[("SIM-REQ-1-01-05", "SIM-REQ-1-01-WR-001")]["alignments"] == 0  # in the REQ, no work reported
    assert cell[("SIM-REQ-1-01-01", "SIM-REQ-1-01-WR-001")]["alignments"] > 0
    in_req = {t for q in env.registry.role_reqs.values() if q["tto_id"].startswith("SIM-TTO-1-")
              for t in q["req_task_ids"]}
    assert {r["req_task"] for r in rows} == in_req  # every REQ task on the contract appears


def test_heatmaps_carry_no_msr_text(demo):
    env, pmo = demo
    blob = json.dumps([hm.potential_new_tasks(env, pmo, C), hm.cross_collaboration(env, pmo, C, False),
                       hm.req_task_coverage(env, pmo, C)])
    for c in env.msr_cases():
        assert c["input"]["raw_narrative"] not in blob


def test_heatmaps_respect_visibility(demo):
    env, _ = demo
    rowan = env.principal("SIM-EMP-005", "employee")
    rows = hm.cross_collaboration(env, rowan, C, cross_only=False)
    assert {r["performing_tto"] for r in rows} == {"SIM-TTO-1-01"} and sum(r["people"] for r in rows) == len(rows)


def test_heatmap_tab_renders(tmp_path, monkeypatch):
    from pathlib import Path
    app = str(Path(__file__).resolve().parent.parent / "app" / "streamlit_app.py")
    monkeypatch.setenv("STRATACASTER_VAR_DIR", str(tmp_path / "ui-hm"))
    monkeypatch.setenv("STRATACASTER_LLM_MODE", "mock")
    monkeypatch.setenv("STRATACASTER_DATASET", "roster")
    at = AppTest.from_file(app, default_timeout=120)
    at.run()
    try:
        env = at.session_state.env
        run_roster_demo(env, say=lambda *_: None)
        at.sidebar.selectbox[0].select("SIM-EMP-001").run()
        assert not at.exception
        headings = " ".join(m.value for m in at.markdown)
        for n in ("1 · Potential new tasks", "2 · Cross-collaboration", "3 · All REQ tasks"):
            assert n in headings
    finally:
        at.session_state.env.close()
