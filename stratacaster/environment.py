"""Wires the prototype environment: config, clock, stores, agents, services, and data loading."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path

from . import PROTOTYPE_VERSION
from .agents import AgentRegistry, RoleExecutionAgent, SolutionsExpertiseAgent, TTOCoordinationAgent
from .clock import Clock
from .delivery import DeliveryService, SourceStatusService
from .feedback_check import LocalAligner
from .llm import make_llm
from .presentation import FEED_FILE, PresentationFeed, enabled_by_env
from .registry import DeliveryVisibility, ReferenceRegistry
from .req_planner import ReqPlanner
from .solutions import RetrievalGateway, SolutionsOrchestrator
from .stores import AuditLog, DeliveryStore, EvidenceStore, OpsStore

ROOT = Path(__file__).resolve().parent.parent


def _wipe(var_dir: Path) -> None:
    """Deletes the synthetic stores. If another process (e.g. the review app) holds a file open, which
    blocks deletion on Windows, drops its tables instead; the stores recreate them on next use."""
    for f in var_dir.glob("*.db"):
        try:
            f.unlink()
        except PermissionError:
            con = sqlite3.connect(f)
            try:
                tables = [r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                for t in tables:
                    con.execute(f'DROP TABLE IF EXISTS "{t}"')
                if con.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
                    con.execute("DELETE FROM sqlite_sequence")
                con.commit()
            finally:
                con.close()
    shutil.rmtree(var_dir, ignore_errors=True)


class Environment:
    def __init__(self, root: Path = ROOT, var_dir: Path | None = None, *, fixed_clock: str | None = "config",
                 llm=None, reset: bool = False, dataset: str | None = None):
        self.root = Path(root)
        self.cfg = json.loads((self.root / "config" / "prototype_config.json").read_text(encoding="utf-8"))
        paths = self.cfg["paths"]
        self.dataset = dataset or os.environ.get("STRATACASTER_DATASET") or self.cfg["dataset"]
        self.dataset_paths = self.cfg["datasets"][self.dataset]
        var_dir = var_dir or os.environ.get("STRATACASTER_VAR_DIR")
        self.var_dir = Path(var_dir) if var_dir else self.root / paths["var_dir"]
        if reset and self.var_dir.exists():
            _wipe(self.var_dir)
        self.var_dir.mkdir(parents=True, exist_ok=True)

        self.registry = ReferenceRegistry.load(self.root / self.dataset_paths["registry"])
        start = self.registry.demo.get("clock_start") or self.cfg["clock"]["fixed"]
        self.clock = Clock(start if fixed_clock == "config" else fixed_clock)
        self.agent_registry = AgentRegistry(self.root / paths["agent_registry"])
        self.llm = llm or make_llm(self.cfg["llm"])

        self.audit = AuditLog(self.var_dir / "audit.db", self.clock)
        self.delivery_store = DeliveryStore(self.var_dir / "delivery.db", self.audit)
        self.evidence_store = EvidenceStore(self.var_dir / "evidence.db", self.audit, self.clock)
        self.ops = OpsStore(self.var_dir / "ops.db", self.clock)
        visibility = DeliveryVisibility(self.registry, self.delivery_store._subject_of)
        self.delivery_store.visibility = self.evidence_store.visibility = visibility
        self.source_status = SourceStatusService(self.delivery_store)

        prompts = self.root / paths["prompts"]
        attempts = self.cfg["llm"]["max_attempts"]
        self.role_agent = RoleExecutionAgent(self.llm, self.agent_registry, prompts, attempts)
        self.expertise_agent = SolutionsExpertiseAgent(self.llm, self.agent_registry, prompts, attempts)
        self.coordination_agent = TTOCoordinationAgent(self.llm, self.agent_registry, prompts, attempts)

        # Employee feedback check before submission (PR-30): free local engine by default; the hybrid deployment
        # option adds a small-model agent that only writes recommended new tasks.
        fb = self.cfg["feedback_check"]
        self.feedback_mode = os.environ.get("STRATACASTER_FEEDBACK_MODE", fb["mode"]).lower()
        self.local_aligner = LocalAligner(fb["similarity_threshold"], fb["max_tasks_per_statement"],
                                          fb["elsewhere_floor"])
        self.suggest_agent = None
        if self.feedback_mode == "hybrid":
            small = llm or make_llm(self.cfg["llm"] | fb["hybrid_llm"])
            self.suggest_agent = RoleExecutionAgent(small, self.agent_registry, prompts, attempts)

        # Delivery side gets the Delivery store; Solutions side never does.
        self.delivery = DeliveryService(registry=self.registry, store=self.delivery_store,
                                        evidence=self.evidence_store, ops=self.ops, audit=self.audit,
                                        clock=self.clock, cfg=self.cfg, role_agent=self.role_agent,
                                        feedback_mode=self.feedback_mode, local_aligner=self.local_aligner,
                                        suggest_agent=self.suggest_agent)
        # REQ-change planning (Tree of Thought pilot, CR-SC-2026-003): Delivery-side, human-decided.
        self.req_planner = ReqPlanner(registry=self.registry, store=self.delivery_store, delivery=self.delivery,
                                      ops=self.ops, audit=self.audit, clock=self.clock, agent=self.coordination_agent)
        self.gateway = RetrievalGateway(evidence=self.evidence_store, source_status=self.source_status,
                                        registry=self.registry, audit=self.audit, clock=self.clock)
        self.orchestrator = SolutionsOrchestrator(gateway=self.gateway, expertise=self.expertise_agent,
                                                  registry=self.registry, agents=self.agent_registry, ops=self.ops,
                                                  audit=self.audit, clock=self.clock, cfg=self.cfg)
        self.orchestrator._trace_dir = self.var_dir / "traces"  # semantic search traces (checkpoint 3.1)
        # Demo-only mirror of the employee's pre-submission dialogue for the Workflow Monitor; off unless enabled.
        self.presentation = PresentationFeed(self.var_dir / FEED_FILE, enabled=enabled_by_env())

    def principal(self, user_id: str, as_role: str | None = None):
        return self.registry.principal(user_id, as_role)

    def msr_cases(self, scope: str = "demo") -> list[dict]:
        """All cases, or (scope="demo") the dataset's bounded demo slice plus every test case."""
        cases = json.loads((self.root / self.dataset_paths["msr_cases"]).read_text(encoding="utf-8"))["cases"]
        demo = self.registry.demo.get("scope")
        if scope != "demo" or not demo:
            return cases
        return [c for c in cases if c.get("category") != "roster-msr"
                or (c["contract_id"] in demo["contracts"] and c["month"] in demo["months"])]

    def load_all_cases(self, operator_id: str | None = None, scope: str = "demo") -> list[dict]:
        """Loads cases as a Delivery operator: the given one, or the in-scope operator for each case's contract."""
        results = []
        for case in self.msr_cases(scope):
            op_id = operator_id or self.registry.operator_for(case["input"]["delivery_scope_id"])
            out = self.delivery.load_input(self.principal(op_id), case["input"],
                                           correlation_id=f"{self.registry.id_prefix}-LOAD-{case['case_id']}")
            results.append({"case_id": case["case_id"], "ok": out.ok, "reason": out.reason,
                            "errors": out.data.get("errors", [])})
        return results

    def versions(self) -> dict:
        return {"prototype": PROTOTYPE_VERSION, "config": self.cfg["config_version"], "dataset": self.dataset,
                "reference_fixture": self.registry.version, "agent_registry": self.agent_registry.version,
                "llm_mode": self.llm.mode, "model": self.llm.model, "feedback_check": self.feedback_mode,
                "prompts": {a.agent_id: a.prompt_version
                            for a in (self.role_agent, self.expertise_agent, self.coordination_agent)}}

    def close(self) -> None:
        for s in (self.audit, self.delivery_store, self.evidence_store, self.ops):
            s.db.close()

