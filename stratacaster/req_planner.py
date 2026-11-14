"""REQ-change planning: the Tree of Thought pilot (CR-SC-2026-003).

For one work-role REQ and reporting period, recurring statements that were tagged "potential new task requirement"
in TTO-approved MSRs are clustered. Clusters are the levels of a search tree: at each level the TTO Coordination
Agent proposes options for that cluster (the thought generator), and deterministic code checks hard constraints,
scores each node, prunes weak ones, and keeps a beam of the best partial plans. The result is a ranked set of
complete plans with a full trace. Two human gates follow: the TTO lead (or, for contract PMO staff REQs, a PMO
peer) endorses one plan or rejects all, and the contract's PMO, the approval authority for REQ changes, approves,
returns, or rejects the endorsed plan. A PMO member who endorsed a plan may not also approve it. An approved plan
becomes a REQ change record for the next REQ version; the prototype does not rewrite the live REQ baseline.
"""
from __future__ import annotations

import re
import uuid
from collections import Counter

from . import content_filter as cf
from .agents import TTOCoordinationAgent
from .clock import Clock
from .common import Outcome, allow, deny
from .delivery import POTENTIAL_NEW_TASK, DeliveryService
from .llm import _STOP
from .registry import TTO_COORDINATION_SERVICE, Principal, ReferenceRegistry
from .stores import AuditLog, DeliveryStore, OpsStore

PLANNER_VERSION = "req-planner/0.1.0"
PARAMS = {"beam_width": 3, "max_depth": 3, "node_budget": 60, "prune_below": 0.35, "cluster_jaccard": 0.6,
          "overlap_jaccard": 0.6, "new_req_min_employees": 3, "change_min_employees": 2,
          "route_min_statements": 2}
WEIGHTS = {"coverage": 0.35, "evidence": 0.20, "distinctness": 0.15, "parsimony": 0.20, "wording": 0.10}
PARSIMONY = {"no-change": 1.0, "widen-task": 0.8, "route-elsewhere": 0.8, "add-task": 0.6, "new-req": 0.3}
# Work that is coordination rather than a deliverable: credited to no-change and discounted for changes.
_COORDINATION = re.compile(r"\b(colleague|talked|discuss\w*|meeting|coordinat\w*|sync\w*|liaison|review team|"
                           r"another (person|reviewer)|stand-?up|status call)", re.I)


def content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.lower()) if w not in _STOP and len(w) > 2}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


class ReqPlanner:
    def __init__(self, *, registry: ReferenceRegistry, store: DeliveryStore, delivery: DeliveryService,
                 ops: OpsStore, audit: AuditLog, clock: Clock, agent: TTOCoordinationAgent, params: dict | None = None):
        self.registry, self.store, self.delivery, self.ops = registry, store, delivery, ops
        self.audit, self.clock, self.agent = audit, clock, agent
        self.params = PARAMS | (params or {})

    # ================================================================== who may run and decide

    def req(self, role_req_id: str) -> dict | None:
        found = self.registry.role_reqs.get(role_req_id)
        if found:
            return found
        for s in self.registry.subjects.values():  # a REQ generated from the Requirement Tasks
            if s.get("role_id") and s.get("tto_id"):
                r = self.registry.role_req_for(s["role_id"], s["tto_id"])
                if r and r["role_req_id"] == role_req_id:
                    return r
        return None

    def _basis(self, p: Principal, req: dict) -> str | None:
        """'tto-lead' or 'pmo-peer' when p may run and decide plans for this REQ; None otherwise."""
        if p.kind != "human":
            return None
        tto = req["tto_id"]
        if self.registry.is_pmo_staff_tto(tto):
            pmo = self.registry.pmo_for_contract(self.registry.ttos[tto]["contract_id"])
            return "pmo-peer" if p.has_role("human_pmo_reviewer") and pmo in p.pmo_authority else None
        return "tto-lead" if p.has_role("tto_lead") and tto in p.tto_authority else None

    def _pmo_of(self, req: dict) -> str | None:
        return self.registry.pmo_for_contract(self.registry.ttos[req["tto_id"]]["contract_id"])

    def _is_pmo_approver(self, p: Principal, req: dict) -> bool:
        return p.kind == "human" and p.has_role("human_pmo_reviewer") and self._pmo_of(req) in p.pmo_authority

    def reviewable_reqs(self, p: Principal) -> list[dict]:
        return [r for r in sorted(self.registry.role_reqs.values(), key=lambda r: r["role_req_id"])
                if self._basis(p, r)]

    def periods(self, role_req_id: str) -> list[str]:
        return sorted({m.period_start[:7] for m, _ in self._approved(role_req_id, None)})

    def _with_status(self, plan: dict) -> dict:
        tto = self.store.req_plan_decision(plan["plan_id"], "tto")
        pmo = self.store.req_plan_decision(plan["plan_id"], "pmo")
        if not plan["plans"]:
            status = "no-viable-plan"
        elif tto is None:
            status = "awaiting-tto-endorsement"
        elif tto["decision"] == "rejected":
            status = "rejected-by-tto"
        elif pmo is None:
            status = "awaiting-pmo-approval"
        else:
            status = {"approved": "approved", "returned": "returned-by-pmo", "rejected": "rejected-by-pmo"}[
                pmo["decision"]]
        return plan | {"tto_decision": tto, "pmo_decision": pmo, "status": status}

    def plans(self, p: Principal, role_req_id: str) -> list[dict]:
        req = self.req(role_req_id)
        if not req or not (self._basis(p, req) or self._is_pmo_approver(p, req)):
            return []
        return [self._with_status(pl) for pl in self.store.req_plans(p, role_req_id)]

    def pmo_queue(self, p: Principal) -> list[dict]:
        """Endorsed plans awaiting this PMO reviewer's decision (not ones the reviewer endorsed)."""
        if not p.has_role("human_pmo_reviewer"):
            return []
        out = []
        for pl in self.store.req_plans(TTO_COORDINATION_SERVICE):
            req = self.req(pl["role_req_id"])
            if not req or not self._is_pmo_approver(p, req):
                continue
            pl = self._with_status(pl)
            if pl["status"] == "awaiting-pmo-approval" and pl["tto_decision"]["decided_by"] != p.user_id:
                out.append(pl)
        return out

    # ================================================================== evidence for one REQ

    def _approved(self, role_req_id: str, period: str | None):
        """(MSR, assessment) for current, TTO-approved inputs assessed against this REQ."""
        s = TTO_COORDINATION_SERVICE
        for m in self.store.list_inputs(s):
            if m.source_status != "active" or (period and m.period_start[:7] != period):
                continue
            if self.delivery.tto_status(m.input_id, m.input_version) != "approved":
                continue
            a = self.delivery.assessment(s, m.input_id, m.input_version)
            if a and a.get("role_req_id") == role_req_id:
                yield m, a

    def gather(self, role_req_id: str, period: str | None) -> tuple[list[dict], list[dict]]:
        """Potential-new-task statements, and those excluded because the content filter flags them (PII,
        restricted content, or possible prompt injection never reach the agent)."""
        items, excluded = [], []
        for m, a in self._approved(role_req_id, period):
            text = dict(self.store.get_statements(TTO_COORDINATION_SERVICE, m.input_id, m.input_version))
            cw = {r["statement_id"]: r["elsewhere"] for r in (a.get("contract_wide") or {}).get("statements", [])}
            for st in a["statements"]:
                if st["tag"] != POTENTIAL_NEW_TASK or st["statement_id"] not in text:
                    continue
                flags = cf.scan_source(text[st["statement_id"]])
                if flags:
                    excluded.append({"statement_id": st["statement_id"], "input_id": m.input_id, "flags": flags})
                    continue
                items.append({"statement_id": st["statement_id"], "input_id": m.input_id,
                              "subject_id": m.subject_id, "text": text[st["statement_id"]],
                              "elsewhere": cw.get(st["statement_id"], [])})
        return sorted(items, key=lambda x: x["statement_id"]), excluded

    def cluster(self, items: list[dict]) -> list[dict]:
        """Greedy and deterministic: a statement joins the first cluster whose seed it matches (content-word
        Jaccard), else seeds a new one. Clusters are ordered by employee support, then size."""
        groups: list[dict] = []
        for it in items:
            words = content_words(it["text"])
            g = next((g for g in groups if jaccard(words, g["seed"]) >= self.params["cluster_jaccard"]), None)
            if g is None:
                g = {"seed": words, "seed_text": it["text"], "items": []}
                groups.append(g)
            g["items"].append(it)
        groups.sort(key=lambda g: (-len({i["subject_id"] for i in g["items"]}), -len(g["items"]),
                                   g["items"][0]["statement_id"]))
        out = []
        for n, g in enumerate(groups, 1):
            freq = Counter(w for i in g["items"] for w in content_words(i["text"]))
            elsewhere: dict[str, dict] = {}
            for i in g["items"]:
                for e in i["elsewhere"]:
                    row = elsewhere.setdefault(e["req_task_id"], {"req_task_id": e["req_task_id"],
                                                                  "tto_id": e["tto_id"], "statements": 0})
                    row["statements"] += 1
            out.append({"cluster_id": f"C{n}", "statement_ids": [i["statement_id"] for i in g["items"]],
                        "input_ids": sorted({i["input_id"] for i in g["items"]}),
                        "employees": len({i["subject_id"] for i in g["items"]}),
                        "label": " ".join(w for w, _ in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:5]),
                        "elsewhere": sorted(elsewhere.values(), key=lambda e: e["req_task_id"]),
                        "_texts": [i["text"] for i in g["items"]], "_seed": g["seed_text"]})
        return out

    # ================================================================== constraints and scoring

    def req_size_limit(self, req: dict) -> int | None:
        """Optional per-contract cap on tasks in one work-role REQ, set by that contract's PMO in the reference data
        (`req_max_tasks` on the contract record). None, the default, means REQs are not size-limited; parsimony
        scoring and the two human gates then keep REQs from growing without reason."""
        contract = self.registry.contracts.get(self.registry.ttos[req["tto_id"]]["contract_id"], {})
        limit = contract.get("req_max_tasks")
        return int(limit) if limit else None

    def _violations(self, opt: dict, cluster: dict, req: dict, plan: list[dict], contract_tasks: list[dict]) -> list[str]:
        p, why = self.params, []
        op, words = opt["operator"], content_words(opt["wording"])
        if op in ("add-task", "new-req"):
            dup = next((t for t in contract_tasks
                        if jaccard(words, content_words(t["title"])) >= p["overlap_jaccard"]), None)
            if dup:
                why.append(f"overlap: wording duplicates {dup['req_task_id']}; widen or route instead")
        limit = self.req_size_limit(req)
        adds = sum(d["operator"] == "add-task" for d in plan) + (op == "add-task")
        if op == "add-task" and limit is not None and len(req["req_task_ids"]) + adds > limit:
            why.append(f"req-size: REQ would exceed its contract's limit of {limit} tasks")
        if op == "new-req":
            if cluster["employees"] < p["new_req_min_employees"]:
                why.append(f"new-req: needs at least {p['new_req_min_employees']} employees "
                           f"(cluster has {cluster['employees']})")
            if any(d["operator"] == "new-req" for d in plan):
                why.append("new-req: at most one new REQ per plan")
        if op != "no-change":
            if any(d["operator"] == op and d["wording"].lower() == opt["wording"].lower() for d in plan):
                why.append("duplicate: the same change is already in this plan")
            if any(t.strip().lower() == opt["wording"].strip().lower() or cf._shingles(opt["wording"]) & cf._shingles(t)
                   for t in cluster["_texts"]):
                why.append("wording-copy: option copies statement text")
            if cf._PERSONNEL.search(opt["wording"]) or cf._INJECTION.search(opt["wording"]):
                why.append("wording: personnel or instruction content")
        return why

    def _score(self, opt: dict, cluster: dict, contract_tasks: list[dict]) -> dict:
        """Weighted score, then the support rule: work reported by fewer than `change_min_employees` people cannot
        rank a change at or above leaving the REQ alone (found in the first live Claude test, where one employee's
        statement and a weak cross-REQ match ranked a re-route first)."""
        s = self._raw_score(opt, cluster, contract_tasks)
        if opt["operator"] != "no-change" and cluster["employees"] < self.params["change_min_employees"]:
            keep = self._raw_score({"operator": "no-change", "wording": "No REQ change"}, cluster, contract_tasks)
            if s["score"] >= keep["score"]:
                s = s | {"score": round(keep["score"] - 0.01, 4),
                         "capped": f"support: {cluster['employees']} employee(s) < "
                                   f"{self.params['change_min_employees']}; ranked below no change"}
        return s

    def _raw_score(self, opt: dict, cluster: dict, contract_tasks: list[dict]) -> dict:
        texts, n = cluster["_texts"], len(cluster["_texts"])
        coord = sum(bool(_COORDINATION.search(t)) for t in texts) / n
        support = min(1.0, cluster["employees"] / self.params["new_req_min_employees"])
        op = opt["operator"]
        if op == "no-change":
            coverage, evidence, distinct = coord, 1.0 - support, 1.0
        elif op == "route-elsewhere":
            hits = next((e["statements"] for e in cluster["elsewhere"]
                         if e["req_task_id"] == opt["target_req_task_id"]), 0)
            # a re-route resting on a single matching statement counts for less
            coverage = hits / n * min(1.0, hits / self.params["route_min_statements"])
            evidence, distinct = support, 1.0
        else:
            basis = content_words(self.registry.req_tasks[opt["target_req_task_id"]]["title"]) \
                if op == "widen-task" else content_words(opt["wording"])
            coverage = sum(bool(content_words(t) & basis) for t in texts) / n * (1.0 - coord)
            evidence = support
            distinct = 1.0 if op == "widen-task" else 1.0 - max(
                (jaccard(content_words(opt["wording"]), content_words(t["title"])) for t in contract_tasks),
                default=0.0)
        wording = 1.0 if 2 <= len(opt["wording"].split()) <= 12 else 0.5
        parts = {"coverage": coverage, "evidence": evidence, "distinctness": distinct,
                 "parsimony": PARSIMONY[op], "wording": wording}
        return {"parts": {k: round(v, 3) for k, v in parts.items()},
                "score": round(sum(WEIGHTS[k] * v for k, v in parts.items()), 4)}

    @staticmethod
    def _rank_key(node: dict):
        """Higher mean score first; ties go to the smaller change, then more evidence, then the plan key."""
        ds = node["decisions"]
        change = sum(1.0 - PARSIMONY[d["operator"]] for d in ds)
        evidence = sum(d["parts"]["evidence"] for d in ds)
        return (-node["score"], change, -evidence, node["key"])

    # ================================================================== the search

    def search(self, req: dict, clusters: list[dict], options: dict[str, list[dict]]) -> dict:
        """Beam search over clusters (one level per cluster). Returns ranked plans and the trace."""
        p = self.params
        contract = self.registry.ttos[req["tto_id"]]["contract_id"]
        contract_tasks = self.registry.contract_req_tasks(contract)
        levels = clusters[:p["max_depth"]]
        beam = [{"node_id": "N0", "decisions": [], "score": 0.0, "key": ""}]
        trace = [{"node_id": "N0", "parent": None, "depth": 0, "status": "root"}]
        generated, exhausted = 0, False
        for depth, c in enumerate(levels, 1):
            children = []
            for parent in beam:
                for opt in options.get(c["cluster_id"], []):
                    if generated >= p["node_budget"]:
                        exhausted = True
                        break
                    generated += 1
                    nid = f"N{generated}"
                    row = {"node_id": nid, "parent": parent["node_id"], "depth": depth, "cluster_id": c["cluster_id"],
                           "operator": opt["operator"], "target_req_task_id": opt["target_req_task_id"],
                           "wording": opt["wording"]}
                    why = self._violations(opt, c, req, parent["decisions"], contract_tasks)
                    if why:
                        trace.append(row | {"status": "pruned", "reason": "; ".join(why)})
                        continue
                    s = self._score(opt, c, contract_tasks)
                    row |= {"score": s["score"], "parts": s["parts"]} | ({"capped": s["capped"]} if "capped" in s else {})
                    if s["score"] < p["prune_below"]:
                        trace.append(row | {"status": "pruned", "reason": f"score {s['score']} < {p['prune_below']}"})
                        continue
                    decision = {"cluster_id": c["cluster_id"], **opt, **s}
                    ds = parent["decisions"] + [decision]
                    key = " | ".join(f"{d['cluster_id']}:{d['operator']}:{d['target_req_task_id'] or '-'}" for d in ds)
                    children.append({"node_id": nid, "decisions": ds, "key": key,
                                     "score": round(sum(d["score"] for d in ds) / len(ds), 4)})
                    trace.append(row | {"status": "candidate", "plan_score": children[-1]["score"]})
                if exhausted:
                    break
            children.sort(key=self._rank_key)
            kept = {ch["node_id"] for ch in children[:p["beam_width"]]}
            for row in trace:
                if row.get("depth") == depth and row["status"] == "candidate":
                    row["status"] = "kept" if row["node_id"] in kept else "beam-cut"
            beam = children[:p["beam_width"]]
            if not beam or exhausted:
                break
        complete = [b for b in beam if len(b["decisions"]) == len(levels)] if levels else []
        plans = [{"rank": i, "plan_score": b["score"], "key": b["key"], "node_id": b["node_id"],
                  "decisions": [{k: d[k] for k in ("cluster_id", "operator", "target_req_task_id", "wording",
                                                   "rationale", "score", "parts")}
                                | ({"capped": d["capped"]} if "capped" in d else {}) for d in b["decisions"]]}
                 for i, b in enumerate(complete, 1)]
        return {"plans": plans, "trace": trace, "nodes_generated": generated, "budget_exhausted": exhausted,
                "depth_reached": max((r["depth"] for r in trace), default=0)}

    # ================================================================== consequential actions

    def plan_req(self, p: Principal, role_req_id: str, period: str | None, correlation_id: str = "") -> Outcome:
        cid = correlation_id or f"{self.registry.id_prefix}-RQP-{uuid.uuid4().hex[:8].upper()}"
        refs = {"role_req_id": role_req_id, "period": period or "all"}
        req = self.req(role_req_id)
        if req is None:
            return self._deny(p, "plan_req", "unknown REQ", cid, refs)
        basis = self._basis(p, req)
        if basis is None:
            return self._deny(p, "plan_req", f"REQ-change planning for {req['tto_id']} is run by its TTO lead (or, for "
                              "contract PMO staff, a member of that contract's PMO team)", cid, refs)
        items, excluded = self.gather(role_req_id, period)
        clusters = self.cluster(items)
        if not clusters:
            self.audit.record(p, "plan_req", "no-signal", correlation_id=cid, refs=refs)
            return allow("no potential new task statements in TTO-approved MSRs for this REQ and period",
                         plan_id=None, correlation_id=cid)
        levels = clusters[:self.params["max_depth"]]
        req_tasks = [self.registry.req_tasks[t] for t in req["req_task_ids"] if t in self.registry.req_tasks]
        options, agent_errors, meta = {}, [], None
        for c in levels:
            payload_cluster = {"cluster_id": c["cluster_id"], "text": c["_seed"],
                               "statements": list(dict.fromkeys(c["_texts"]))[:5], "employees": c["employees"],
                               "statement_count": len(c["_texts"])}
            elsewhere = [{"req_task_id": e["req_task_id"], "title": self.registry.req_tasks[e["req_task_id"]]["title"],
                          "tto_id": e["tto_id"]} for e in c["elsewhere"]]
            result, errors = self.agent.propose(payload_cluster, req_tasks, elsewhere)
            agent_errors += [f"{c['cluster_id']}: {e}" for e in errors]
            if result is None:
                options[c["cluster_id"]] = []
                continue
            meta = result.meta()
            options[c["cluster_id"]] = [o.model_dump() for o in result.parsed.options]
        found = self.search(req, levels, options)
        plan_id = f"{self.registry.id_prefix}-RQP-{uuid.uuid4().hex[:10].upper()}"
        record = {
            "plan_id": plan_id, "planner_version": PLANNER_VERSION, "role_req_id": role_req_id,
            "role_id": req["role_id"], "tto_id": req["tto_id"], "period": period or "all",
            "created_at": self.clock.now_str(), "requested_by": p.user_id, "review_basis": basis,
            "agent_id": self.agent.agent_id, "prompt_version": self.agent.prompt_version, "model": meta,
            "params": dict(self.params), "weights": dict(WEIGHTS), "req_size_limit": self.req_size_limit(req),
            "baseline_req_task_ids": list(req["req_task_ids"]),
            "clusters": [{k: v for k, v in c.items() if not k.startswith("_")} for c in clusters],
            "searched_clusters": [c["cluster_id"] for c in levels],
            "deferred_clusters": [c["cluster_id"] for c in clusters[len(levels):]],
            "excluded_statements": excluded,
            "options": options, "agent_errors": agent_errors, **found,
            "recommended_rank": 1 if found["plans"] else None,
            "status": "awaiting-tto-endorsement" if found["plans"] else "no-viable-plan",
            "approval_authority": self._pmo_of(req), "baseline_changed": False}
        self.store.append_req_plan(TTO_COORDINATION_SERVICE, record)
        esc = None
        if not found["plans"]:
            esc = self.ops.add_escalation(f"ESC-{uuid.uuid4().hex[:8].upper()}", cid, "req-plan-no-viable-plan",
                                          p.user_id, "delivery",
                                          "REQ-change planning found no plan that passed the checks; review the "
                                          "clusters manually.", refs | {"plan_id": plan_id})
        self.audit.record(p, "plan_req", record["status"], correlation_id=cid,
                          refs=refs | {"plan_id": plan_id, "clusters": len(clusters), "plans": len(found["plans"]),
                                       "nodes": found["nodes_generated"], "escalation_id": esc},
                          meta=(meta or {}) | {"agent_id": self.agent.agent_id,
                                               "prompt_version": self.agent.prompt_version,
                                               "agent_errors": len(agent_errors)})
        return allow(record["status"], plan_id=plan_id, plans=len(found["plans"]), correlation_id=cid,
                     escalation_id=esc)

    def _plan(self, plan_id: str) -> dict | None:
        return next((x for x in self.store.req_plans(TTO_COORDINATION_SERVICE) if x["plan_id"] == plan_id), None)

    def decide_req_plan(self, p: Principal, plan_id: str, decision: str, plan_rank: int | None = None,
                        reason: str = "", correlation_id: str = "") -> Outcome:
        """TTO-stage gate: the TTO lead (or a PMO peer, for PMO staff REQs) endorses one ranked plan for PMO approval
        or rejects all. Endorsement is a recommendation, not approval."""
        cid = correlation_id or f"{self.registry.id_prefix}-RQD-{uuid.uuid4().hex[:8].upper()}"
        refs = {"plan_id": plan_id, "decision": decision}
        if decision not in ("endorsed", "rejected"):
            return self._deny(p, "decide_req_plan", f"invalid decision {decision}", cid, refs)
        plan = self._plan(plan_id)
        if plan is None:
            return self._deny(p, "decide_req_plan", "unknown plan", cid, refs)
        req = self.req(plan["role_req_id"])
        basis = self._basis(p, req) if req else None
        if basis is None:
            return self._deny(p, "decide_req_plan", "only a human TTO lead for this REQ's TTO (or, for contract PMO "
                              "staff, a PMO team member) may endorse a REQ-change plan", cid, refs)
        if self.store.req_plan_decision(plan_id, "tto"):
            return self._deny(p, "decide_req_plan", "an endorsement decision is already recorded for this plan", cid,
                              refs)
        chosen = None
        if decision == "endorsed":
            chosen = next((x for x in plan["plans"] if x["rank"] == plan_rank), None)
            if chosen is None:
                return self._deny(p, "decide_req_plan", f"plan rank {plan_rank} is not one of the ranked plans", cid,
                                  refs)
        if decision == "rejected" and not reason.strip():
            return self._deny(p, "decide_req_plan", "a reason is required to reject all plans", cid, refs)
        event = {"plan_id": plan_id, "stage": "tto", "decision": decision,
                 "plan_rank": plan_rank if chosen else None, "endorsed_plan": chosen, "decided_by": p.user_id,
                 "review_basis": basis, "decided_at": self.clock.now_str(), "reason": reason[:300],
                 "next_step": f"PMO approval ({self._pmo_of(req)})" if chosen else "none"}
        self.store.append_req_plan_decision(p, event)
        self.audit.record(p, "decide_req_plan", decision, reason=reason[:200], correlation_id=cid,
                          refs=refs | {"plan_rank": event["plan_rank"], "role_req_id": plan["role_req_id"],
                                       "review_basis": basis})
        return allow(f"req-plan-{decision}", **refs, plan_rank=event["plan_rank"], correlation_id=cid)

    def pmo_decide_req_plan(self, p: Principal, plan_id: str, decision: str, reason: str = "",
                            correlation_id: str = "") -> Outcome:
        """PMO gate: the contract's PMO is the approval authority for REQ changes. A member of that PMO team approves,
        returns, or rejects an endorsed plan; whoever endorsed it may not approve it. Approval records a REQ change
        for the next REQ version; the live REQ baseline is not rewritten by the prototype."""
        cid = correlation_id or f"{self.registry.id_prefix}-RQA-{uuid.uuid4().hex[:8].upper()}"
        refs = {"plan_id": plan_id, "decision": decision}
        if decision not in ("approved", "returned", "rejected"):
            return self._deny(p, "pmo_decide_req_plan", f"invalid decision {decision}", cid, refs)
        plan = self._plan(plan_id)
        if plan is None:
            return self._deny(p, "pmo_decide_req_plan", "unknown plan", cid, refs)
        req = self.req(plan["role_req_id"])
        if not req or not self._is_pmo_approver(p, req):
            return self._deny(p, "pmo_decide_req_plan", f"REQ changes are approved by the contract's PMO "
                              f"({self._pmo_of(req) if req else 'unknown'}); a human member of that PMO team must "
                              "decide", cid, refs)
        tto = self.store.req_plan_decision(plan_id, "tto")
        if not tto or tto["decision"] != "endorsed":
            return self._deny(p, "pmo_decide_req_plan", "the plan has not been endorsed at the TTO stage", cid, refs)
        if tto["decided_by"] == p.user_id:
            return self._deny(p, "pmo_decide_req_plan", "separation of duties: the person who endorsed a plan may not "
                              "also approve it", cid, refs)
        if self.store.req_plan_decision(plan_id, "pmo"):
            return self._deny(p, "pmo_decide_req_plan", "a PMO decision is already recorded for this plan", cid, refs)
        if decision != "approved" and not reason.strip():
            return self._deny(p, "pmo_decide_req_plan", f"a reason is required to {decision[:-2]} a plan", cid,
                              refs)
        chosen = tto["endorsed_plan"]
        change = None
        if decision == "approved":
            changes = [d for d in chosen["decisions"] if d["operator"] != "no-change"]
            adds = sum(d["operator"] == "add-task" for d in changes)
            change = {"change_id": f"{self.registry.id_prefix}-RQC-{uuid.uuid4().hex[:8].upper()}",
                      "role_req_id": plan["role_req_id"], "baseline_req_task_ids": plan["baseline_req_task_ids"],
                      "changes": [{k: d[k] for k in ("cluster_id", "operator", "target_req_task_id", "wording")}
                                  for d in changes],
                      "resulting_task_count": len(plan["baseline_req_task_ids"]) + adds,
                      "status": "approved; to be incorporated as the next REQ version"}
        event = {"plan_id": plan_id, "stage": "pmo", "decision": decision, "plan_rank": tto["plan_rank"],
                 "decided_by": p.user_id, "pmo_id": self._pmo_of(req), "decided_at": self.clock.now_str(),
                 "reason": reason[:300], "req_change": change, "baseline_changed": False,
                 "next_step": "incorporate into the controlled Requirement Tasks as the next REQ version"
                 if change else "returned to the TTO stage for a new plan" if decision == "returned" else "none"}
        self.store.append_req_plan_decision(p, event)
        self.audit.record(p, "pmo_decide_req_plan", decision, reason=reason[:200], correlation_id=cid,
                          refs=refs | {"role_req_id": plan["role_req_id"], "plan_rank": tto["plan_rank"],
                                       "pmo_id": event["pmo_id"],
                                       "change_id": change["change_id"] if change else None})
        return allow(f"req-change-{decision}", **refs, change_id=change["change_id"] if change else None,
                     correlation_id=cid)

    def _deny(self, p: Principal, action: str, reason: str, cid: str, refs: dict) -> Outcome:
        self.audit.record(p, action, "denied", reason=reason, correlation_id=cid, refs=refs)
        return deny(reason, **refs)
