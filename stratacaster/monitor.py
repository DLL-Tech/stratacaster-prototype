"""Maps audit-trail events onto workflow steps for the Workflow Monitor page.

The monitor only reads the audit log, through a short-lived read-only connection, so it can never
change workflow state and never holds a database open between refreshes. During a demonstration it also reads the
presentation feed (stratacaster/presentation.py), which mirrors the employee's unrecorded feedback dialogue. From both
it shows the people in the demo and what each is doing.
"""
from __future__ import annotations

import html
import json
import os
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

OK, DENIED, ESCALATED = "ok", "denied", "escalated"

# Step id -> (workflow, label, kind). Order defines the left-to-right flow.
STEPS = {
    "A0": ("A", "Feedback check", "Employee · not recorded"),
    "A1": ("A", "Load / submit MSR", "System · employee"),
    "A2": ("A", "Employee submit / review", "Human gate"),
    "A3": ("A", "REQ assessment", "Role Execution Agent"),
    "A2T": ("A", "TTO lead review", "Human gate · own TTO only"),
    "A4": ("A", "PMO decision", "Human gate"),
    "A5": ("A", "Publish", "System"),
    "BND": ("X", "Boundary check", "Enforcement"),
    "CHG": ("X", "Change handling", "Revise · correct · withdraw · revoke"),
    "RQP": ("X", "REQ-change planning", "TTO Coordination Agent · ToT pilot"),
    "RQD": ("X", "REQ plan endorsement", "Human gate · TTO lead / PMO peer"),
    "RQA": ("X", "REQ change approval", "Human gate · PMO"),
    "B1": ("B", "Validate request", "Solutions Orchestrator"),
    "B2": ("B", "Gateway retrieval", "System"),
    "B3": ("B", "Expertise Agent", "Agent"),
    "B4": ("B", "Escalation", "Human gate"),
    "B5": ("B", "Acknowledge", "Human gate"),
}


def _detail(refs: dict) -> str:
    for key in ("query_id", "record_id", "input_id", "escalation_id"):
        if refs.get(key):
            extra = refs.get("decision") or ""
            return f"{refs[key]} {extra}".strip()
    return ""


def classify(row: dict) -> list[tuple[str, str, str]]:
    """Return (step, status, detail) tuples for one audit row."""
    a, o = row["action"], row["outcome"]
    refs = row.get("refs") or {}
    d = _detail(refs)
    if a.startswith(("delivery_store.", "evidence_store.")):  # stores only audit denials
        return [("BND", DENIED, f"{row['actor']} blocked from {a.split('.')[0]}")]
    if a == "load_input":
        return [("A1", OK if o in ("accepted", "duplicate-ignored") else DENIED, d or row.get("reason", ""))]
    if a == "submit_msr":
        return [("A1", OK if o == "submitted" else DENIED, d or row.get("reason", ""))]
    if a == "tto_review":
        if o == "denied":
            return [("A2T", DENIED, d)]
        own = " (own MSR)" if refs.get("own_submission") else ""
        tto = f" for {refs['tto_id']}" if refs.get("tto_id") else ""
        tto += " (PMO peer)" if refs.get("review_basis") == "pmo-peer" else ""
        return [("A2T", OK if o == "approved" else ESCALATED, d + tto + own)]
    if a == "employee_review":
        if o != "recorded":
            return [("A2", DENIED, d)]
        return [("A2", ESCALATED if refs.get("decision") != "accepted" else OK, d)]
    if a in ("prepare_candidates", "assess_msr"):
        tag = refs.get("input_tag")
        return [("A3", OK if o == "completed" else DENIED, f"{d} {tag}".strip() if tag else d)]
    if a == "process_approval_event":
        decision = refs.get("decision", "")
        if o == "denied":
            return [("A4", DENIED, d)]
        if decision == "revoked":
            return [("A4", ESCALATED, d), ("CHG", ESCALATED, f"{refs.get('record_id', '')} revoked")]
        return [("A4", OK if decision == "approved" or o == "duplicate-ignored" else ESCALATED, d)]
    if a == "publish":
        return [("A5", OK if o == "published" else DENIED, d)]
    if a in ("revise_evidence", "correct_input", "withdraw_input", "invalidate_evidence"):
        return [("CHG", ESCALATED, f"{a.replace('_', ' ')}: {d}")]
    if a == "plan_req":
        status = DENIED if o == "denied" else OK if o == "awaiting-tto-endorsement" else ESCALATED
        return [("RQP", status, f"{refs.get('role_req_id', '')} {o}".strip())]
    if a in ("decide_req_plan", "pmo_decide_req_plan"):
        step, good = ("RQD", "endorsed") if a == "decide_req_plan" else ("RQA", "approved")
        status = DENIED if o == "denied" else OK if o == good else ESCALATED
        rank = f" plan {refs['plan_rank']}" if refs.get("plan_rank") else ""
        return [(step, status, f"{refs.get('role_req_id', '')} {o}{rank}".strip())]
    if a == "semantic_search":
        return [("B2", OK if o == "complete" else ESCALATED, f"semantic {refs.get('query_id', '')}".strip())]
    if a == "retrieve":
        return [("B2", OK if o == "ok" else DENIED, o)]
    if a == "workflow_b":
        if o == "denied":
            return [("B1", DENIED, row.get("reason", "")[:60])]
        if o == "failed_safe":
            step = "B2" if "retrieval" in (row.get("reason") or "") else "B3"
            return [("B1", OK, d), (step, DENIED, "failed safe")]
        return [("B1", OK, d), ("B3", OK if o == "awaiting_acknowledgement" else ESCALATED, d)]
    if a == "escalate":
        return [("B4", ESCALATED, f"{o} → {refs.get('route_to', '')}")]
    if a == "resolve_escalation":
        return [("B4", OK if o == "resolved" else DENIED, d)]
    if a == "acknowledge":
        return [("B5", OK if o == "complete" else (ESCALATED if o == "stale" else DENIED), o)]
    return []


@dataclass
class StepState:
    status: str = ""
    detail: str = ""
    count: int = 0
    seen: float = 0.0  # wall-clock time the monitor saw the latest event


@dataclass
class Person:
    status: str = ""
    activity: str = "waiting"
    count: int = 0
    seen: float = 0.0
    tasks: list = field(default_factory=list)  # REQ tasks from the person's latest assessed MSR
    flags: list = field(default_factory=list)  # e.g. a new task the employee confirmed
    tally: Counter = field(default_factory=Counter)  # decisions made, e.g. "approved"


# People Monitor cards: one per authority level being acted in (ROLE_BUNDLES keys), plus the agents.
ROLE_CARDS = [("employee", "Employee"), ("tto", "TTO lead"), ("pmo", "PMO reviewer"),
              ("operator", "Delivery operator"), ("solutions", "Solutions requester"),
              ("agent", "Agents · decision support")]
AGENT_NAMES = {"AGT-ROLE-EXECUTION": "Role Execution Agent", "AGT-SOLUTIONS-ORCHESTRATOR": "Solutions Orchestrator",
               "AGT-SOLUTIONS-EXPERTISE": "Solutions Expertise Agent", "AGT-TTO-COORDINATION": "TTO Coordination Agent"}


@dataclass
class RoleCard:
    current: str = ""  # the person (or agent) most recently acting, or selected, in this role
    status: str = ""
    activity: str = "No one acting yet"
    seen: float = 0.0
    count: int = 0
    recent: dict = field(default_factory=dict)  # user_id -> order of last activity in this role (higher = later)
    tallies: dict = field(default_factory=dict)  # user_id -> Counter of decisions made in this role


def subject_of(input_id: str) -> str | None:
    """SIM-MSR-202611-005 -> SIM-EMP-005 (dataset labeling); test inputs have no subject in their ID."""
    m = re.fullmatch(r"([A-Z]+)-MSR-\d{6}-(\d+)", input_id or "")
    return f"{m.group(1)}-EMP-{m.group(2)}" if m else None


_DEFAULT_ROLE = {"submit_msr": "employee", "employee_review": "employee", "correct_input": "employee",
                 "tto_review": "tto", "process_approval_event": "pmo", "publish": "pmo", "revise_evidence": "pmo",
                 "workflow_b": "solutions", "acknowledge": "solutions", "plan_req": "tto", "decide_req_plan": "tto",
                 "pmo_decide_req_plan": "pmo", "load_input": "operator", "prepare_candidates": "operator",
                 "withdraw_input": "operator"}


def activities(row: dict, hits: list, tasks_title) -> list[tuple[str, str, str, dict]]:
    """(user_id, status, activity text, extras) for the people an audit row concerns; extras["role"] is the authority
    level acted in: the recorded acting role, or the role the action implies."""
    a, o, refs, actor = row["action"], row["outcome"], row.get("refs") or {}, row["actor"]
    status = DENIED if any(h[1] == DENIED for h in hits) else ESCALATED if any(h[1] == ESCALATED for h in hits) \
        else OK
    iid, rid, dec = refs.get("input_id", ""), refs.get("record_id", ""), refs.get("decision", o)
    role = row.get("acting_role") or _DEFAULT_ROLE.get(a, "employee")
    if a == "tto_review" and refs.get("review_basis") == "pmo-peer":
        role = "pmo"

    def r(**kw):
        return {"role": role} | kw

    if actor.startswith(("AGT-", "SVC-")) and a not in ("workflow_b", "assess_msr", "prepare_candidates"):
        return []
    if status == DENIED:
        return [(actor, DENIED, f"Denied: {a.replace('_', ' ')} {iid or rid}".strip(), r())]
    if a == "submit_msr":
        extra = " with feedback responses" if refs.get("employee_feedback") else ""
        return [(actor, OK, f"Submitted {iid}{extra}", r())]
    if a == "employee_review":
        return [(actor, status, f"Reviewed own MSR {iid}: {dec}", r())]
    if a in ("assess_msr", "prepare_candidates"):
        who = subject_of(iid)
        tasks = [tasks_title(t) for t in refs.get("aligned_tasks", [])]
        out = [(who, OK, f"Work assessed by the Role Execution Agent ({refs.get('input_tag', '')})",
                {"role": "employee", "tasks": tasks})] if who else []
        if a == "prepare_candidates" and not actor.startswith("AGT-"):
            out.append((actor, OK, f"Ran the REQ assessment for {iid}", r()))
        return out
    if a == "tto_review":
        out = [(actor, status, f"TTO review {dec}: {iid}", r(tally=dec))]
        if subject_of(iid) and subject_of(iid) != actor:
            out.append((subject_of(iid), status, f"MSR {iid} {dec} at TTO review", {"role": "employee"}))
        return out
    if a == "process_approval_event":
        return [(actor, status, f"PMO {dec}: {rid}", r(tally=dec))]
    if a == "publish":
        return [(actor, OK, f"Published {rid} to Solutions", r(tally="published"))]
    if a == "workflow_b":  # the orchestrator records the run; the person is the requester
        who = refs.get("requester_id") or actor
        return [(who, status, f"Asked {refs.get('query_id', 'a coverage question')} ({o.replace('_', ' ')})",
                 {"role": "solutions", "tally": "queries"})]
    if a == "acknowledge":
        return [(actor, status, f"Acknowledged the explanation ({o})", r())]
    if a in ("plan_req", "decide_req_plan", "pmo_decide_req_plan"):
        return [(actor, status, f"REQ change {a.replace('_', ' ')}: {refs.get('role_req_id', '')} {o}", r())]
    if a in ("revise_evidence", "correct_input", "withdraw_input"):
        return [(actor, ESCALATED, f"{a.replace('_', ' ').capitalize()}: {iid or rid}", r())]
    if a == "load_input":
        return [(actor, status, f"Loaded {iid}", r(tally="loaded"))]
    return []


def agent_activity(row: dict, hits: list) -> tuple[str, str, str] | None:
    """(agent name, status, text) when an agent did the work recorded in this row."""
    a, o, refs = row["action"], row["outcome"], row.get("refs") or {}
    status = DENIED if any(h[1] == DENIED for h in hits) else ESCALATED if any(h[1] == ESCALATED for h in hits) \
        else OK
    if a in ("assess_msr", "prepare_candidates") and o in ("completed", "failed-safe"):
        return "Role Execution Agent", status, f"Assessed {refs.get('input_id', '')}: {refs.get('input_tag', o)}"
    if a == "workflow_b" and o not in ("denied",):
        return "Solutions Expertise Agent", status, f"Explained coverage for {refs.get('query_id', '')} ({o})"
    if a == "plan_req" and o != "denied":
        return "TTO Coordination Agent", status, f"Proposed REQ-change options for {refs.get('role_req_id', '')}"
    return None


@dataclass
class MonitorState:
    last_seq: int = 0
    steps: dict = field(default_factory=lambda: {k: StepState() for k in STEPS})
    feed: list = field(default_factory=list)
    active_correlation: str = ""
    people: dict = field(default_factory=dict)  # user_id -> Person (tasks and flags, across roles)
    roles: dict = field(default_factory=lambda: {k: RoleCard() for k, _ in ROLE_CARDS})
    tick: int = 0
    selections: int = 0  # 'acting' events received from the review app (Presentation mode)
    feed_pos: int = 0
    dialogue: dict = field(default_factory=dict)
    people_log: list = field(default_factory=list)  # latest first: who did what, in which role

    def _person(self, user_id: str, status: str, text: str, now: float, extras: dict, ts: str = "") -> None:
        role = extras.get("role", "employee")
        self.tick += 1
        self.people_log.insert(0, {"ts": ts, "user_id": user_id, "role": role, "status": status, "text": text,
                                   "seen": now})
        del self.people_log[60:]
        p = self.people.setdefault(user_id, Person())
        p.status, p.activity, p.seen = status, text, now
        p.count += 1
        if "tasks" in extras:
            p.tasks = extras["tasks"]
        if extras.get("flags"):
            p.flags = extras["flags"]
        card = self.roles.setdefault(role, RoleCard())
        card.current, card.status, card.activity, card.seen = user_id, status, text, now
        card.count += 1
        card.recent[user_id] = self.tick
        if extras.get("tally"):
            card.tallies.setdefault(user_id, Counter())[extras["tally"]] += 1

    def select(self, user_id: str, role: str, now: float, ts: str = "") -> None:
        """Someone was selected in the review app to act in this role (presentation feed)."""
        self.selections += 1
        if role not in self.roles:
            return
        label = dict(ROLE_CARDS)[role]
        self._person(user_id, "", f"Now acting as {label} (selected in the review app)", now, {"role": role}, ts)
        self.roles[role].count -= 1  # a selection is not an action

    def apply(self, rows: list[dict], now: float, tasks_title=lambda t: t) -> None:
        for row in rows:
            self.last_seq = max(self.last_seq, row["seq"])
            hits = classify(row)
            for step, status, detail in hits:
                st = self.steps[step]
                st.status, st.detail, st.seen = status, detail, now
                st.count += 1
            for who, status, text, extras in activities(row, hits, tasks_title):
                if who:
                    self._person(who, status, text, now, extras, row.get("ts", ""))
            agent = agent_activity(row, hits)
            if agent:
                name, status, text = agent
                self._person(name, status, text, now, {"role": "agent"}, row.get("ts", ""))
            if hits:
                self.active_correlation = row.get("correlation_id") or self.active_correlation
                self.feed.insert(0, row)
        del self.feed[40:]

    def apply_feed(self, rows: list[dict], now: float) -> None:
        """The presentation feed: the employee's unrecorded feedback dialogue (check, edit, responses, submit)."""
        for r in rows:
            self.feed_pos = max(self.feed_pos, r["line"])
            who, kind, d = r["actor"], r["kind"], self.dialogue
            ts = r.get("ts", "")
            if kind == "acting":
                self.select(who, r.get("role", ""), now, ts)
            elif kind == "check":
                if d.get("actor") != who or d.get("stage") == "Submitted":
                    self.dialogue = d = {"actor": who, "edits": [], "responses": [], "result": None}
                d.update(stage="Feedback shown" if r["draft"] == 1 else "Re-checked after edits", month=r["month"],
                         draft=r["draft"], engine=r.get("engine"), role_req_id=r.get("role_req_id"),
                         statements=r["statements"], responses=[])
                new = sum(s["tag"] != "aligned" for s in r["statements"])
                text = f"Checking a draft MSR against their REQ ({len(r['statements'])} statements, {new} potential new)"
                st = self.steps["A0"]
                st.status, st.detail, st.seen = OK, f"{who} draft {r['draft']}", now
                st.count += 1
                self._person(who, OK, text, now, {}, ts)
            elif kind == "edit":
                d.setdefault("edits", []).append(r)
                d["stage"] = "Editing"
                self._person(who, ESCALATED, f"Editing statement {r['index']} after feedback", now, {}, ts)
            elif kind == "responses":
                d["responses"], d["stage"] = r["responses"], "Responses given"
                self._person(who, OK, "Responding to the feedback for each statement", now, {}, ts)
            elif kind == "submitted":
                d["result"], d["stage"] = r, "Submitted"
                flags = [f"New task flagged: {t}" for t in r.get("recommended", [])]
                self._person(who, OK, f"Submitted {r['input_id']} after the feedback dialogue", now, {"flags": flags},
                             ts)
            elif kind == "submit_denied":
                d["stage"] = "Submit refused"
                self._person(who, DENIED, f"Submit refused: {r.get('reason', '')}", now, {}, ts)


# ===================================================================== rendering (HTML used by the page)

PALETTE = {  # status -> (fill, accent)
    OK: ("#E8F5EE", "#2E7D4F"),
    DENIED: ("#FDECEA", "#B3261E"),
    ESCALATED: ("#FFF1E6", "#DF6D35"),
    "": ("#F1F5F8", "#9DB0BD"),
}
HOT = "#F2C230"

CSS = """
<style>
.lane {border:2px solid #C9D6DF; border-radius:14px; padding:14px 16px 16px; margin-bottom:14px; background:#FFFFFF;
       transition: box-shadow .3s, border-color .3s;}
.lane.hot {border-color:#F2C230; box-shadow:0 0 0 3px #F2C23055, 0 0 24px #F2C23088;}
.lane-head {display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;}
.lane-title {font-size:1.15rem; font-weight:700; color:#0E2841;}
.lane-sub {font-size:.8rem; color:#196784; font-weight:600; letter-spacing:.04em;}
.badge {font-size:.75rem; font-weight:700; padding:3px 10px; border-radius:999px; background:#F2C230; color:#0E2841;}
.badge.idle {background:#E3EAEF; color:#5C6F7B;}
.flow {display:flex; align-items:stretch; gap:6px;}
.arrow {align-self:center; color:#9DB0BD; font-size:1.4rem; font-weight:700;}
.node {flex:1; min-width:0; border-radius:10px; padding:10px 12px; transition: all .3s;}
.node .kind {font-size:.68rem; font-weight:700; letter-spacing:.05em;}
.node .label {font-size:1rem; font-weight:700; color:#0E2841; margin:2px 0 4px;}
.node .meta {font-size:.75rem; color:#354D59; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
.node.hot {animation: pulse 1s ease-in-out infinite alternate;}
@keyframes pulse {from {transform:scale(1.0);} to {transform:scale(1.05);}}
.legend span {display:inline-block; margin-right:14px; font-size:.8rem; color:#354D59;}
.legend i {display:inline-block; width:12px; height:12px; border-radius:3px; margin-right:5px; vertical-align:-1px;}
.mid {display:flex; gap:14px; margin-bottom:14px;}
.mid .node {flex:1;}
.people {display:flex; flex-wrap:wrap; gap:10px; margin-bottom:14px;}
.person {flex:1 1 230px; min-width:0; border-radius:12px; padding:10px 12px; background:#FFFFFF;
         border:2px solid #C9D6DF; transition: all .3s;}
.person.hot {border-color:#F2C230; box-shadow:0 0 0 4px #F2C23055, 0 0 22px #F2C230;
             animation: pulse 1s ease-in-out infinite alternate;}
.person .top {display:flex; gap:8px; align-items:center;}
.avatar {width:34px; height:34px; border-radius:50%; background:#0E2841; color:#FFFFFF; font-weight:700;
         display:flex; align-items:center; justify-content:center; font-size:.85rem; flex:none;}
.person .role {font-weight:800; color:#0E2841; font-size:1.05rem; letter-spacing:.01em;}
.person .who {margin:6px 0 2px; padding:4px 8px; border-radius:8px; background:#F1F5F8;}
.person .name {font-weight:700; color:#0E2841; font-size:.95rem; line-height:1.1;}
.person .lead {font-size:.74rem; color:#0E2841; margin-top:3px; padding-top:3px; border-top:1px solid #D5E0E6;}
.person .title {font-size:.7rem; color:#5C6F7B; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
.chip {display:inline-block; font-size:.66rem; font-weight:700; letter-spacing:.04em; color:#196784;
       background:#E6EFF3; border-radius:999px; padding:1px 8px; margin:6px 0 4px;}
.person .act {font-size:.8rem; font-weight:600; margin:2px 0 4px;}
.person ul {margin:0 0 0 14px; padding:0; font-size:.72rem; color:#354D59;}
.person .tally {font-size:.7rem; color:#5C6F7B;}
.dialogue {border:2px solid #C9D6DF; border-radius:14px; padding:12px 16px; margin-bottom:14px; background:#FFFFFF;}
.dialogue.hot {border-color:#F2C230; box-shadow:0 0 0 3px #F2C23055, 0 0 24px #F2C23088;}
.stepper {display:flex; gap:6px; margin:6px 0 10px;}
.stepper span {flex:1; text-align:center; font-size:.72rem; font-weight:700; padding:4px; border-radius:8px;
               background:#E3EAEF; color:#5C6F7B;}
.stepper span.done {background:#E8F5EE; color:#2E7D4F;}
.stepper span.now {background:#FFF6E0; color:#0E2841; box-shadow:0 0 0 2px #F2C230;}
.dialogue table {width:100%; border-collapse:collapse; font-size:.8rem;}
.dialogue td, .dialogue th {border-bottom:1px solid #E3EAEF; padding:4px 6px; text-align:left; vertical-align:top;}
.dialogue th {color:#196784; font-size:.72rem; letter-spacing:.04em;}
.tag-new {color:#DF6D35; font-weight:700;} .tag-al {color:#2E7D4F; font-weight:700;}
</style>
"""

LEGEND = (
    '<div class="legend">'
    f'<span><i style="background:{HOT}"></i>Executing now</span>'
    f'<span><i style="background:{PALETTE[OK][1]}"></i>Completed</span>'
    f'<span><i style="background:{PALETTE[ESCALATED][1]}"></i>Routed to a human / changed</span>'
    f'<span><i style="background:{PALETTE[DENIED][1]}"></i>Denied or blocked</span>'
    f'<span><i style="background:{PALETTE[""][1]}"></i>Not yet run</span></div>')


def render_node(step: str, s: StepState, hot: bool) -> str:
    _, label, kind = STEPS[step]
    fill, accent = PALETTE[s.status]
    style = f"background:{fill}; border:2px solid {accent};"
    if hot:
        style = f"background:#FFF6E0; border:2px solid {HOT}; box-shadow:0 0 0 4px {HOT}55, 0 0 22px {HOT};"
    meta = f"×{s.count} · {html.escape(s.detail)}" if s.count else "not yet run"
    return (f'<div class="node{" hot" if hot else ""}" style="{style}">'
            f'<div class="kind" style="color:{HOT if hot else accent}">{html.escape(kind.upper())}</div>'
            f'<div class="label">{html.escape(label)}</div><div class="meta">{meta}</div></div>')


def render_lane(title: str, sub: str, steps: list[str], state: MonitorState, now: float, hold: float) -> str:
    hot = {k: now - state.steps[k].seen < hold for k in steps}
    running = any(hot.values())
    parts = []
    for i, k in enumerate(steps):
        if i:
            parts.append('<div class="arrow">→</div>')
        parts.append(render_node(k, state.steps[k], hot[k]))
    badge = '<span class="badge">EXECUTING</span>' if running else '<span class="badge idle">IDLE</span>'
    return (f'<div class="lane{" hot" if running else ""}"><div class="lane-head"><div>'
            f'<div class="lane-title">{html.escape(title)}</div><div class="lane-sub">{html.escape(sub)}</div>'
            f'</div>{badge}</div><div class="flow">{"".join(parts)}</div></div>')


ROLE_ICONS = {"employee": "E", "tto": "T", "pmo": "P", "operator": "O", "solutions": "S", "agent": "A"}


def _name(ref: dict, uid: str) -> tuple[str, str]:
    return ref.get("users", {}).get(uid, (uid, ""))


def render_people(state: MonitorState, now: float, hold: float, ref: dict) -> str:
    """One card per authority level: who is acting in it now (latest action, or selected in the review app), what
    they are doing, their tasks or decisions in that role, and anyone else recently active in the same role."""
    cards = []
    for key, label in ROLE_CARDS:
        c = state.roles.get(key, RoleCard())
        hot = bool(c.current) and now - c.seen < hold
        color = HOT if hot else PALETTE[c.status][1]
        bg = "#196784" if key == "agent" else "#0E2841"
        who = ""
        items = []
        if c.current:
            name, title = _name(ref, c.current)
            who = (f'<div class="name">{html.escape(name)}</div>'
                   f'<div class="title">{html.escape(c.current if name != c.current else "")}'
                   f'{" · " if name != c.current and title else ""}{html.escape(title)}</div>')
            p = state.people.get(c.current, Person())
            if key == "employee":
                rv = ref.get("reviewer", {}).get(c.current)
                if rv and rv["lead"]:
                    lead = _name(ref, rv["lead"])[0]
                    who += (f'<div class="lead"><b>TTO lead:</b> {html.escape(lead)} '
                            f'({html.escape(rv["lead"])}, {html.escape(rv["tto_id"])})</div>')
                elif rv and rv["pmo_id"]:
                    who += (f'<div class="lead"><b>TTO-stage review:</b> a {html.escape(rv["pmo_id"])} teammate '
                            f'(contract PMO staff have no TTO lead)</div>')
                elif rv:
                    who += f'<div class="lead"><b>TTO lead:</b> none assigned for {html.escape(rv["tto_id"])}</div>'
                items = [f"<li>{html.escape(t)}</li>" for t in p.flags[:2]]
                if p.tasks:
                    items.append('<div class="tally">Tasks in latest MSR:</div>')
                    items += [f"<li>{html.escape(t)}</li>" for t in p.tasks[:4 - min(len(p.flags), 2)]]
        tally = " · ".join(f"{k} {v}" for k, v in c.tallies.get(c.current, Counter()).most_common(3))
        others = [u for u, _ in sorted(c.recent.items(), key=lambda kv: -kv[1]) if u != c.current]
        also = ", ".join(_name(ref, u)[0] for u in others[:3]) + (f" +{len(others) - 3}" if len(others) > 3 else "")
        cards.append(
            f'<div class="person{" hot" if hot else ""}"><div class="top"><div class="avatar" style="background:{bg}">'
            f'{ROLE_ICONS[key]}</div><div style="min-width:0"><div class="role">{html.escape(label)}</div>'
            f'</div></div>'
            + (f'<div class="who">{who}</div>' if who else "")
            + f'<div class="act" style="color:{color}">{html.escape(c.activity)}</div>'
            + (f"<ul>{''.join(items)}</ul>" if items else "")
            + (f'<div class="tally">In this role: {html.escape(tally)}</div>' if tally else "")
            + (f'<div class="tally">Also active in this role: {html.escape(also)}</div>' if also else "") + "</div>")
    return f'<div class="lane-title" style="margin-bottom:6px">Who is acting, by authority level</div>' \
           f'<div class="people">{"".join(cards)}</div>'


def render_dialogue(state: MonitorState, now: float, hold: float, ref: dict) -> str:
    """The latest employee feedback dialogue, from the presentation feed (not recorded by the system)."""
    d = state.dialogue
    if not d.get("statements"):
        return ('<div class="dialogue"><div class="lane-title">Employee dialogue · before submission</div>'
                '<div class="lane-sub">Shows here when an employee checks a draft MSR in the demo (presentation mode). '
                'The check itself is never recorded.</div></div>')
    name = ref.get("users", {}).get(d["actor"], (d["actor"], ""))[0]
    hot = now - state.people.get(d["actor"], Person()).seen < hold
    stages = ["Feedback shown", "Editing", "Re-checked after edits", "Responses given", "Submitted"]
    cur = stages.index(d["stage"]) if d["stage"] in stages else 0
    if not d.get("edits") and cur > 0:  # no edit round: skip those two stages in the stepper
        stages = ["Feedback shown", "Responses given", "Submitted"]
        cur = stages.index(d["stage"]) if d["stage"] in stages else 0
    stepper = "".join(f'<span class="{"done" if i < cur else "now" if i == cur else ""}">{html.escape(s)}</span>'
                      for i, s in enumerate(stages))
    resp = {r["index"]: r for r in d.get("responses", [])}
    edited = {e["index"] for e in d.get("edits", [])}
    rows = []
    for s in d["statements"]:
        if s["tag"] == "aligned":
            check = f'<span class="tag-al">Aligned</span> → {html.escape("; ".join(s["aligned"]))}'
        else:
            check = '<span class="tag-new">Potential new task</span>'
            if s.get("suggested_task"):
                check += f"<br>Recommended: {html.escape(s['suggested_task'])}"
            if s.get("similar_elsewhere"):
                check += f"<br>Similar elsewhere: {html.escape('; '.join(s['similar_elsewhere']))}"
        r = resp.get(s["index"])
        answer = html.escape(r["response"] + (f' — "{r["note"]}"' if r.get("note") else "")) if r else "…"
        mark = " ✎" if s["index"] in edited else ""
        rows.append(f"<tr><td>{s['index']}{mark}</td><td>{html.escape(s['text'])}</td><td>{check}</td>"
                    f"<td>{answer}</td></tr>")
    res = d.get("result")
    tail = ""
    if res:
        rec = "; ".join(res.get("recommended", [])) or "none"
        tail = (f'<div class="lane-sub" style="margin-top:8px">Submitted {html.escape(res["input_id"])} → REQ '
                f'assessment: {html.escape(res.get("input_tag") or "")} · recommended new task(s) for the TTO lead: '
                f'{html.escape(rec)}' + (" · <b>needs higher-level review</b>" if res.get("needs_review") else "")
                + (f' · {res["differed"]} statement(s) assessed differently from the check' if res.get("differed")
                   else "") + "</div>")
    engine = {"local": "free local check", "hybrid": "local check + small model", "model": "agent check"}.get(
        d.get("engine"), d.get("engine") or "")
    return (f'<div class="dialogue{" hot" if hot else ""}"><div class="lane-head"><div>'
            f'<div class="lane-title">Employee dialogue · {html.escape(name)} · {html.escape(d.get("month", ""))}'
            f'</div><div class="lane-sub">BEFORE SUBMISSION · {html.escape(engine.upper())} against '
            f'{html.escape(d.get("role_req_id") or "")} · NOTHING RECORDED UNTIL SUBMIT</div></div></div>'
            f'<div class="stepper">{stepper}</div><table><tr><th>#</th><th>STATEMENT</th><th>FEEDBACK</th>'
            f'<th>EMPLOYEE RESPONSE</th></tr>{"".join(rows)}</table>{tail}</div>')


def render_board(state: MonitorState, now: float, hold: float) -> list[str]:
    """The Workflow Monitor: Workflow A, the shared boundary/change row, Workflow B (audit trail only)."""
    mid = "".join(render_node(k, state.steps[k], now - state.steps[k].seen < hold)
                  for k in ("BND", "CHG", "RQP", "RQD", "RQA"))
    return [
        render_lane("Workflow A · Publish filtered evidence", "DELIVERY DOMAIN · raw MSRs stay here",
                    ["A1", "A2", "A3", "A2T", "A4", "A5"], state, now, hold),
        f'<div class="mid">{mid}</div>',
        render_lane("Workflow B · Retrieve and explain", "SOLUTIONS DOMAIN · approved filtered records only",
                    ["B1", "B2", "B3", "B4", "B5"], state, now, hold),
    ]


def render_people_log(state: MonitorState, now: float, hold: float, ref: dict, n: int = 14) -> str:
    """Who did what, latest first: the people in the demo, and everyone else in grey."""
    rows = []
    for e in state.people_log[:n]:
        name = ref.get("users", {}).get(e["user_id"], (e["user_id"], ""))[0]
        color = HOT if now - e["seen"] < hold else PALETTE[e["status"]][1]
        role = dict(ROLE_CARDS).get(e["role"], e["role"])
        rows.append(f'<tr><td style="white-space:nowrap">{html.escape(e["ts"][11:16] if e["ts"] else "")}</td>'
                    f'<td style="white-space:nowrap; color:#196784; font-weight:700">{html.escape(role)}</td>'
                    f'<td style="white-space:nowrap; font-weight:700">{html.escape(name)}</td>'
                    f'<td><span style="color:{color}">●</span> {html.escape(e["text"])}</td></tr>')
    body = "".join(rows) or '<tr><td colspan="4">No actions yet. Run the demo or act in the review app.</td></tr>'
    return (f'<div class="dialogue"><div class="lane-title">Actions, latest first</div><div class="lane-sub">'
            f'DEMO CLOCK · ROLE · PERSON · ACTION · {state.steps["A0"].count} FEEDBACK CHECK(S) (NOT RECORDED)</div>'
            f'<table><tr><th>TIME</th><th>ROLE</th><th>PERSON</th><th>ACTION</th></tr>{body}</table></div>')


def render_people_board(state: MonitorState, now: float, hold: float, ref: dict | None = None) -> list[str]:
    """The People Monitor: the people in the demo, the employee dialogue, and the action log."""
    ref = ref or {}
    return [render_people(state, now, hold, ref), render_dialogue(state, now, hold, ref),
            render_people_log(state, now, hold, ref)]


def load_reference(root: Path) -> dict:
    """Names, titles, and REQ task titles for the board, read from the dataset's reference registry (read-only)."""
    cfg = json.loads((root / "config" / "prototype_config.json").read_text(encoding="utf-8"))
    dataset = os.environ.get("STRATACASTER_DATASET") or cfg["dataset"]
    try:
        reg = json.loads((root / cfg["datasets"][dataset]["registry"]).read_text(encoding="utf-8"))
    except (OSError, KeyError, json.JSONDecodeError):
        return {}
    users = {}
    for u in reg.get("users", []):
        name, _, title = u.get("display", u["user_id"]).partition(" — ")
        users[u["user_id"]] = (name, re.sub(r"\s*\([^)]*\)$", "", title))
    def rows(key):
        raw = reg.get(key, [])
        return [x for x in (raw.values() if isinstance(raw, dict) else raw) if isinstance(x, dict)]

    tasks = {t["req_task_id"]: t.get("title", t["req_task_id"]) for t in rows("req_tasks")}
    ttos = {t["tto_id"]: t for t in rows("ttos")}
    pmo_of = {c["contract_id"]: c.get("pmo_id", "") for c in rows("contracts")}
    leads = {}  # tto_id -> TTO lead user_id (the lead with authority over that TTO)
    for u in reg.get("users", []):
        if "tto_lead" in u.get("roles", []):
            for t in u.get("tto_authority", []):
                leads.setdefault(t, u["user_id"])
    reviewer = {}  # subject_id -> who reviews their MSRs at the TTO stage
    for sj in rows("subjects"):
        sid, tto = sj.get("subject_id"), sj.get("tto_id", "")
        if not sid:
            continue
        if ttos.get(tto, {}).get("pmo_review"):
            reviewer[sid] = {"tto_id": tto, "lead": None, "pmo_id": pmo_of.get(sj.get("contract_id", ""), "")}
        else:
            reviewer[sid] = {"tto_id": tto, "lead": leads.get(tto), "pmo_id": None}
    return {"users": users, "tasks": tasks, "reviewer": reviewer}


def feed_path(root: Path) -> Path:
    return audit_path(root).parent / "presentation_feed.jsonl"


def audit_path(root: Path) -> Path:
    cfg = json.loads((root / "config" / "prototype_config.json").read_text(encoding="utf-8"))
    var_dir = os.environ.get("STRATACASTER_VAR_DIR") or str(root / cfg["paths"]["var_dir"])
    return Path(var_dir) / "audit.db"


def read_events(db: Path, after_seq: int) -> tuple[list[dict], int]:
    """Returns (new rows, current max seq). Empty when the audit log does not exist yet."""
    if not db.exists():
        return [], 0
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=1)
    try:
        max_seq = con.execute("SELECT COALESCE(MAX(seq), 0) FROM audit").fetchone()[0]
        cur = con.execute("SELECT seq, ts, correlation_id, actor, domain, action, outcome, reason, refs, meta "
                          "FROM audit WHERE seq > ? ORDER BY seq", (after_seq,))
        cols = [c[0] for c in cur.description]
        rows = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["refs"] = json.loads(d["refs"] or "{}")
            role = json.loads(d.pop("meta") or "{}").get("acting_role")
            d["actor_label"] = f"{d['actor']} (as {role})" if role else d["actor"]
            d["acting_role"] = role
            rows.append(d)
        return rows, max_seq
    except sqlite3.Error:
        return [], 0
    finally:
        con.close()
