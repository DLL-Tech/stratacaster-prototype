"""Scripted demonstration on the roster dataset (bounded slice: SIM-FC-001, October 2026, plus test cases).

Used by scripts/run_demo.py and by the roster tests. Returns a summary dict of what actually happened.
"""
from __future__ import annotations

import time
from collections import Counter

from .environment import Environment
from .stores import PermissionDenied

SOLUTIONS_LEAD, GROWTH_USER = "SIM-EMP-210", "SIM-EMP-221"
QUERY_TTO = "SIM-TTO-1-01"
# Leila (WR-001, SIM-TTO-1-01): statement 1 is in her REQ; 2 is not but matches SIM-TTO-1-02 work (collaboration
# opportunity); 3 is in her REQ and also matches SIM-TTO-1-03 work (similar work).
CROSS_SUBMITTER = "SIM-EMP-006"
CROSS_BULLETS = ["I updated the scan settings for the assigned systems.",
                 "I retested the claimed fix after the changes and compared notes with the defenders.",
                 "I added the newly visible addresses to the system list and repeated the checks."]

# The employee feedback dialogue (PR-30), shown when the demo runs with dialogue=True. Rowan's draft has a vague
# statement he rewords after the check, and a mentoring statement that the free check matches to scan planning; he
# answers "Not what I intended", and the assessment at submission disagrees with the check, which the TTO lead sees.
# Responses are chosen by the tag the check shows, so the script works with any check engine.
ACCURATE_OR_NEW = {"aligned": {"response": "accurate"}, "potential-new-task-requirement": {"response": "confirm_new"}}
DIALOGUES = [
    {"who": "SIM-EMP-005", "month": "2026-11",
     "draft": ["I reran checks on the systems that were missed and added them to the patching queue.",
               "I helped the team with the systems list.",
               "I mentored two new analysts and wrote a how-to guide for running the weekly scans."],
     "edits": {2: "I added the newly visible addresses to the system list and confirmed they were scanned."},
     "responses": {3: {"aligned": {"response": "not_intended",
                                   "note": "This was mentoring and training material, not scan planning."},
                       "potential-new-task-requirement": {"response": "confirm_new"}}}},
    {"who": CROSS_SUBMITTER, "month": "2026-11", "draft": CROSS_BULLETS, "edits": {}, "responses": {}},
]


def submission_dialogue(env: Environment, script: dict, say=print, pause=lambda: None):
    """One employee's submission as a dialogue: check, read the feedback, edit and re-check, respond, submit. The
    check is not recorded; the presentation feed (when enabled) mirrors each step to the Workflow Monitor."""
    reg, feed = env.registry, env.presentation
    who, month, bullets = script["who"], script["month"], list(script["draft"])
    me = env.principal(who, "employee")
    name = reg.subjects.get(who, {}).get("name", who)

    def check(draft_no):
        pv = env.delivery.preview_msr(me, month, bullets)
        if not pv.ok:
            say(f"    check refused: {pv.reason}")
            return None
        feed.check(who, env.clock.now_str(), month, pv.data, reg, draft=draft_no)
        for r in pv.data["statements"]:
            if r["tag"] == "aligned":
                what = "aligned → " + "; ".join(f"{a['req_task_id']} {a['title']}" for a in r["aligned"])
            else:
                what = "potential new task"
                if r.get("suggested_task"):
                    what += f"; recommended: {r['suggested_task']['title']}"
                if r.get("similar_elsewhere"):
                    what += "; similar work elsewhere: " + ", ".join(
                        f"{e['req_task_id']} ({e['tto_id']})" for e in r["similar_elsewhere"])
            say(f"      {r['index']}. \"{r['text']}\"  [{what}]")
        pause()
        return pv

    say(f"  {name} ({who}) drafts a {month} MSR and checks it against their REQ ({env.feedback_mode} check; "
        "nothing is recorded):")
    pv = check(1)
    if pv is None:
        return None
    if script["edits"]:
        for idx, text in script["edits"].items():
            say(f"    {name} edits statement {idx}: \"{bullets[idx - 1]}\" → \"{text}\"")
            feed.edit(who, env.clock.now_str(), idx, bullets[idx - 1], text)
            bullets[idx - 1] = text
        say("    …and checks again:")
        pv = check(2)
        if pv is None:
            return None
    responses = {}
    for r in pv.data["statements"]:
        choice = script["responses"].get(r["index"], ACCURATE_OR_NEW)[r["tag"]]
        responses[r["index"]] = dict(choice)
    feed.responses(who, env.clock.now_str(), responses)
    say("    Responses: " + "; ".join(f"{i}: {r['response']}" + (f" (\"{r['note']}\")" if r.get("note") else "")
                                     for i, r in responses.items()))
    pause()
    out = env.delivery.submit_msr(me, month, bullets, correlation_id=f"SIM-WF-A-SUBMIT-{who}",
                                  preview_id=pv.data["preview_id"], responses=responses)
    feed.submitted(who, env.clock.now_str(), out)
    if not out.ok:
        say(f"    [NO ] submit: {out.reason}")
        return out
    a = out.data["assessment"]["assessment"]
    say(f"    Submitted {out.data['input_id']} → REQ assessment by the Role Execution Agent: {a['input_tag']}")
    stand_in = " (placeholder wording from the offline stand-in; live Claude writes the task)" \
        if env.llm.mode == "mock" else ""
    for s in a["statements"]:
        if s.get("suggested_task"):
            say(f"      recommended new task for the TTO lead: {s['suggested_task']['title']}{stand_in}")
    say(f"    For the TTO lead: {env.delivery._feedback_text(a)}")
    pause()
    return out


def run_roster_demo(env: Environment, pace: float = 0.0, say=print, dialogue: bool = False) -> dict:
    """dialogue=True adds new MSR submissions through the employee feedback dialogue (step 2a) and mirrors it to the
    Workflow Monitor; it adds records, so the evaluation and the count-based tests run without it."""
    reg, times = env.registry, env.registry.demo["times"]
    step = pace / 10 if pace else 0.0
    summary: dict = {}

    def pause(t=pace):
        if t:
            time.sleep(t)

    def ok(flag):
        return "OK " if flag else "NO "

    say(f"Dataset: {env.dataset}  ·  {env.versions()['reference_fixture']}  ·  model {env.llm.mode}")
    say(f"Clock: {env.clock.now_str()}\n")

    # 1 ---------------------------------------------------------------- load
    say("1. Load Delivery MSRs (SIM-FC-001, October) and the workbook's test cases")
    cases = env.msr_cases()
    loaded, rejected = [], []
    for case in cases:
        op = reg.operator_for(case["input"]["delivery_scope_id"])
        out = env.delivery.load_input(env.principal(op), case["input"],
                                      correlation_id=f"SIM-LOAD-{case['case_id']}")
        (loaded if out.ok else rejected).append((case, out))
        if case["category"] != "roster-msr" or not out.ok:
            say(f"  [{ok(out.ok)}] {case['case_id']} ({case['category']}): {out.reason} "
                f"{out.data.get('errors') or ''}")
        pause(step)
    n_roster = sum(c["category"] == "roster-msr" for c, _ in loaded)
    say(f"  Loaded {len(loaded)} inputs ({n_roster} roster MSRs); rejected {len(rejected)}")
    summary["loaded"] = [c["case_id"] for c, _ in loaded]
    summary["rejected"] = {c["case_id"]: o.reason for c, o in rejected}
    pause()

    # 2 ---------------------------------------------------------------- employee submission + REQ assessment
    say("\n2. Each employee submits (accepts) their own MSR; the Role Execution Agent assesses it against the "
        "employee's work-role REQ")
    decisions, tags, cw_all = Counter(), Counter(), Counter()
    prepared, gaps, created, blocked, denied_prep = 0, 0, [], {}, {}

    def collect(case, a):
        nonlocal prepared, gaps
        prepared += 1
        gaps += len(a["gaps"])
        tags[a["assessment"]["input_tag"]] += 1
        cw_all.update((a["assessment"].get("contract_wide") or {}).get("counts") or {})
        for c in a["created"]:
            created.append((c["record_id"], case))
            if c["blocking_exceptions"]:
                blocked[c["record_id"]] = (case["case_id"], c["blocking_exceptions"])

    for case, _ in loaded:
        rv = case["scripted_review"]
        if not rv:
            decisions["left pending"] += 1
            continue
        env.clock.set(rv["at"])
        out = env.delivery.employee_review(env.principal(rv["reviewer_id"]), case["input"]["input_id"], 1,
                                           rv["decision"], correlation_id=f"SIM-WF-A-{case['case_id']}")
        decisions[rv["decision"] if out.ok else "denied"] += 1
        if out.ok and out.data.get("assessment"):
            collect(case, out.data["assessment"])
        pause(step)
    say(f"  {dict(decisions)}")
    # Inputs the employee did not accept are never assessed; an operator re-run is denied too.
    env.clock.set(times["prepare"])
    for case, _ in loaded:
        if case["scripted_review"] and case["scripted_review"]["decision"] == "accepted":
            continue
        op = env.principal(reg.operator_for(case["input"]["delivery_scope_id"],
                                            exclude=case["input"].get("subject_id")))
        out = env.delivery.prepare_candidates(op, case["input"]["input_id"])
        if out.ok:
            collect(case, out.data)
        else:
            denied_prep[case["case_id"]] = out.reason
    if dialogue and reg.assess_on_submission:
        env.presentation.enabled = True  # a demonstration: mirror the dialogue to the Workflow Monitor
        say("\n2a. New MSR submissions with the employee feedback dialogue")
        env.clock.set(times["prepare"])
        summary["dialogues"] = {}
        for script in DIALOGUES[:-1]:  # Leila's submission follows below, also through the dialogue
            out = submission_dialogue(env, script, say, pause)
            if out is not None and out.ok:
                emp = reg.subjects[script["who"]]
                case = {"case_id": out.data["input_id"], "category": "submitted",
                        "scripted_review": {"decision": "accepted"},
                        "input": {"input_id": out.data["input_id"], "subject_id": script["who"],
                                  "tto_id": emp["tto_id"], "contract_id": emp["contract_id"],
                                  "delivery_scope_id": emp["delivery_scope_id"]}}
                loaded.append((case, out))
                collect(case, out.data["assessment"])
                summary["dialogues"][script["who"]] = {"input_id": out.data["input_id"]} | (
                    out.data["assessment"]["assessment"].get("employee_feedback") or {})
    if reg.contract_wide_analytic:  # a new submission whose statements also describe work done in other TTOs
        emp = reg.subjects[CROSS_SUBMITTER]
        if dialogue and reg.assess_on_submission:
            out = submission_dialogue(env, DIALOGUES[-1], say, pause)
            if out is not None and out.ok:
                summary["dialogues"][CROSS_SUBMITTER] = {"input_id": out.data["input_id"]} | (
                    out.data["assessment"]["assessment"].get("employee_feedback") or {})
        else:
            out = env.delivery.submit_msr(env.principal(CROSS_SUBMITTER, "employee"), "2026-11", CROSS_BULLETS,
                                          correlation_id="SIM-WF-A-SUBMIT-CROSS")
        if out is not None and out.ok:
            case = {"case_id": out.data["input_id"], "category": "submitted",
                    "scripted_review": {"decision": "accepted"},
                    "input": {"input_id": out.data["input_id"], "subject_id": CROSS_SUBMITTER,
                              "tto_id": emp["tto_id"], "contract_id": emp["contract_id"],
                              "delivery_scope_id": emp["delivery_scope_id"]}}
            loaded.append((case, out))
            collect(case, out.data["assessment"])
            cw = out.data["assessment"]["assessment"]["contract_wide"]
            say(f"  {CROSS_SUBMITTER} submits {out.data['input_id']}; contract-wide analytic: {cw['counts']}")
            for row in cw["statements"]:
                say(f"    {row['statement_id']}: {row['signal']} → "
                    f"{[(x['req_task_id'], x['tto_id']) for x in row['elsewhere']]}")
            summary["contract_wide"] = cw["counts"]
    say(f"  Assessed {prepared} inputs → {len(created)} task alignments (draft records); tags {dict(tags)}; "
        f"{gaps} statements tagged as potential new task requirements")
    for cid, reason in denied_prep.items():
        say(f"  [NO ] {cid}: {reason}")
    for rid, (cid, exc) in blocked.items():
        say(f"  [blocked] {rid} from {cid}: {exc}")
    summary.update(prepared=prepared, records=len(created), gaps=gaps, blocked=blocked, denied_prep=denied_prep,
                   tags=dict(tags), contract_wide_all=dict(cw_all))
    pause()

    # 3 ---------------------------------------------------------------- TTO lead review (roster workflow)
    if reg.tto_review_required:
        say("\n3. TTO lead review: only the lead of the employee's own TTO (including for the lead's own MSR); contract "
            "PMO staff are reviewed by a PMO teammate other than the author")
        tto_counts, own, by_tto = Counter(), 0, Counter()
        for who, fb in (summary.get("dialogues") or {}).items():  # what the lead sees from the feedback dialogue
            lead = env.principal(reg.tto_lead_for(reg.subjects[who]["tto_id"]), "tto")
            for row in env.delivery.tto_review_queue(lead):
                if row["employee"].endswith(f"({who})") and row["input_id"] == (fb or {}).get("input_id"):
                    say(f"  {lead.user_id} sees {row['input_id']} ({row['employee'].strip()}): {row['input_tag']}; "
                        f"{row['employee_feedback']}")
            pause(step * 5)
        for case, _ in loaded:
            if not case["scripted_review"] or case["scripted_review"]["decision"] != "accepted":
                continue
            tto_id, subject = case["input"]["tto_id"], case["input"].get("subject_id")
            if reg.is_pmo_staff_tto(tto_id):  # contract PMO staff: a PMO teammate other than the author reviews
                lead_id = next(u for u in reg.pmo_team(reg.pmo_for_contract(case["input"]["contract_id"]))
                               if u != subject)
                acting, kind = env.principal(lead_id, "pmo"), "approved by a PMO teammate"
            else:
                lead_id = reg.tto_lead_for(tto_id)
                if lead_id is None:
                    tto_counts["no TTO lead assigned"] += 1
                    continue
                acting, kind = env.principal(lead_id, "tto"), "approved"
            out = env.delivery.tto_review(acting, case["input"]["input_id"], 1, "approved",
                                          correlation_id=f"SIM-WF-A-{case['case_id']}")
            tto_counts[kind if out.ok else "denied"] += 1
            own += bool(out.ok and out.data.get("own_submission"))
            by_tto[f"{case['input']['tto_id']} by {lead_id}"] += out.ok
            pause(step)
        say(f"  {dict(tto_counts)}; {own} approved by a TTO lead for the lead's own MSR")
        for k, n in sorted(by_tto.items()):
            say(f"    {k}: {n}")
        # Shared authority no longer exists: another TTO's lead is denied.
        other = env.delivery.tto_review(env.principal("SIM-EMP-004", "tto"), "SIM-MSR-202610-006", 1, "approved")
        say(f"  [{ok(other.ok)}] SIM-EMP-004 (lead of SIM-TTO-1-03) approves a SIM-TTO-1-01 MSR: {other.reason}")
        summary["tto"] = dict(tto_counts) | {"own": own, "other_tto_denial": other.reason}
        pause()

    # 4 ---------------------------------------------------------------- human PMO gate + publication
    say("\n4. Human PMO review of the final view, then separate publication of task alignments (no MSR content)")
    env.clock.set(times["approve"])
    events, approve_denials = {}, {}
    tc10 = next((rid for rid, c in created if c["case_id"] == "TC-10"), None)
    if tc10:
        out = env.delivery.pmo_decide(env.principal("SIM-EMP-001"), tc10, 1, "approved", "Fictional approval.")
        say(f"  [{ok(out.ok)}] SIM-EMP-001 (PMO FC-001) approves {tc10} (TC-10, FC-002): {out.reason}")
        summary["cross_pmo_denial"] = out.reason
    turns = Counter()  # spread the review across each contract's PMO team
    approvers = Counter()
    for rid, case in created:
        pmo_id = reg.pmo_for_contract(case["input"]["contract_id"])
        team = [u for u in reg.pmo_team(pmo_id) if u != case["input"].get("subject_id")]  # never the author
        reviewer = env.principal(team[turns[pmo_id] % len(team)], "pmo")
        turns[pmo_id] += 1
        out = env.delivery.pmo_decide(reviewer, rid, 1, "approved", "Fictional reviewer approves the minimized summary.")
        if out.ok:
            events[rid] = (reviewer, out.data["event_id"])
            approvers[reviewer.user_id] += 1
        else:
            approve_denials[rid] = out.reason
        pause(step)
    env.clock.set(times["publish"])
    published = 0
    for rid, (reviewer, eid) in events.items():
        published += env.delivery.publish(reviewer, rid, 1, eid).ok
        pause(step)
    say(f"  Approved {len(events)}, denied {len(approve_denials)} (blocking content), published {published}")
    say(f"  PMO-level reviewers who approved: {len(approvers)} "
        f"({', '.join(f'{u} ×{n}' for u, n in sorted(approvers.items()))})")
    summary.update(approved=len(events), approve_denials=approve_denials, published=published,
                   pmo_approvers=dict(approvers))
    pause()

    # 5 ---------------------------------------------------------------- Workflow B
    say(f"\n5. Workflow B: Solutions lead asks which approved evidence covers {QUERY_TTO}")
    env.clock.set(times["query"])
    tasks = [t for t, v in reg.req_tasks.items() if v["tto_id"] == QUERY_TTO]
    q = {"query_id": "SIM-QUERY-001", "requester_id": SOLUTIONS_LEAD, "audience_id": "SIM-AUD-SOLUTIONS",
         "purpose": "requirement-evidence-explanation", "req_task_ids": tasks,
         "question": "Which approved evidence covers the vulnerability-management deliverables?"}
    resp = env.orchestrator.handle(env.principal(SOLUTIONS_LEAD), q)
    say(f"  status={resp['status']}  ({resp['label']})")
    for c in resp["coverage"]:
        say(f"    {c['req_task_id']} {reg.req_tasks[c['req_task_id']]['title']}: {c['status']} "
            f"({len(c['cited_record_ids'])} citations)")
    say(f"  escalations: {len(resp['escalations'])}")
    summary["coverage"] = {c["req_task_id"]: c["status"] for c in resp["coverage"]}
    summary["escalations"] = len(resp["escalations"])
    if reg.contract_wide_analytic:
        other = [t for t, v in reg.req_tasks.items() if v["tto_id"] == "SIM-TTO-1-02"]
        r2 = env.orchestrator.handle(env.principal(SOLUTIONS_LEAD), q | {
            "query_id": "SIM-QUERY-1-02", "req_task_ids": other,
            "question": "Who else on the contract does work like SIM-TTO-1-02's deliverables?"})
        say(f"  Related work for SIM-TTO-1-02 (contract-wide analytic, tracked apart from coverage): "
            f"{len(r2.get('related_work', []))} record(s)")
        for x in r2.get("related_work", []):
            say(f"    [{x['alignment_scope']}] {x['record_id']}: {x['evidence_summary']}")
        summary["related_work"] = [(x["req_task_id"], x["alignment_scope"]) for x in r2.get("related_work", [])]
    pause()

    # 6 ---------------------------------------------------------------- deny paths
    say("\n6. Deny paths")
    deny = {}
    r = env.orchestrator.handle(env.principal(GROWTH_USER), q | {"query_id": None, "requester_id": GROWTH_USER,
                                                                 "audience_id": "SIM-AUD-GROWTH"})
    deny["other_audience"] = (r["status"], len(r["citations"]), r.get("records_withheld"))
    say(f"  [withheld] Growth audience sees {len(r['citations'])} citations; {r.get('records_withheld')} records withheld")
    pause()
    for title, extra in (("raw MSR request", {"include_raw_sources": True}),
                         ("personnel-ranking purpose", {"purpose": "personnel-ranking"})):
        r = env.orchestrator.handle(env.principal(SOLUTIONS_LEAD), q | {"query_id": None} | extra)
        deny[title] = r["status"]
        say(f"  [{r['status']}] {title}: {r.get('reason', '')}")
        pause()
    try:
        env.delivery_store.list_inputs(env.principal(SOLUTIONS_LEAD))
    except PermissionDenied as e:
        deny["store"] = "denied"
        say(f"  [denied] Solutions lead reads the Delivery store: {e}")
    pause()
    out = env.delivery.prepare_candidates(env.principal(reg.operator_for("SIM-DELIVERY-FC-002")), "SIM-MSR-202610-005")
    deny["operator_scope"] = out.reason
    say(f"  [{ok(out.ok)}] FC-002 operator prepares an FC-001 MSR: {out.reason}")
    summary["deny"] = deny
    pause()

    # 7 ---------------------------------------------------------------- revocation
    say("\n7. Revocation: the PMO revokes the contradicting record (TC-03)")
    tc03 = next((rid for rid, c in created if c["case_id"] == "TC-03" and rid in events), None)
    if tc03:
        out = env.delivery.revoke(env.principal("SIM-EMP-001"), tc03, 1, "Fictional correction after review.")
        say(f"  [{ok(out.ok)}] revoke {tc03}: {out.reason}")
        closure = [t for t in tasks if reg.req_tasks[t]["title"] == "Closure evidence"]
        r = env.orchestrator.handle(env.principal(SOLUTIONS_LEAD),
                                    q | {"query_id": "SIM-QUERY-002", "req_task_ids": closure})
        say(f"  re-query Closure evidence: {[c['status'] for c in r['coverage']]}")
        ack = env.orchestrator.acknowledge(env.principal(SOLUTIONS_LEAD), "SIM-QUERY-002")
        say(f"  [{ok(ack['ok'])}] acknowledge SIM-QUERY-002: {ack['reason']}")
        summary["after_revoke"] = [c["status"] for c in r["coverage"]]
        summary["ack"] = ack["reason"]
    pause()

    # 8 ---------------------------------------------------------------- staleness
    say("\n8. Staleness: advance the clock past the 31-day freshness window")
    env.clock.set("2026-11-24T17:00:00Z")
    r = env.orchestrator.handle(env.principal(SOLUTIONS_LEAD), q | {"query_id": "SIM-QUERY-003"})
    summary["after_expiry_citations"] = len(r["citations"])
    say(f"  after expiry: {len(r['citations'])} citations; {r.get('records_withheld')} records withheld")
    say(f"\nAudit entries recorded: {len(env.audit.entries(limit=100000))}")
    return summary
