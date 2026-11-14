"""StrataCaster prototype review interface.

Run:  streamlit run app/streamlit_app.py
"Acting as" simulates authentication with fictional fixture users. Every action goes through the same
guarded services the tests exercise, so denials shown here are real permission checks, not UI hiding.
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stratacaster import PROTOTYPE_LABEL  # noqa: E402
from stratacaster.environment import Environment  # noqa: E402
from stratacaster.registry import ROLE_BUNDLES  # noqa: E402
from stratacaster.stores import PermissionDenied  # noqa: E402

CAT = Path(__file__).resolve().parent / "assets" / "stratacaster_cat.png"  # circular StrataCaster cat (project deck)

st.set_page_config(page_title="StrataCaster Prototype", layout="wide", page_icon=str(CAT) if CAT.exists() else None)
if CAT.exists():
    st.logo(str(CAT), size="large")


def get_env() -> Environment:
    if "env" not in st.session_state:
        st.session_state.env = Environment()
    return st.session_state.env


# Sequential single-hue ramp (blue 100 -> 700); in-scope cells with no work use a neutral fill.
SEQ_RAMP = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
ZERO_FILL, INK, INK_ON_DARK, GAP = "#e9e8e4", "#0b0b0b", "#ffffff", "#fcfcfb"


def heatmap(rows: list[dict], x: str, y: str, value: str, tooltip: list, x_title: str, y_title: str,
            zero_label: str = "0"):
    """Rect heatmap with a 2px surface gap, count labels, and a per-cell tooltip."""
    import altair as alt
    import pandas as pd

    df = pd.DataFrame(rows)
    top = max(int(df[value].max()), 1)
    df["label"] = df[value].map(lambda v: str(v) if v else zero_label)
    df["dark"] = df[value] >= max(top * 0.55, 2)
    height = max(140, 22 * df[y].nunique() + 60)
    base = alt.Chart(df).encode(
        x=alt.X(f"{x}:N", title=x_title, axis=alt.Axis(labelAngle=-40, labelLimit=180)),
        y=alt.Y(f"{y}:N", title=y_title, axis=alt.Axis(labelLimit=260)))
    cells = base.mark_rect(cornerRadius=3, stroke=GAP, strokeWidth=2).encode(
        color=alt.condition(f"datum.{value} > 0",
                            alt.Color(f"{value}:Q", title=value.replace("_", " "),
                                      scale=alt.Scale(range=SEQ_RAMP, domain=[1, top]),
                                      legend=alt.Legend(gradientLength=120)),
                            alt.value(ZERO_FILL)),
        tooltip=tooltip)
    text = base.mark_text(fontSize=11).encode(
        text="label:N", color=alt.condition("datum.dark", alt.value(INK_ON_DARK), alt.value(INK)), tooltip=tooltip)
    return (cells + text).properties(height=height)


def render_heatmaps(env, me) -> None:
    from stratacaster import heatmaps as hm

    st.subheader("Heatmaps of PMO-published task alignments")
    if not env.registry.assess_on_submission:
        st.info("Heatmaps use the roster workflow's REQ assessment and contract-wide analytic.")
        return
    try:
        contracts = hm.contracts(env, me)
    except PermissionDenied as e:
        st.error(f"Denied: {e}. Heatmaps are a Delivery/PMO view built from records you are authorized to see.")
        return
    if not contracts:
        st.info("Nothing published yet. Heatmaps appear once the PMO publishes task alignments (tab 2).")
        return
    c1, c2 = st.columns(2)
    contract = c1.selectbox("Contract", contracts)
    months = hm.periods(env, me, contract)
    period = c2.selectbox("Reporting period", ["All periods"] + months, index=len(months) if months else 0)
    period = None if period == "All periods" else period
    st.caption("Counts only, from published records you are authorized to see (and, for potential new tasks, the "
               "Delivery-side REQ assessment). No MSR text appears here. Darker = more; grey = in scope, none yet.")

    st.markdown("#### 1 · Potential new tasks and where they may align elsewhere on the contract")
    rows = hm.potential_new_tasks(env, me, contract, period)
    if rows:
        for r in rows:
            r["target"] = r["target"] if r["target"] == hm.NOT_ELSEWHERE else f"{r['target']} · {r['target_title']}"
        st.altair_chart(heatmap(
            rows, "target", "origin_req", "statements",
            ["origin_req", "target", "target_tto", "target_title", "statements"],
            "REQ task elsewhere on the contract", "REQ where the potential new task was reported"), width="stretch")
        st.caption("Each statement tagged as a potential new task for the employee's REQ is counted in the column of "
                   "every REQ task it aligns to elsewhere (published collaboration-opportunity records), or in "
                   f"'{hm.NOT_ELSEWHERE}'. Columns other than that one are collaboration candidates.")
        with st.expander("Table view"):
            st.dataframe(rows, hide_index=True, width="stretch")
    else:
        st.caption("No TTO-approved potential new tasks in this period.")

    st.markdown("#### 2 · Cross-collaboration: tasks performed across the contract")
    show_all = st.toggle("Show every task with published work (not only cross-TTO)", value=False)
    rows = hm.cross_collaboration(env, me, contract, period, cross_only=not show_all)
    if rows:
        for r in rows:
            r["task"] = f"{r['req_task']} · {r['title']}"
        st.altair_chart(heatmap(
            rows, "performing_tto", "task", "alignments",
            ["req_task", "title", "owning_tto", "performing_tto", "relation", "alignments", "people"],
            "TTO of the people who performed the work", "REQ task (owning TTO in the ID)"), width="stretch")
        st.caption("A task with cells in more than one column is being performed in several areas of the contract; a "
                   "cell outside the task's own TTO is cross-TTO work.")
        with st.expander("Table view"):
            st.dataframe(rows, hide_index=True, width="stretch")
    else:
        st.caption("No published task is performed outside its own TTO in this period. Toggle above to see all.")

    st.markdown("#### 3 · All REQ tasks and where work was performed")
    rows = hm.req_task_coverage(env, me, contract, period)
    if rows:
        ttos = sorted({r["task_tto"] for r in rows})
        pick = st.multiselect("Task orders (rows)", ttos, default=ttos, key="hm3_ttos")
        rows = [r for r in rows if r["task_tto"] in pick]
        for r in rows:
            r["task"] = f"{r['req_task']} · {r['title']}"
            r["scope"] = "in this REQ" if r["in_req"] else "outside this REQ"
        if rows:
            st.altair_chart(heatmap(
                rows, "req", "task", "alignments",
                ["req_task", "title", "req", "role", "scope", "alignments"],
                "Work-role REQ (TTO-role)", "REQ task"), width="stretch")
            st.caption("Every REQ task on the contract against each work-role REQ that lists it. Grey '0' = the task "
                       "is in that REQ but no published work this period; a coloured cell outside the REQ's own tasks "
                       "is work done for another REQ (contract-wide analytic). Blank = not part of that REQ.")
            with st.expander("Table view"):
                st.dataframe(rows, hide_index=True, width="stretch")


def bulk_table(rows: list[dict], key: str, labels: dict):
    """Editable table with a checkbox per row and Select all / Select none. Returns the selected rows."""
    import pandas as pd

    ver, default = st.session_state.get(f"{key}_ver", 0), st.session_state.get(f"{key}_all", False)
    c = st.columns([1, 1, 5])
    for col, label, value in ((c[0], "Select all", True), (c[1], "Select none", False)):
        if col.button(label, key=f"{key}_{label}"):
            st.session_state[f"{key}_all"], st.session_state[f"{key}_ver"] = value, ver + 1
            st.rerun()
    df = pd.DataFrame(rows)
    df.insert(0, "select", default)
    config = {"select": st.column_config.CheckboxColumn("Select", help="Tick some, or use Select all / none")}
    config |= {k: st.column_config.TextColumn(v, width="large" if k in ("msr_input", "solutions_receives") else None)
               for k, v in labels.items()}
    edited = st.data_editor(df, key=f"{key}_editor_{ver}", hide_index=True, width="stretch",
                            disabled=[x for x in df.columns if x != "select"], column_config=config,
                            column_order=["select"] + [k for k in labels if k in df.columns])
    picked = edited[edited["select"]]
    c[2].caption(f"{len(picked)} of {len(df)} selected")
    return picked


def bulk_tto_panel(env, me, queue: list[dict], key: str) -> None:
    """TTO-stage bulk review: TTO leads for their TTO(s), PMO teammates for contract PMO staff."""
    bulk_result(key)
    if not queue:
        st.caption("Nothing awaiting your review: MSRs appear here once the employee has submitted (accepted) them "
                   "and the REQ assessment has run.")
        return
    st.caption("Check that each input is professional and aligned to the REQ tasks shown (or is a potential new task "
               "requirement). Tick some, all, or none, then decide.")
    picked = bulk_table(queue, key, {
        "input_id": "MSR", "tto": "TTO", "employee": "Employee", "msr_input": "MSR input",
        "req_alignment": "Aligned REQ tasks", "input_tag": "Tag", "employee_feedback": "Employee feedback",
        "elsewhere_on_contract": "Also aligns elsewhere on the contract", "own_msr": "Own MSR"})
    reason = st.text_input("Reason (recorded with each decision; required to return or reject)", key=f"{key}_reason")
    b = st.columns(3)
    for i, decision in enumerate(["approved", "returned", "rejected"]):
        if b[i].button(f"{decision.title()} selected ({len(picked)})", key=f"{key}_{decision}",
                       type="primary" if decision == "approved" else "secondary",
                       disabled=picked.empty or (decision != "approved" and not reason.strip())):
            bulk_done(key, env.delivery.bulk_tto_review(
                me, list(zip(picked["input_id"], picked["version"].astype(int))), decision, reason))


def bulk_done(key: str, outcome) -> None:
    """Keep the bulk result across the rerun that refreshes the queue, and clear the selection."""
    st.session_state[f"{key}_result"] = outcome
    st.session_state[f"{key}_all"] = False
    st.session_state[f"{key}_ver"] = st.session_state.get(f"{key}_ver", 0) + 1
    st.rerun()


def bulk_result(key: str) -> None:
    out = st.session_state.pop(f"{key}_result", None)
    if out is not None:
        (st.success if out.ok else st.warning)(out.reason)
        failed = [r for r in out.data.get("results", []) if not r["ok"]]
        if failed:
            st.dataframe(failed, hide_index=True, width="stretch")


FEEDBACK_LABELS = {"accurate": "Accurate: this is what I meant",
                   "not_intended": "Not what I intended",
                   "confirm_new": "Yes, this is new work: flag it for TTO lead review",
                   "not_new": "Not new: it is part of my existing tasks"}


def submission_panel(env, me, prof) -> bool:
    """Employee MSR submission with a feedback check first (PR-30): check the draft against the REQ, respond to each
    statement, then edit and re-check or submit. Nothing is stored until Submit. Returns True after a submission."""
    done = st.session_state.pop("msr_done", None)
    if done is not None:
        flash(done)
    if st.session_state.pop("msr_clear", False):
        for k in ("msr_month", "bullet1", "bullet2", "bullet3"):
            st.session_state.pop(k, None)
    month = st.text_input("Reporting month (YYYY-MM)", value=env.clock.now().strftime("%Y-%m"), key="msr_month")
    bullets = [st.text_area(f"Narrative bullet {n}", height=68, key=f"bullet{n}") for n in (1, 2, 3)]
    if not env.registry.assess_on_submission:  # Build Guide dataset: no REQ assessment, so no feedback check
        if st.button("Submit MSR", type="primary"):
            out = env.delivery.submit_msr(me, month.strip(), bullets)
            flash(out)
            return out.ok
        return False

    st.markdown("**Step 1 · Check against my REQ.** The Role Execution Agent reviews the draft; nothing is saved, "
                "logged, or sent for review until you submit.")
    if st.button("Check against my REQ", type="primary"):
        out = env.delivery.preview_msr(me, month.strip(), bullets)
        if out.ok:
            prev = st.session_state.get("msr_preview")
            draft = 1
            if prev and prev["user"] == me.user_id:  # a re-check: mirror the edits first (presentation mode only)
                draft = prev.get("draft", 1) + 1
                for i, (old, new) in enumerate(zip(prev["bullets"], bullets), 1):
                    if old.strip() != new.strip():
                        env.presentation.edit(me.user_id, env.clock.now_str(), i, old, new)
            env.presentation.check(me.user_id, env.clock.now_str(), month.strip(), out.data, env.registry, draft)
            st.session_state.msr_preview = {"data": out.data, "month": month.strip(), "bullets": list(bullets),
                                            "user": me.user_id, "draft": draft}
        else:
            st.session_state.pop("msr_preview", None)
            flash(out)
    pv = st.session_state.get("msr_preview")
    if not pv or pv["user"] != me.user_id:
        return False
    data = pv["data"]
    stale = pv["month"] != month.strip() or pv["bullets"] != list(bullets)
    st.markdown(f"**Step 2 · Feedback** against `{data['role_req_id'] or 'no REQ identified for your role'}`")
    engine = data.get("engine", "model")
    if engine == "local":
        st.caption("Preliminary, free check: keyword and local similarity matching, no model call. The Role Execution "
                   "Agent makes the full assessment when you submit, and writes any recommended new task for your TTO "
                   "lead. Re-check as often as you like.")
    elif engine == "hybrid":
        st.caption(f"Preliminary check: free local matching, plus recommended new-task wording from a small model "
                   f"(about ${data['model'].get('cost_usd', 0):.4f}). The full assessment runs when you submit.")
    if stale:
        st.warning("You edited the draft after this check. Check it again before submitting.")
    req = env.registry.role_req_for(prof["role_id"], prof["tto_id"])
    my_tasks = [""] + (req["req_task_ids"] if req else [])
    task_label = (lambda t: f"{t} {env.registry.req_tasks[t]['title']}" if t else "(not sure)")
    responses = {}
    for r in data["statements"]:
        with st.container(border=True):
            st.markdown(f"**Statement {r['index']}:** {r['text']}")
            if r["tag"] == "aligned":
                st.success("Aligned to " + "; ".join(
                    f"{a['req_task_id']} {a['title']}" + (" (reported as not completed)"
                                                         if a["reported_status"] == "not_completed" else "")
                    for a in r["aligned"]))
                options = ["accurate", "not_intended"]
            else:
                st.warning("Potential new task: no task in your REQ covers this work.")
                s = r["suggested_task"]
                if s:
                    st.markdown(f"Recommended new task: **{s['title']}**  \n{s['description']}  \n*{s['rationale']}*")
                elif engine == "local":
                    st.caption("Recommended new-task wording is written by the assessment when you submit; your TTO "
                               "lead sees it with your response.")
                else:
                    st.caption("No usable recommended wording was produced; your TTO lead will see the statement.")
                for e in r.get("similar_elsewhere", []):
                    st.caption(f"Similar work elsewhere on your contract: {e['req_task_id']} {e['title']} "
                               f"({e['tto_id']}, similarity {e['similarity']:.2f})")
                options = ["confirm_new", "not_new"]
            key = f"fb_{data['preview_id']}_{r['index']}"
            choice = st.radio("Your response", options, format_func=FEEDBACK_LABELS.get, key=key, horizontal=True)
            intended, note = None, ""
            if choice in ("not_intended", "not_new"):
                intended = st.selectbox("Which of your REQ tasks did you mean? (optional)", my_tasks,
                                        format_func=task_label, key=f"{key}_task") or None
                note = st.text_input("Note for your TTO lead (optional)", max_chars=300, key=f"{key}_note")
            responses[r["index"]] = {"response": choice, "intended_req_task_id": intended, "note": note}
    flagged = sum(v["response"] != "accurate" for v in responses.values())
    st.markdown("**Step 3 · Submit.** Edit the bullets above and check again, or submit as is with your responses."
                + (f" {flagged} statement(s) will be flagged for higher-level review." if flagged else ""))
    c = st.columns(2)
    if c[0].button("Submit MSR with my responses", type="primary", disabled=stale):
        env.presentation.responses(me.user_id, env.clock.now_str(), responses)
        out = env.delivery.submit_msr(me, month.strip(), bullets, preview_id=data["preview_id"], responses=responses)
        env.presentation.submitted(me.user_id, env.clock.now_str(), out)
        if out.ok:
            st.session_state.last_input = out.data["input_id"]
            st.session_state.pop("msr_preview", None)
            st.session_state.msr_clear = True
            st.session_state.msr_done = out
            st.rerun()
        flash(out)
    if c[1].button("Discard this check"):
        st.session_state.pop("msr_preview", None)
        st.rerun()
    return False


def flash(outcome) -> None:
    ok = outcome.ok if hasattr(outcome, "ok") else outcome.get("ok")
    reason = outcome.reason if hasattr(outcome, "reason") else outcome.get("reason")
    (st.success if ok else st.error)(f"{'Allowed' if ok else 'Denied'}: {reason}")
    data = getattr(outcome, "data", None)
    if data:
        with st.expander("Details"):
            st.json(data)


env = get_env()

# ------------------------------------------------------------------ sidebar
st.sidebar.markdown(f"**{PROTOTYPE_LABEL}**")
humans = sorted(env.registry.human_users(), key=lambda u: (not u.get("featured", False), u["user_id"]))
reg = env.registry


def person_facets(u: dict) -> tuple[set, str | None, str | None]:
    """(role keys, contract, TTO) used by the people filter."""
    roles = {k for k, _, b in ROLE_BUNDLES if b & set(u.get("roles", []))}
    s = reg.subjects.get(u["user_id"], {})
    return roles, s.get("contract_id"), s.get("tto_id")


facets = {u["user_id"]: person_facets(u) for u in humans}
with st.sidebar.expander("Filter people", expanded=False):
    role_names = {k: label for k, label, _ in ROLE_BUNDLES}
    f_roles = st.multiselect("Role", list(role_names), format_func=lambda k: role_names[k],
                             help="People holding any of the chosen roles")
    f_contracts = st.multiselect("Contract", sorted({c for _, c, _ in facets.values() if c}))
    f_ttos = st.multiselect("Task order (TTO)", sorted({t for _, c, t in facets.values() if t
                                                        and (not f_contracts or c in f_contracts)}),
                            help="TTO the person is assigned to, or leads")
    st.checkbox("Featured demo users only (★)", key="featured_only")
    if st.session_state.get("featured_only"):
        humans = [u for u in humans if u.get("featured")]
    humans = [u for u in humans
              if (not f_roles or facets[u["user_id"]][0] & set(f_roles))
              and (not f_contracts or facets[u["user_id"]][1] in f_contracts)
              and (not f_ttos or facets[u["user_id"]][2] in f_ttos or set(u.get("tto_authority", [])) & set(f_ttos))]
    st.caption(f"{len(humans)} of {len(facets)} people match")
if not humans:
    st.sidebar.warning("No one matches the filter; showing everyone.")
    humans = sorted(env.registry.human_users(), key=lambda u: (not u.get("featured", False), u["user_id"]))
users = {u["user_id"]: ("★ " if u.get("featured") else "") + f"{u['user_id']} · {u['display']}"
         + (f" · {facets[u['user_id']][2]}" if facets[u["user_id"]][2] else "")
         + (f" · leads {', '.join(u['tto_authority'])}" if u.get("tto_authority") else "") for u in humans}
uid = st.sidebar.selectbox("Acting as (simulated authentication; type to search)", list(users),
                           format_func=lambda u: users[u])
role_opts = env.registry.role_options(uid)
role_labels = dict(role_opts)
role_keys = [k for k, _ in role_opts]
if len(role_keys) > 1:  # managers are also employees: choose which hat this session wears
    role = st.sidebar.selectbox(
        "Acting in role", role_keys, index=next((i for i, k in enumerate(role_keys) if k != "employee"), 0),
        format_func=lambda k: role_labels[k],
        help="Only the chosen role's authority applies. Act as Employee to work your own MSR without management "
             "authority.")
else:
    role = role_keys[0] if role_keys else None
me = env.principal(uid, role)
if env.cfg.get("governance", {}).get("management_authority_covers_own_msr", False) is False and len(role_keys) > 1:
    st.sidebar.caption("Separation of duties is on: management roles cannot act on your own MSR.")
st.sidebar.caption(f"Domain: **{me.domain}** · Roles: {', '.join(sorted(me.roles))}"
                   + (f" · PMO authority: {', '.join(me.pmo_authority)}" if me.pmo_authority else "")
                   + (f" · TTO lead of: {', '.join(sorted(me.tto_authority))}" if me.tto_authority else ""))

st.sidebar.divider()
st.sidebar.markdown(f"**Clock:** `{env.clock.now_str()}` ({'fixed' if env.clock.is_fixed else 'system'})")
new_time = st.sidebar.text_input("Set clock (UTC, ...Z)", value=env.clock.now_str())
c1, c2, c3 = st.sidebar.columns(3)
if c1.button("Set"):
    try:
        env.clock.set(new_time)
        st.rerun()
    except ValueError as e:
        st.sidebar.error(str(e))
if c2.button("+1 h"):
    env.clock.advance(hours=1)
    st.rerun()
if c3.button("+32 d"):
    env.clock.advance(days=32)
    st.rerun()

st.sidebar.divider()
v = env.versions()
st.sidebar.markdown(f"**Model service:** `{v['llm_mode']}` · `{v['model']}`")
st.sidebar.markdown(f"**Dataset:** `{env.dataset}` · `{v['reference_fixture']}`")
if env.registry.demo.get("scope"):
    sc = env.registry.demo["scope"]
    st.sidebar.caption(f"Demo slice: {', '.join(sc['contracts'])} · {', '.join(sc['months'])} + test cases")
st.session_state.setdefault("presentation_mode", env.presentation.enabled)  # kept across environment resets
env.presentation.enabled = st.sidebar.toggle(
    "Presentation mode", key="presentation_mode",
    help="Mirror who is acting (and in which role) and the employee's feedback dialogue to the People Monitor for an "
         "audience. The check itself is never recorded; this writes a separate, non-authoritative demo feed. Leave "
         "off outside demonstrations.")
if env.presentation.enabled and role and st.session_state.get("acting_sent") != (uid, role):
    env.presentation.acting(uid, env.clock.now_str(), role)  # the People Monitor shows the selection at once
    st.session_state.acting_sent = (uid, role)
elif not env.presentation.enabled:
    st.session_state.pop("acting_sent", None)  # re-announce the selection when presentation mode is turned on
if st.sidebar.button("Load synthetic dataset", help="Loads the dataset's demo slice as each contract's Delivery operator"):
    res = env.load_all_cases()
    st.sidebar.dataframe([{k: r[k] for k in ("case_id", "ok", "reason")} for r in res], hide_index=True)
if st.sidebar.button("Reset environment (synthetic data only)", type="secondary"):
    env.close()
    st.session_state.env = Environment(reset=True)
    st.session_state.pop("acting_sent", None)  # the reset wiped the feed: announce the current selection again
    st.rerun()

if CAT.exists():
    _icon, _head = st.columns([1, 11], vertical_alignment="center")
    _icon.image(str(CAT), width=88)
    _head.title("StrataCaster governed evidence prototype")
else:
    st.title("StrataCaster governed evidence prototype")
if env.registry.assess_on_submission:
    st.caption("Workflow A: employee submits MSR → Role Execution Agent assesses it against the employee's REQ "
               "(aligned tasks or potential new task requirement) → TTO lead review → human PMO approval → separate "
               "publication of task alignments (no MSR content). Workflow B: permissioned retrieval → cited, "
               "non-authoritative explanation → reviewer acknowledgement.")
else:
    st.caption("Workflow A: reviewed MSR input → Role/REQ alignment → human PMO approval → separate publication. "
               "Workflow B: permissioned retrieval → cited, non-authoritative explanation → reviewer acknowledgement.")

tabs = st.tabs(["1 · Delivery inputs", "2 · PMO review & publication", "3 · Published heatmaps", "4 · Solutions query",
                "5 · Escalations", "6 · Audit trail", "7 · Agents & configuration", "8 · REQ review (ToT pilot)"])

# ------------------------------------------------------------------ 1. Delivery
with tabs[0]:
    st.subheader("Delivery source store (raw MSRs; Delivery domain only)")
    try:
        inputs = env.delivery_store.list_inputs(me)
    except PermissionDenied as e:
        st.error(f"Denied by the Delivery store: {e}")
        inputs = None
    if inputs is not None and me.has_role("employee_reviewer") and env.registry.profile(me.user_id):
        prof = env.registry.profile(me.user_id)
        with st.expander("Submit a new MSR (as this employee)",
                         expanded=bool(st.session_state.get("msr_preview") or st.session_state.get("msr_done"))):
            st.caption(f"Labeled like the dataset: {env.registry.id_prefix}-MSR-<YYYYMM>-"
                       f"{me.user_id.rsplit('-', 1)[-1]} · contract {prof['contract_id']} · task order "
                       f"{prof['tto_id']} · role {prof['role_id']}. A second submission for the same month becomes "
                       "the next version.")
            if submission_panel(env, me, prof):
                inputs = env.delivery_store.list_inputs(me)
    if inputs is not None and me.tto_authority:  # TTO lead: show what they lead, and filter across those TTOs
        led = sorted(me.tto_authority)
        st.info("**You are the TTO lead for:** " + " · ".join(
            f"{t} ({env.registry.ttos[t]['title']})" for t in led))
        shown = led
        if len(led) > 1:
            shown = st.multiselect("Filter by TTO", led, default=led)
            inputs = [i for i in inputs if i.tto_id in shown]
        if env.registry.tto_review_required:
            st.markdown(f"### Bulk TTO lead review · approving for **{', '.join(shown) or 'no TTO selected'}**")
            bulk_tto_panel(env, me, [r for r in env.delivery.tto_review_queue(me) if r["tto"] in shown], "bulk_tto")
            st.markdown("### Review one MSR")
    if inputs is not None and me.has_role("human_pmo_reviewer") and env.registry.tto_review_required:
        st.markdown(f"### PMO peer review of PMO staff MSRs · {', '.join(sorted(me.pmo_authority))}")
        st.caption("Contract PMO staff are not assigned to a TTO, so no TTO lead reviews them. A PMO teammate other "
                   "than the author reviews their MSRs at this stage; your own MSR goes to a teammate.")
        bulk_tto_panel(env, me, env.delivery.tto_review_queue(me), "bulk_peer")
        st.markdown("### Review one MSR")
    if inputs is not None:
        st.caption(f"Showing the {len(inputs)} MSR(s) this user is authorized to see: their own MSRs as an employee, "
                   "their contract as an operator, the TTO(s) they lead as a TTO lead, or their PMO's contract as a "
                   "PMO reviewer.")
        if not inputs:
            st.info("No visible inputs. Load the dataset as a Delivery operator, or act as a user with access.")
        else:
            tto_on = env.registry.tto_review_required
            assess_on = env.registry.assess_on_submission
            assessed = {i.input_id: env.delivery.assessment(me, i.input_id, i.input_version)
                        for i in inputs} if assess_on else {}
            st.dataframe([{"input_id": i.input_id, "version": i.input_version, "subject": i.subject_id,
                           "role": i.role_id, "tto": i.tto_id, "source": i.source_status,
                           "employee review": i.review.status,
                           **({"REQ assessment": (assessed[i.input_id] or {}).get("input_tag", "not assessed")}
                              if assess_on else {}),
                           **({"TTO lead review": env.delivery.tto_status(i.input_id, i.input_version)}
                              if tto_on else {})} for i in inputs],
                         hide_index=True, width="stretch")
            ids = [i.input_id for i in inputs]
            last = st.session_state.get("last_input")  # keep the selection when switching users
            sel = st.selectbox("Input", ids, index=ids.index(last) if last in ids else 0)
            st.session_state.last_input = sel
            msr = next(i for i in inputs if i.input_id == sel)
            st.markdown(f"**Raw narrative (Delivery-only):** {msr.raw_narrative}")
            st.caption(f"Assigned employee reviewer: {env.registry.employee_reviewer_for(msr.subject_id)}")
            a = assessed.get(sel)
            if assess_on and a:
                st.markdown(f"**REQ assessment** against `{a['role_req_id'] or 'no REQ identified for this role'}` "
                            f"· input tag: **{a['input_tag']}**")
                stmts = dict(env.delivery_store.get_statements(me, sel, msr.input_version))
                fb = a.get("employee_feedback")
                st.dataframe([{"statement": stmts.get(s["statement_id"], ""), "tag": s["tag"],
                               "aligned REQ tasks": "; ".join(f"{t} {env.registry.req_tasks[t]['title']}"
                                                              for t in s["req_task_ids"]),
                               **({"employee's check": s.get("check_tag", "") if s.get("check_agreed", True)
                                   else f"{s.get('check_tag')} (differs)",
                                   "employee response": FEEDBACK_LABELS.get(s.get("employee_response"), ""),
                                   "recommended new task": (s.get("suggested_task") or {}).get("title", ""),
                                   "intended task": s.get("intended_req_task_id") or "",
                                   "employee note": s.get("notes") or ""} if fb else {})}
                              for s in a["statements"]], hide_index=True, width="stretch")
                if fb:
                    st.markdown(f"**Employee feedback:** {env.delivery._feedback_text(a)}")
                cw = a.get("contract_wide")
                if cw:
                    st.markdown(f"**Contract-wide analytic** (other REQs on {msr.contract_id}; tracked separately) · "
                                f"similar work: **{cw['counts'].get('similar-work', 0)}** · collaboration "
                                f"opportunities: **{cw['counts'].get('collaboration-opportunity', 0)}**")
                    if cw["statements"]:
                        st.dataframe([{"statement": stmts.get(r["statement_id"], ""), "signal": r["signal"],
                                       "also aligns to": "; ".join(
                                           f"{x['req_task_id']} {env.registry.req_tasks[x['req_task_id']]['title']} "
                                           f"({x['tto_id']}; {', '.join(x['reqs']) or 'no REQ'})"
                                           for x in r["elsewhere"])} for r in cw["statements"]],
                                     hide_index=True, width="stretch")
                    else:
                        st.caption("No statement aligns to another REQ on the contract.")
            elif assess_on:
                st.caption("Not yet assessed: the REQ assessment runs when the employee submits (accepts) the MSR.")
            col = st.columns(4)
            for i, decision in enumerate(["accepted", "rejected", "correction_requested"]):
                if col[i].button(f"Employee review: {decision}", key=f"rev-{decision}"):
                    flash(env.delivery.employee_review(me, sel, msr.input_version, decision,
                                                       correlation_id=f"SYN-WF-A-{sel}"))
            agent_label = "Re-run REQ assessment" if assess_on else "Run Role Execution Agent"
            if col[3].button(agent_label, type="primary"):
                flash(env.delivery.prepare_candidates(me, sel, correlation_id=f"SYN-WF-A-{sel}"))
            if tto_on:
                status = env.delivery.tto_status(sel, msr.input_version)
                peer = env.registry.is_pmo_staff_tto(msr.tto_id)
                if peer:
                    st.caption(f"TTO-stage review: **{status}** · {msr.tto_id} staff have no TTO lead; a member of the "
                               f"{env.registry.pmo_for_contract(msr.contract_id)} team other than the author decides")
                else:
                    lead_id = env.registry.tto_lead_for(msr.tto_id)
                    st.caption(f"TTO lead review: **{status}** · only the lead of {msr.tto_id} "
                               f"({lead_id or 'no TTO lead assigned'}) may decide, including for their own MSR")
                if me.has_role("tto_lead") or (peer and me.has_role("human_pmo_reviewer")):
                    if not peer and msr.tto_id in me.tto_authority:
                        st.success(f"Reviewing as TTO lead **for {msr.tto_id}** "
                                   f"({env.registry.ttos[msr.tto_id]['title']}). Check that the input is "
                                   "professional and aligned to the REQ tasks shown, or is a potential new task "
                                   "requirement.")
                    elif peer and msr.subject_id != me.user_id:
                        st.success(f"Reviewing as a PMO teammate **for {msr.tto_id}** (contract PMO staff). Check "
                                   "that the input is professional and aligned to the PMO REQ tasks shown.")
                    tcol = st.columns(3)
                    label = "PMO peer review" if peer else "TTO lead"
                    for i, decision in enumerate(["approved", "returned", "rejected"]):
                        if tcol[i].button(f"{label}: {decision}", key=f"tto-{decision}"):
                            flash(env.delivery.tto_review(me, sel, msr.input_version, decision,
                                                          correlation_id=f"SYN-WF-A-{sel}"))
            with st.expander("Correct or withdraw this input"):
                txt = st.text_area("Corrected narrative (creates a new version; review resets)", key="corr")
                cc = st.columns(2)
                if cc[0].button("Submit correction") and txt.strip():
                    flash(env.delivery.correct_input(me, sel, txt.strip()))
                if cc[1].button("Withdraw source"):
                    flash(env.delivery.withdraw_input(me, sel))
            runs = env.delivery_store.alignment_runs(me, sel)
            if runs:
                with st.expander(f"Alignment runs ({len(runs)}) - Delivery-only intermediate content"):
                    st.json(runs[-1])

# ------------------------------------------------------------------ 2. PMO
with tabs[1]:
    st.subheader("Approved-evidence repository (filtered records)")
    try:
        records = env.evidence_store.list_records(me)
    except PermissionDenied as e:
        st.error(f"Denied: {e}. Solutions users retrieve evidence only through the gateway (tab 4).")
        records = None
    if records and me.has_role("human_pmo_reviewer"):
        st.markdown(f"### Bulk PMO review · {', '.join(sorted(me.pmo_authority))}")
        bulk_result("bulk_pmo")
        queue = env.delivery.pmo_queue(me)
        if not queue:
            st.caption("Nothing awaiting approval or publication.")
        else:
            st.caption("One row per aligned task. Read the MSR input against the aligned task and what Solutions "
                       "will receive (never the MSR input). Approval and publication stay separate, recorded actions: "
                       "approve the selected rows awaiting approval, then publish the selected approved rows. Rows "
                       "whose TTO lead review is not approved, or with blocking exceptions, are refused per item.")
            picked = bulk_table(queue, "bulk_pmo", {
                "record_id": "Record", "status": "Status", "source_msr": "MSR", "employee": "Employee",
                "msr_input": "MSR input", "aligned_task": "Aligned task", "alignment": "Alignment",
                "solutions_receives": "Solutions receives", "tto_review": "TTO lead", "blocking": "Blocking"})
            pending = picked[picked["status"] == "awaiting approval"]
            approved = picked[picked["status"] == "approved, not published"]
            reason = st.text_input("Decision reason", value="Bulk approval of the reviewed task alignments.",
                                   key="bulk_pmo_reason")
            b = st.columns(2)
            if b[0].button(f"Approve selected ({len(pending)})", type="primary", disabled=pending.empty,
                           key="bulk_pmo_approve"):
                bulk_done("bulk_pmo", env.delivery.bulk_pmo_approve(
                    me, list(zip(pending["record_id"], pending["version"].astype(int))), reason))
            if b[1].button(f"Publish selected approved ({len(approved)})", disabled=approved.empty,
                           key="bulk_pmo_publish"):
                bulk_done("bulk_pmo", env.delivery.bulk_publish(
                    me, list(zip(approved["record_id"], approved["version"].astype(int)))))
        st.markdown("### Review one record")
    if records:
        if env.registry.publish_task_alignment_only:
            st.caption("Final view: one task-alignment record per aligned REQ task (an MSR aligned to several tasks "
                       "yields several records, and all are published). Records carry the task and the aligned "
                       "employee data (name, ID, role, TTO), never the MSR input. Contract-wide analytic records "
                       "(alignment = similar-work or collaboration-opportunity) are reviewed and published the same "
                       "way and reach Solutions as related work, separate from coverage.")
        tto_on = env.registry.tto_review_required
        st.dataframe([{"record": r.record_id, "v": r.record_version, "pmo": r.owning_pmo_id,
                       "source MSR": r.source_ref.input_id, "tto": r.tto_id, "req_task": r.req_task_id,
                       "alignment": r.alignment_scope,
                       **({"TTO lead review": env.delivery.tto_status(r.source_ref.input_id,
                                                                      r.source_ref.input_version)} if tto_on else {}),
                       "pmo_approval": r.pmo_approval.status, "disposition": r.disposition,
                       "summary (what Solutions receives)": r.evidence_summary,
                       "expires": r.freshness.expires_at,
                       "blocking": ", ".join(e for e in r.exceptions if e.startswith("BLOCKING"))} for r in records],
                     hide_index=True, width="stretch")
        keys = [f"{r.record_id} v{r.record_version}" for r in records]
        pick = st.selectbox("Record version", keys, index=len(keys) - 1)
        rec = records[keys.index(pick)]
        left, right = st.columns([3, 2])
        with left:
            st.json(rec.model_dump(), expanded=False)
        with right:
            st.markdown("**Recomputed validation snapshot**")
            st.json(env.delivery.validate_for_approval(me, rec))
        reason = st.text_input("Decision reason", value="Fictional reviewer decision on the minimized summary.")
        cols = st.columns(4)
        for i, d in enumerate(["approved", "rejected", "returned_for_correction", "revoked"]):
            if cols[i].button(d.replace("_", " ").title(), key=f"pmo-{d}"):
                flash(env.delivery.pmo_decide(me, rec.record_id, rec.record_version, d, reason,
                                              correlation_id=f"SYN-WF-A-{rec.record_id}"))
        events = [e for e in env.evidence_store.approval_events_for(me, rec.record_id) if e.decision == "approved"]
        if events:
            ev_id = st.selectbox("Approval event to publish with", [e.event_id for e in events],
                                 index=len(events) - 1)
            if st.button("Publish (separate action; rechecks every prerequisite)", type="primary"):
                flash(env.delivery.publish(me, rec.record_id, rec.record_version, ev_id,
                                           correlation_id=f"SYN-WF-A-{rec.record_id}"))
        with st.expander("Revise summary (creates next version; PMO approval resets)"):
            ns = st.text_input("New evidence summary", key="newsum")
            if st.button("Create revision") and ns.strip():
                flash(env.delivery.revise_evidence(me, rec.record_id, ns.strip()))
        with st.expander("Approval events and publication log"):
            st.json([e.model_dump() for e in env.evidence_store.approval_events_for(me, rec.record_id)])
            st.dataframe(env.evidence_store.publications(me, rec.record_id), hide_index=True)
    elif records == []:
        st.info("No filtered records yet. They are created when an employee submits (accepts) an MSR that aligns to "
                "REQ tasks (tab 1)." if env.registry.assess_on_submission else
                "No filtered records yet. Run the Role Execution Agent on an employee-accepted input (tab 1).")

# ------------------------------------------------------------------ 3. Published heatmaps
with tabs[2]:
    render_heatmaps(env, me)

# ------------------------------------------------------------------ 4. Solutions
with tabs[3]:
    st.subheader("Requirement-focused query (Solutions Orchestrator → Expertise Agent)")
    with st.form("query"):
        aud = st.selectbox("Audience", list(env.registry.audiences))
        purpose = st.selectbox("Purpose", list(env.registry.purposes))
        first_tto = next(iter(env.registry.req_tasks.values()))["tto_id"]
        default_tasks = [t for t, r in env.registry.req_tasks.items() if r["tto_id"] == first_tto]
        tasks = st.multiselect("REQ tasks", list(env.registry.req_tasks), default=default_tasks,
                               format_func=lambda t: f"{t} - {env.registry.req_tasks[t]['title']}")
        question = st.text_area("Question", "What approved evidence covers these requirements?")
        raw = st.checkbox("Also include raw source MSRs (should be denied)")
        submitted = st.form_submit_button("Submit query", type="primary")
    if submitted:
        st.session_state.last = env.orchestrator.handle(me, {
            "requester_id": me.user_id, "audience_id": aud, "purpose": purpose, "req_task_ids": tasks,
            "question": question, "include_raw_sources": raw})
    resp = st.session_state.get("last")
    if resp:
        st.markdown(f"**{resp['label']}**")
        status = resp["status"]
        (st.error if status in ("denied", "failed_safe") else st.warning if status == "escalated" else st.success)(
            f"Status: {status}" + (f" - {resp['reason']}" if resp.get("reason") else ""))
        if resp.get("coverage"):
            st.dataframe(resp["coverage"], hide_index=True, width="stretch")
            st.markdown(resp["explanation"])
            st.caption(f"Uncertainty: {resp['uncertainty']}")
            st.markdown("**Citations (filtered records only)**")
            st.dataframe(resp["citations"], hide_index=True, width="stretch")
            if resp.get("related_work"):
                st.markdown("**Related work elsewhere on the contract** (contract-wide analytic: similar work and "
                            "collaboration opportunities; not counted as coverage)")
                st.dataframe(resp["related_work"], hide_index=True, width="stretch")
            st.caption(f"Records withheld by the gateway: {resp['records_withheld']} · "
                       f"Escalations: {', '.join(resp['escalations']) or 'none'} · Model: {resp['model']}")
        if status in ("awaiting_acknowledgement", "escalated") and st.button("Acknowledge as responsible reviewer"):
            flash(env.orchestrator.acknowledge(me, resp["query_id"]))
    st.subheader("Expertise search (semantic retrieval)")
    st.caption("Chroma vector store with all-MiniLM-L6-v2 embeddings over requirement tasks and published records. The "
               "gateway decides eligibility first; the search then takes 8 candidates and reranks to 4, needing both "
               "the controlling requirement and approved evidence, with one targeted retry before escalating.")
    with st.form("semantic"):
        s_aud = st.selectbox("Audience", list(env.registry.audiences), key="s_aud")
        s_q = st.text_input("Question", "Who has confirmed that reported fixes were actually closed?")
        s_go = st.form_submit_button("Search", type="primary")
    if s_go:
        with st.spinner("Searching (the first search loads the embedding model)..."):
            st.session_state.last_search = env.orchestrator.expertise_search(me, s_q, s_aud)
    sr = st.session_state.get("last_search")
    if sr:
        t = sr.get("trace")
        (st.error if sr["status"] in ("denied", "failed_safe") else st.warning if sr["status"] == "incomplete"
         else st.success)(f"Status: {sr['status']}" + (f" - {sr['reason']}" if sr.get("reason") else ""))
        if t:
            st.dataframe(t["results"], hide_index=True, width="stretch")
            if t["excluded_from_ranking"]:
                st.caption("Excluded before ranking: " + "; ".join(
                    f"{b['id']} ({', '.join(b['reasons'])})" for b in t["excluded_from_ranking"]))
            if t["retry"]:
                st.caption(f"Targeted retry for missing {t['retry']['missing']}: found {t['retry']['found'] or 'none'}")
            with st.expander("Retrieval trace"):
                st.json(t, expanded=False)
    with st.expander("Response history"):
        st.dataframe([{k: r[k] for k in ("query_id", "created_at", "requester_id", "status", "acknowledged_by")}
                      for r in env.ops.responses()], hide_index=True)

# ------------------------------------------------------------------ 4. Escalations
with tabs[4]:
    st.subheader("Exceptions routed to named human reviewers")
    escs = env.ops.escalations()
    if not escs:
        st.info("No escalations.")
    for e in escs:
        with st.container(border=True):
            st.markdown(f"**{e['escalation_id']}** · {e['kind']} · route to **{e['route_to']}** ({e['route_domain']}) "
                        f"· {e['status']}")
            st.write(e["summary"])
            st.caption(f"refs: {e['refs']} · correlation: {e['correlation_id']}")
            if e["status"] == "open":
                note = st.text_input("Resolution note", key=f"res-{e['escalation_id']}")
                if st.button("Resolve", key=f"btn-{e['escalation_id']}"):
                    flash(env.orchestrator.resolve_escalation(me, e["escalation_id"], note or "Reviewed."))

# ------------------------------------------------------------------ 5. Audit
with tabs[5]:
    st.subheader("Append-only audit trail (sanitized)")
    cid = st.text_input("Filter by correlation id")
    import json as _json
    st.dataframe([{k: (_json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in e.items()}
                  for e in env.audit.entries(correlation_id=cid or None, limit=1000)], hide_index=True,
                 width="stretch")  # nested refs/meta shown as JSON text so the table renders cleanly

# ------------------------------------------------------------------ 6. Agents
with tabs[6]:
    st.subheader("Prototype agent registry")
    st.dataframe([{k: a[k] for k in ("agent_id", "name", "maturity", "prototype_status", "domain", "prompt_version")}
                  for a in env.agent_registry.records.values()], hide_index=True, width="stretch")
    st.markdown("**Excluded (future extensions; not invocable):**")
    st.dataframe(env.agent_registry.excluded, hide_index=True)
    with st.expander("Full agent records"):
        st.json(env.agent_registry.records)
    st.markdown("**Versions**")
    st.json(env.versions())

# ------------------------------------------------------------------ 8. REQ review (Tree of Thought pilot)
TREE_COLORS = {"root": "#d0d7de", "kept": "#9fd5a8", "beam-cut": "#f2d48f", "pruned": "#f0a8a8"}


def search_tree_dot(plan: dict) -> str:
    best = {d["node_id"] for d in plan["plans"][:1]}
    chain, rows = set(), {r["node_id"]: r for r in plan["trace"]}
    node = next(iter(best), None)
    while node:
        chain.add(node)
        node = rows[node].get("parent")
    lines = ['digraph T { rankdir=LR; node [shape=box, style="rounded,filled", fontname=Helvetica, fontsize=10];']
    for r in plan["trace"]:
        label = "root" if r["status"] == "root" else (
            f"{r['cluster_id']} · {r['operator']}\\n{r['wording'][:28]}\\n"
            + (f"score {r['score']}" if "score" in r else "pruned"))
        pen = ", penwidth=3" if r["node_id"] in chain else ""
        lines.append(f'"{r["node_id"]}" [label="{label}", fillcolor="{TREE_COLORS[r["status"]]}"{pen}];')
        if r.get("parent"):
            lines.append(f'"{r["parent"]}" -> "{r["node_id"]}";')
    return "\n".join(lines + ["}"])


with tabs[7]:
    st.subheader("REQ-change planning: Tree of Thought pilot (CR-SC-2026-003)")
    st.caption("Recurring 'potential new task' statements from TTO-approved MSRs are clustered. The TTO Coordination "
               "Agent proposes options for each cluster; deterministic code checks constraints, scores, prunes, and "
               "keeps the best 3 partial plans per level (beam search, depth ≤ 3). The TTO lead endorses one plan; "
               "the contract's PMO, the approval authority for REQ changes, approves, returns, or rejects it. An "
               "approved plan becomes a REQ change record for the next REQ version.")
    planner = env.req_planner
    if me.has_role("human_pmo_reviewer"):
        queue = planner.pmo_queue(me)
        st.markdown(f"**REQ changes awaiting PMO approval** ({len(queue)})")
        if not queue:
            st.caption("No endorsed plans are waiting for your PMO. Plans you endorsed yourself go to a teammate.")
        for pl in queue:
            t = pl["tto_decision"]
            chosen = t["endorsed_plan"]
            with st.container(border=True):
                st.markdown(f"**{pl['role_req_id']}** · {pl['period']} · plan {t['plan_rank']} (score "
                            f"{chosen['plan_score']}) endorsed by {t['decided_by']} ({t['review_basis']}) · "
                            f"{pl['plan_id']}")
                st.dataframe([{"cluster": d["cluster_id"], "operator": d["operator"],
                               "target": d["target_req_task_id"] or "", "wording": d["wording"],
                               "score": d["score"], "rationale": d["rationale"]} for d in chosen["decisions"]],
                             hide_index=True, width="stretch")
                why = st.text_input("Reason (required to return or reject)", key=f"rqa-why-{pl['plan_id']}")
                a1, a2, a3 = st.columns(3)
                for col, label, dec in ((a1, "Approve REQ change", "approved"), (a2, "Return", "returned"),
                                        (a3, "Reject", "rejected")):
                    if col.button(label, key=f"rqa-{dec}-{pl['plan_id']}", type="primary" if dec == "approved"
                                  else "secondary"):
                        flash(planner.pmo_decide_req_plan(me, pl["plan_id"], dec, why))
        st.divider()
    reqs = planner.reviewable_reqs(me)
    if not env.registry.role_reqs:
        st.info("This dataset has no work-role REQs.")
    elif not reqs:
        if not me.has_role("human_pmo_reviewer"):
            st.info("Act as a TTO lead (or, for contract PMO staff REQs, a PMO team member) to plan REQ changes, or "
                    "as a PMO reviewer to approve them.")
    else:
        rmap = {r["role_req_id"]: r for r in reqs}
        rid = st.selectbox("Work-role REQ", list(rmap), key="rq-req", format_func=lambda r: (
            f"{r} · {env.registry.roles.get(rmap[r]['role_id'], {}).get('name', rmap[r]['role_id'])} · "
            f"{len(rmap[r]['req_task_ids'])} tasks"))
        periods = planner.periods(rid)
        period = st.selectbox("Period", periods + ["all"], key="rq-period",
                              help="Months with TTO-approved MSRs assessed against this REQ")
        if st.button("Run REQ-change planning", key="rq-run"):
            flash(planner.plan_req(me, rid, None if period == "all" else period))
        history = planner.plans(me, rid)
        if not history:
            st.info("No plans yet for this REQ. Approve MSRs at the TTO stage first, then run planning.")
        else:
            plan = history[-1]
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Clusters", len(plan["clusters"]))
            c2.metric("Nodes generated", f"{plan['nodes_generated']} / {plan['params']['node_budget']}")
            c3.metric("Ranked plans", len(plan["plans"]))
            c4.metric("Status", plan["status"])
            st.caption(f"{plan['plan_id']} · {plan['period']} · created {plan['created_at']} by "
                       f"{plan['requested_by']} · {plan['agent_id']} ({plan['prompt_version']}) · "
                       f"{len(plan['excluded_statements'])} flagged statement(s) excluded before the agent · REQ size "
                       f"limit: {plan.get('req_size_limit') or 'none (contract setting)'}")
            st.markdown("**Clusters** (searched clusters are the tree's levels, in order of employee support)")
            st.dataframe([{"cluster": c["cluster_id"], "employees": c["employees"],
                           "statements": len(c["statement_ids"]), "label (content words)": c["label"],
                           "elsewhere on contract": ", ".join(e["req_task_id"] for e in c["elsewhere"]),
                           "searched": c["cluster_id"] in plan["searched_clusters"]} for c in plan["clusters"]],
                         hide_index=True, width="stretch")
            with st.expander("Statements in the searched clusters (Delivery only)"):
                ids = {s: c["cluster_id"] for c in plan["clusters"] if c["cluster_id"] in plan["searched_clusters"]
                       for s in c["statement_ids"]}
                texts = []
                for c in plan["clusters"]:
                    if c["cluster_id"] not in plan["searched_clusters"]:
                        continue
                    for iid in c["input_ids"]:
                        m = env.delivery_store.get_input(me, iid)
                        texts += [{"cluster": ids[s], "statement": s, "text": t} for s, t in
                                  env.delivery_store.get_statements(me, iid, m.input_version) if s in ids]
                st.dataframe(sorted(texts, key=lambda x: (x["cluster"], x["statement"])), hide_index=True,
                             width="stretch")
            st.markdown("**Ranked plans**")
            for p in plan["plans"]:
                with st.container(border=True):
                    st.markdown(f"**Plan {p['rank']}** · score {p['plan_score']}"
                                + (" · recommended" if p["rank"] == plan["recommended_rank"] else ""))
                    st.dataframe([{"cluster": d["cluster_id"], "operator": d["operator"],
                                   "target": d["target_req_task_id"] or "", "wording": d["wording"],
                                   "score": d["score"], **d["parts"], "rationale": d["rationale"]}
                                  for d in p["decisions"]], hide_index=True, width="stretch")
            st.markdown("**Search tree** (green kept in the beam · amber cut by the beam · red pruned; bold path = "
                        "recommended plan)")
            st.graphviz_chart(search_tree_dot(plan))
            with st.expander("Trace (every node, with prune reasons)"):
                st.dataframe([{k: r.get(k) for k in ("node_id", "parent", "depth", "cluster_id", "operator",
                                                     "target_req_task_id", "wording", "score", "plan_score",
                                                     "status", "reason")} for r in plan["trace"]],
                             hide_index=True, width="stretch")
            t, pm = plan["tto_decision"], plan["pmo_decision"]
            if t:
                st.info(f"TTO stage: {t['decision']}" + (f" plan {t['plan_rank']}" if t["plan_rank"] else "")
                        + f" by {t['decided_by']} ({t['review_basis']}) at {t['decided_at']}. Next: {t['next_step']}.")
            if pm:
                (st.success if pm["decision"] == "approved" else st.warning)(
                    f"PMO {pm['pmo_id']}: {pm['decision']} by {pm['decided_by']} at {pm['decided_at']}"
                    + (f" · change {pm['req_change']['change_id']}" if pm["req_change"] else "")
                    + f". Next: {pm['next_step']}.")
            if not t and plan["plans"]:
                st.markdown("**Your endorsement** (the agent decides nothing; the PMO approves REQ changes)")
                rank = st.radio("Plan", [p["rank"] for p in plan["plans"]], horizontal=True, key="rq-rank",
                                format_func=lambda r: f"Plan {r}")
                why = st.text_input("Reason (required to reject all)", key="rq-why")
                b1, b2 = st.columns(2)
                if b1.button(f"Endorse plan {rank} for PMO approval", key="rq-endorse", type="primary"):
                    flash(planner.decide_req_plan(me, plan["plan_id"], "endorsed", rank, why))
                    st.rerun()
                if b2.button("Reject all plans", key="rq-reject"):
                    flash(planner.decide_req_plan(me, plan["plan_id"], "rejected", None, why))
            if len(history) > 1:
                with st.expander(f"Earlier runs ({len(history) - 1})"):
                    st.dataframe([{"plan_id": h["plan_id"], "period": h["period"], "created_at": h["created_at"],
                                   "plans": len(h["plans"]), "status": h["status"]} for h in history[:-1]],
                                 hide_index=True, width="stretch")
