"""Synthetic reference-fixture registry: referential integrity, authority, and audience checks."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Principal:
    """An authenticated actor. In the prototype, authentication is simulated by fixture lookup."""
    user_id: str
    kind: str  # "human" | "agent" | "service"
    domain: str  # "delivery" | "solutions" | "shared"
    roles: frozenset = field(default_factory=frozenset)
    pmo_authority: frozenset = field(default_factory=frozenset)
    delivery_scopes: frozenset = field(default_factory=frozenset)  # empty = unrestricted (operator scope)
    active_role: str = ""  # set when the user acts in one chosen role (see ReferenceRegistry.principal)
    tto_authority: frozenset = field(default_factory=frozenset)  # TTOs this person leads (TTO lead role)

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def in_scope(self, delivery_scope_id: str) -> bool:
        return not self.delivery_scopes or delivery_scope_id in self.delivery_scopes


# Roles a person can act in, one at a time. A manager who also submits MSRs can act purely as an employee.
ROLE_BUNDLES = [
    ("employee", "Employee (own MSRs)", {"employee_reviewer"}),
    ("operator", "Delivery operator", {"delivery_operator"}),
    ("tto", "TTO lead reviewer", {"tto_lead"}),
    ("pmo", "PMO reviewer", {"human_pmo_reviewer"}),
    ("solutions", "Solutions requester / reviewer", {"solutions_requester", "solutions_reviewer"}),
]

GATEWAY_SERVICE = Principal("SVC-RETRIEVAL-GATEWAY", "service", "shared", frozenset({"retrieval_gateway"}))
SOURCE_STATUS_SERVICE = Principal("SVC-SOURCE-STATUS", "service", "delivery", frozenset({"source_status"}))


class DeliveryVisibility:
    """Row-level least privilege inside Delivery. A Delivery principal sees an MSR (and evidence derived from it)
    only if one of its roles grants it: the subject's own reviewer, an operator whose scope covers the contract,
    or a PMO reviewer with authority over the contract's PMO. The source-status service sees status for all."""

    def __init__(self, registry: "ReferenceRegistry", subject_of):
        self.registry = registry
        self.subject_of = subject_of  # input_id -> subject_id (Delivery-internal lookup)

    def can_see(self, p: Principal, delivery_scope_id: str, subject_id: str | None, tto_id: str | None = None) -> bool:
        if p.has_role("source_status") or p.has_role("role_execution") or p.has_role("tto_coordination"):
            return True  # agent and service identities; their callers scope what they read
        if p.has_role("delivery_operator") and p.in_scope(delivery_scope_id):
            return True
        if p.has_role("tto_lead") and tto_id and tto_id in p.tto_authority:
            return True
        if p.has_role("human_pmo_reviewer") and any(
                c["delivery_scope_id"] == delivery_scope_id and c["pmo_id"] in p.pmo_authority
                for c in self.registry.contracts.values()):
            return True
        return bool(subject_id) and p.has_role("employee_reviewer") \
            and self.registry.employee_reviewer_for(subject_id) == p.user_id

    def can_see_record(self, p: Principal, rec) -> bool:
        return self.can_see(p, rec.delivery_scope_id, self.subject_of(rec.source_ref.input_id), rec.tto_id)


# The Role Execution Agent's identity inside Delivery when it assesses a submitted MSR.
ROLE_EXECUTION_SERVICE = Principal("AGT-ROLE-EXECUTION", "agent", "delivery", frozenset({"role_execution"}))
# The TTO Coordination Agent's identity when the REQ planning service reads one REQ's assessments.
TTO_COORDINATION_SERVICE = Principal("AGT-TTO-COORDINATION", "agent", "delivery", frozenset({"tto_coordination"}))


class ReferenceRegistry:
    def __init__(self, data: dict):
        self.version = data["fixture_version"]
        self.pmos = {p["pmo_id"]: p for p in data["pmos"]}
        self.contracts = {c["contract_id"]: c for c in data["contracts"]}
        self.ttos = {t["tto_id"]: t for t in data["ttos"]}
        self.roles = {r["role_id"]: r for r in data["roles"]}
        self.role_tasks = {t["role_task_id"]: t for t in data["role_tasks"]}
        self.req_tasks = {t["req_task_id"]: t for t in data["req_tasks"]}
        self.subjects = {s["subject_id"]: s for s in data["subjects"]}
        self.audiences = {a["audience_id"]: a for a in data["audiences"]}
        self.purposes = {p["purpose_id"]: p for p in data["purposes"]}
        self.users = {u["user_id"]: u for u in data["users"]}
        # Dataset-specific settings (the roster dataset carries these; the guide dataset uses config defaults).
        self.id_prefix = data.get("id_prefix", "SYN")
        self.routing = data.get("routing", {})
        self.evidence_defaults = data.get("evidence_defaults", {})
        self.demo = data.get("demo", {})
        self.workflow = data.get("workflow", {})  # e.g. {"tto_lead_review": true} on the roster dataset
        # Each work role within a TTO has an individual REQ listing its tasks (roster dataset).
        self.role_reqs = {r["role_req_id"]: r for r in data.get("role_reqs", [])}

    @property
    def tto_review_required(self) -> bool:
        return bool(self.workflow.get("tto_lead_review"))

    @property
    def assess_on_submission(self) -> bool:
        return bool(self.workflow.get("assess_on_submission"))

    @property
    def contract_wide_analytic(self) -> bool:
        return bool(self.workflow.get("contract_wide_analytic"))

    @property
    def publish_task_alignment_only(self) -> bool:
        return bool(self.workflow.get("publish_task_alignment_only"))

    def is_pmo_staff_tto(self, tto_id: str) -> bool:
        """The contract PMO label (staff assigned directly to the contract, no TTO): a PMO teammate reviews instead."""
        return bool(self.ttos.get(tto_id, {}).get("pmo_review"))

    def tto_lead_for(self, tto_id: str) -> str | None:
        """The TTO lead with authority over this TTO (only that lead may approve its MSRs)."""
        for u in self.users.values():
            if "tto_lead" in u.get("roles", []) and tto_id in u.get("tto_authority", []):
                return u["user_id"]
        return None

    def role_req_for(self, role_id: str, tto_id: str) -> dict | None:
        """The work role's individual REQ within a TTO (its list of tasks). If none has been identified, one is
        generated from the Requirement Tasks (REQ tasks under the TTO that name the role as eligible)."""
        found = next((r for r in self.role_reqs.values() if r["role_id"] == role_id and r["tto_id"] == tto_id), None)
        if found:
            return found
        tasks = self.req_tasks_for(role_id, tto_id)
        if not tasks:
            return None
        return {"role_req_id": f"{self.id_prefix}-REQ-{tto_id.split('TTO-', 1)[-1]}-{role_id}", "role_id": role_id,
                "tto_id": tto_id, "req_task_ids": [t["req_task_id"] for t in tasks], "generated": True}

    def profile(self, subject_id: str) -> dict | None:
        """Roster assignment used to label a newly submitted MSR (roster dataset only)."""
        s = self.subjects.get(subject_id)
        return s if s and all(k in s for k in ("role_id", "tto_id", "contract_id", "delivery_scope_id")) else None

    @classmethod
    def load(cls, path: Path) -> "ReferenceRegistry":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    # ------------------------------------------------------------ authentication (simulated)

    def principal(self, user_id: str, as_role: str | None = None) -> Principal:
        """All of the user's roles, or (as_role) only one role bundle with only that role's authority."""
        u = self.users.get(user_id)
        if u is None:
            raise KeyError(f"unknown user {user_id!r}")
        roles = frozenset(u.get("roles", []))
        pmo, scopes = frozenset(u.get("pmo_authority", [])), frozenset(u.get("delivery_scopes", []))
        ttos = frozenset(u.get("tto_authority", []))
        if as_role is None:
            return Principal(u["user_id"], u["kind"], u["domain"], roles, pmo, scopes, "", ttos)
        bundle = next((b for k, _, b in ROLE_BUNDLES if k == as_role), None)
        if bundle is None or not bundle & roles:
            raise KeyError(f"{user_id} cannot act in role {as_role!r}")
        active = frozenset(bundle & roles)
        return Principal(u["user_id"], u["kind"], u["domain"], active,
                         pmo if "human_pmo_reviewer" in active else frozenset(),
                         scopes if "delivery_operator" in active else frozenset(), as_role,
                         ttos if "tto_lead" in active else frozenset())

    def role_options(self, user_id: str) -> list[tuple[str, str]]:
        """(key, label) for each role the user can act in, with that role's authority in the label."""
        u = self.users[user_id]
        roles = set(u.get("roles", []))
        out = []
        for key, label, bundle in ROLE_BUNDLES:
            if bundle & roles:
                if key == "tto" and u.get("tto_authority"):
                    label += f" ({', '.join(u['tto_authority'])})"
                if key == "operator" and u.get("delivery_scopes"):
                    label += f" ({', '.join(u['delivery_scopes'])})"
                if key == "pmo" and u.get("pmo_authority"):
                    label += f" ({', '.join(u['pmo_authority'])})"
                out.append((key, label))
        return out

    def operator_for(self, delivery_scope_id: str, exclude: str | None = None) -> str | None:
        """First Delivery operator whose scope covers the given delivery scope, skipping `exclude` (e.g. the
        MSR's own subject, for separation of duties)."""
        for u in self.users.values():
            if u["user_id"] != exclude and "delivery_operator" in u.get("roles", []) and (
                    not u.get("delivery_scopes") or delivery_scope_id in u["delivery_scopes"]):
                return u["user_id"]
        return None

    # ------------------------------------------------------------ referential integrity

    def pmo_team(self, pmo_id: str) -> list[str]:
        """Everyone with PMO review authority for this PMO, primary reviewer (Program Manager) first."""
        primary = self.pmos.get(pmo_id, {}).get("reviewer_id")
        team = sorted(u["user_id"] for u in self.users.values()
                      if "human_pmo_reviewer" in u.get("roles", []) and pmo_id in u.get("pmo_authority", []))
        return sorted(team, key=lambda uid: uid != primary)

    def pmo_for_contract(self, contract_id: str) -> str | None:
        c = self.contracts.get(contract_id)
        return c["pmo_id"] if c else None

    def input_reference_errors(self, role_id: str, tto_id: str, contract_id: str,
                               delivery_scope_id: str, subject_id: str) -> list[str]:
        errs = self._structure_errors(role_id, tto_id, contract_id, delivery_scope_id)
        if subject_id not in self.subjects:
            errs.append(f"unknown subject_id {subject_id}")
        return errs

    def _structure_errors(self, role_id: str, tto_id: str, contract_id: str,
                          delivery_scope_id: str) -> list[str]:
        errs = []
        if role_id not in self.roles:
            errs.append(f"unknown role_id {role_id}")
        contract = self.contracts.get(contract_id)
        if contract is None:
            errs.append(f"unknown contract_id {contract_id}")
        elif contract["delivery_scope_id"] != delivery_scope_id:
            errs.append(f"contract {contract_id} is not in delivery scope {delivery_scope_id}")
        tto = self.ttos.get(tto_id)
        if tto is None:
            errs.append(f"unknown tto_id {tto_id}")
        elif tto["contract_id"] != contract_id:
            errs.append(f"tto {tto_id} belongs to {tto['contract_id']}, not {contract_id}")
        return errs

    def evidence_reference_errors(self, rec) -> list[str]:
        errs = self._structure_errors(rec.role_id, rec.tto_id, rec.contract_id, rec.delivery_scope_id)
        if rec.owning_pmo_id not in self.pmos:
            errs.append(f"unknown owning_pmo_id {rec.owning_pmo_id}")
        elif self.pmo_for_contract(rec.contract_id) != rec.owning_pmo_id:
            errs.append(f"contract {rec.contract_id} is not owned by {rec.owning_pmo_id}")
        req = self.req_tasks.get(rec.req_task_id)
        own = rec.alignment_scope == "own-req"
        if req is None:
            errs.append(f"unknown req_task_id {rec.req_task_id}")
        else:
            if own and req["tto_id"] != rec.tto_id:
                errs.append(f"req task {rec.req_task_id} is not under tto {rec.tto_id}")
            if not own and self.ttos.get(req["tto_id"], {}).get("contract_id") != rec.contract_id:
                errs.append(f"req task {rec.req_task_id} is not on contract {rec.contract_id}")
            if rec.role_task_id not in self.eligible_role_tasks(req):
                errs.append(f"req task {rec.req_task_id} is not eligible for role task {rec.role_task_id}")
        rt = self.role_tasks.get(rec.role_task_id)
        if rt is None:
            errs.append(f"unknown role_task_id {rec.role_task_id}")
        elif own and rt["role_id"] != rec.role_id:  # a cross-REQ record names the other REQ's role task
            errs.append(f"role task {rec.role_task_id} is not for role {rec.role_id}")
        for a in rec.audience_ids:
            if a not in self.audiences:
                errs.append(f"unknown audience {a}")
        for p in rec.permitted_purposes:
            if not self.purposes.get(p, {}).get("allowed"):
                errs.append(f"purpose {p} is unknown or prohibited")
        return errs

    @staticmethod
    def eligible_role_tasks(req: dict) -> list[str]:
        """A REQ task names one role task (guide dataset) or a list of eligible role tasks (roster dataset)."""
        return req.get("role_task_ids") or [req["role_task_id"]]

    def role_task_for(self, req_task_id: str, role_id: str) -> str | None:
        for rt in self.eligible_role_tasks(self.req_tasks[req_task_id]):
            if self.role_tasks.get(rt, {}).get("role_id") == role_id:
                return rt
        return None

    def req_tasks_for(self, role_id: str, tto_id: str) -> list[dict]:
        """Bounded task subset an input may align to: REQ tasks under its TTO with a role task for its role."""
        return [t for t in self.req_tasks.values()
                if t["tto_id"] == tto_id and self.role_task_for(t["req_task_id"], role_id)]

    def contract_req_tasks(self, contract_id: str, exclude: Iterable[str] = ()) -> list[dict]:
        """Every REQ task on the contract (all TTOs, all work roles), less `exclude` (e.g. the employee's own REQ)."""
        skip = set(exclude)
        return [t for t in self.req_tasks.values() if t["req_task_id"] not in skip
                and self.ttos.get(t["tto_id"], {}).get("contract_id") == contract_id]

    def reqs_containing(self, req_task_id: str) -> list[str]:
        """Work-role REQs (role_req_id) that list this task."""
        return sorted(r["role_req_id"] for r in self.role_reqs.values() if req_task_id in r["req_task_ids"])

    def delivery_owner_for(self, req_task_id: str) -> str | None:
        """Named Delivery reviewer for an evidence gap: the task order's technical director, if a known user."""
        req = self.req_tasks.get(req_task_id)
        td = self.ttos.get(req["tto_id"], {}).get("technical_director_id") if req else None
        return td if td in self.users else None

    # ------------------------------------------------------------ authority and membership

    def employee_reviewer_for(self, subject_id: str) -> str | None:
        s = self.subjects.get(subject_id)
        return s["employee_reviewer_id"] if s else None

    def is_audience_member(self, user_id: str, audience_id: str) -> bool:
        a = self.audiences.get(audience_id)
        return bool(a) and user_id in a["members"]

    def purpose_allowed(self, purpose: str) -> tuple[bool, str]:
        p = self.purposes.get(purpose)
        if p is None:
            return False, f"unknown purpose {purpose}"
        return bool(p["allowed"]), p.get("reason", "")

    def human_users(self, domain: str | None = None, role: str | None = None) -> Iterable[dict]:
        for u in self.users.values():
            if u["kind"] != "human":
                continue
            if domain and u["domain"] != domain:
                continue
            if role and role not in u.get("roles", []):
                continue
            yield u
