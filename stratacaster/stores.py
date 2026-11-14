"""Permission-domain stores.

Each store is a separate SQLite file. Every method takes the calling Principal and checks it
before touching data, so Delivery and Solutions are separate permission domains even on one
development machine:

  delivery.db  - raw MSR inputs, review events, atomic statements, alignment runs (Delivery only)
  evidence.db  - approved-evidence repository: filtered records, approval/publication events
                 (written by Delivery; read only by Delivery or the retrieval gateway service)
  ops.db       - escalations and Solutions responses (no raw content)
  audit.db     - append-only audit trail (sanitized; raw content rejected)
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from .clock import Clock
from .registry import Principal
from .schemas import DELIVERY_ONLY_FIELDS, FilteredEvidence, MsrInput, PmoApprovalEvent


class PermissionDenied(Exception):
    pass


class _Db:
    def __init__(self, path: Path, ddl: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(ddl)

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, tuple(params))

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchone()

    def close(self) -> None:
        self.conn.close()


def _append_only(table: str) -> str:
    return f"""
    CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table}
      BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;
    CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table}
      BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;
    """


def find_delivery_only_keys(obj: Any, path: str = "") -> list[str]:
    """Return paths of any Delivery-only field names found at any nesting level."""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if k in DELIVERY_ONLY_FIELDS:
                hits.append(p)
            hits.extend(find_delivery_only_keys(v, p))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            hits.extend(find_delivery_only_keys(v, f"{path}[{i}]"))
    return hits


# ===================================================================== audit

class AuditLog:
    DDL = """
    CREATE TABLE IF NOT EXISTS audit (
      seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, correlation_id TEXT, actor TEXT NOT NULL,
      domain TEXT, action TEXT NOT NULL, outcome TEXT NOT NULL, reason TEXT, refs TEXT, meta TEXT);
    """ + _append_only("audit")

    def __init__(self, path: Path, clock: Clock):
        self.db = _Db(path, self.DDL)
        self.clock = clock

    def record(self, actor: Principal | str, action: str, outcome: str, reason: str = "",
               correlation_id: str = "", refs: dict | None = None, meta: dict | None = None) -> None:
        refs, meta = refs or {}, meta or {}
        leaked = find_delivery_only_keys(refs) + find_delivery_only_keys(meta)
        if leaked:
            raise ValueError(f"refusing to log Delivery-only fields: {leaked}")
        actor_id = actor.user_id if isinstance(actor, Principal) else actor
        domain = actor.domain if isinstance(actor, Principal) else ""
        if isinstance(actor, Principal) and actor.active_role:
            meta = {**meta, "acting_role": actor.active_role}
        self.db.execute(
            "INSERT INTO audit (ts, correlation_id, actor, domain, action, outcome, reason, refs, meta) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (self.clock.now_str(), correlation_id, actor_id, domain, action, outcome, reason,
             json.dumps(refs, sort_keys=True), json.dumps(meta, sort_keys=True)))

    def entries(self, correlation_id: str | None = None, limit: int = 500) -> list[dict]:
        if correlation_id:
            rows = self.db.all("SELECT * FROM audit WHERE correlation_id=? ORDER BY seq DESC LIMIT ?",
                               (correlation_id, limit))
        else:
            rows = self.db.all("SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            d["refs"] = json.loads(d["refs"] or "{}")
            d["meta"] = json.loads(d["meta"] or "{}")
            out.append(d)
        return out


class _GuardedStore:
    """Base class: every public method must call _require() with the caller's principal."""
    store_name = "store"

    def __init__(self, audit: AuditLog):
        self.audit = audit

    def _require(self, principal: Principal, *, domains: set[str], action: str, roles: set[str] | None = None):
        ok = principal.domain in domains and (roles is None or bool(roles & set(principal.roles)))
        if not ok:
            self.audit.record(principal, f"{self.store_name}.{action}", "denied",
                              reason=f"domain {principal.domain!r} not permitted on {self.store_name}")
            raise PermissionDenied(f"{principal.user_id} ({principal.domain}) may not {action} on {self.store_name}")


# ===================================================================== Delivery source store

class DeliveryStore(_GuardedStore):
    store_name = "delivery_store"
    DDL = """
    CREATE TABLE IF NOT EXISTS msr_inputs (
      input_id TEXT NOT NULL, input_version INTEGER NOT NULL, body TEXT NOT NULL,
      PRIMARY KEY (input_id, input_version));
    CREATE TABLE IF NOT EXISTS review_events (
      seq INTEGER PRIMARY KEY AUTOINCREMENT, input_id TEXT, input_version INTEGER, body TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS statements (
      statement_id TEXT PRIMARY KEY, input_id TEXT, input_version INTEGER, text TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS alignment_runs (
      run_id TEXT PRIMARY KEY, input_id TEXT, input_version INTEGER, created_at TEXT, body TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS tto_reviews (
      seq INTEGER PRIMARY KEY AUTOINCREMENT, input_id TEXT, input_version INTEGER, body TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS req_plans (
      seq INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT UNIQUE, role_req_id TEXT, period TEXT, body TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS req_plan_decisions (
      seq INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT, body TEXT NOT NULL);
    """ + _append_only("review_events") + _append_only("tto_reviews") + _append_only("req_plans") \
        + _append_only("req_plan_decisions")

    def __init__(self, path: Path, audit: AuditLog):
        super().__init__(audit)
        self.db = _Db(path, self.DDL)
        self.visibility = None  # DeliveryVisibility; set by the Environment

    def _d(self, p: Principal, action: str):
        self._require(p, domains={"delivery"}, action=action)

    def _sees(self, p: Principal, msr: MsrInput) -> bool:
        return self.visibility is None or self.visibility.can_see(p, msr.delivery_scope_id, msr.subject_id,
                                                                  msr.tto_id)

    def _require_row(self, p: Principal, msr: MsrInput, action: str) -> None:
        if not self._sees(p, msr):
            self.audit.record(p, f"{self.store_name}.{action}", "denied",
                              reason="row not visible: not the subject's reviewer, an in-scope operator, the TTO's "
                                     "lead, or the contract's PMO reviewer", refs={"input_id": msr.input_id})
            raise PermissionDenied(f"{p.user_id} is not authorized to see {msr.input_id}")

    def _subject_of(self, input_id: str) -> str | None:
        """Delivery-internal lookup used by the visibility policy; returns no content."""
        row = self.db.one("SELECT body FROM msr_inputs WHERE input_id=? ORDER BY input_version DESC LIMIT 1",
                          (input_id,))
        return json.loads(row["body"]).get("subject_id") if row else None

    def _latest(self, input_id: str) -> MsrInput | None:
        row = self.db.one("SELECT body FROM msr_inputs WHERE input_id=? ORDER BY input_version DESC LIMIT 1",
                          (input_id,))
        return MsrInput.model_validate_json(row["body"]) if row else None

    # -- inputs
    def get_input(self, p: Principal, input_id: str, version: int | None = None) -> MsrInput | None:
        self._d(p, "get_input")
        if version is None:
            row = self.db.one("SELECT body FROM msr_inputs WHERE input_id=? ORDER BY input_version DESC LIMIT 1",
                              (input_id,))
        else:
            row = self.db.one("SELECT body FROM msr_inputs WHERE input_id=? AND input_version=?",
                              (input_id, version))
        msr = MsrInput.model_validate_json(row["body"]) if row else None
        if msr is not None:
            self._require_row(p, msr, "get_input")
        return msr

    def list_inputs(self, p: Principal, latest_only: bool = True) -> list[MsrInput]:
        """Returns only the inputs this principal is authorized to see."""
        self._d(p, "list_inputs")
        rows = self.db.all("SELECT body FROM msr_inputs ORDER BY input_id, input_version")
        items = [MsrInput.model_validate_json(r["body"]) for r in rows]
        if latest_only:
            latest: dict[str, MsrInput] = {}
            for i in items:
                latest[i.input_id] = i
            items = list(latest.values())
        return [i for i in items if self._sees(p, i)]

    def _require_input_visible(self, p: Principal, input_id: str, action: str) -> None:
        msr = self._latest(input_id)
        if msr is not None:
            self._require_row(p, msr, action)

    def insert_input(self, p: Principal, msr: MsrInput) -> None:
        self._d(p, "insert_input")
        self.db.execute("INSERT INTO msr_inputs (input_id, input_version, body) VALUES (?,?,?)",
                        (msr.input_id, msr.input_version, msr.model_dump_json()))

    def update_input_metadata(self, p: Principal, msr: MsrInput) -> None:
        """Updates review/source-status metadata on an existing version (narrative is immutable)."""
        self._d(p, "update_input_metadata")
        existing = self.get_input(p, msr.input_id, msr.input_version)
        if existing is None or existing.raw_narrative != msr.raw_narrative:
            raise ValueError("narrative changes require a new input version")
        self.db.execute("UPDATE msr_inputs SET body=? WHERE input_id=? AND input_version=?",
                        (msr.model_dump_json(), msr.input_id, msr.input_version))

    # -- review events
    def append_review_event(self, p: Principal, event: dict) -> None:
        self._d(p, "append_review_event")
        self.db.execute("INSERT INTO review_events (input_id, input_version, body) VALUES (?,?,?)",
                        (event["input_id"], event["input_version"], json.dumps(event)))

    def review_events(self, p: Principal, input_id: str) -> list[dict]:
        self._d(p, "review_events")
        self._require_input_visible(p, input_id, "review_events")
        return [json.loads(r["body"]) for r in
                self.db.all("SELECT body FROM review_events WHERE input_id=? ORDER BY seq", (input_id,))]

    # -- TTO lead reviews (append-only; one decision per input version)
    def append_tto_review(self, p: Principal, event: dict) -> None:
        self._d(p, "append_tto_review")
        self.db.execute("INSERT INTO tto_reviews (input_id, input_version, body) VALUES (?,?,?)",
                        (event["input_id"], event["input_version"], json.dumps(event)))

    def tto_review(self, p: Principal, input_id: str, version: int) -> dict | None:
        self._d(p, "tto_review")
        self._require_input_visible(p, input_id, "tto_review")
        return self._tto_latest(input_id, version)

    # -- REQ-change plans (TTO Coordination Agent pilot; Delivery-only, append-only)
    def append_req_plan(self, p: Principal, plan: dict) -> None:
        self._d(p, "append_req_plan")
        self.db.execute("INSERT INTO req_plans (plan_id, role_req_id, period, body) VALUES (?,?,?,?)",
                        (plan["plan_id"], plan["role_req_id"], plan["period"], json.dumps(plan)))

    def req_plans(self, p: Principal, role_req_id: str | None = None) -> list[dict]:
        self._d(p, "req_plans")
        rows = (self.db.all("SELECT body FROM req_plans WHERE role_req_id=? ORDER BY seq", (role_req_id,))
                if role_req_id else self.db.all("SELECT body FROM req_plans ORDER BY seq"))
        return [json.loads(r["body"]) for r in rows]

    def append_req_plan_decision(self, p: Principal, decision: dict) -> None:
        self._d(p, "append_req_plan_decision")
        self.db.execute("INSERT INTO req_plan_decisions (plan_id, body) VALUES (?,?)",
                        (decision["plan_id"], json.dumps(decision)))

    def req_plan_decision(self, plan_id: str, stage: str) -> dict | None:
        """Delivery-internal status lookup (decision metadata only): the latest decision at one stage
        ('tto' endorsement or 'pmo' approval)."""
        rows = self.db.all("SELECT body FROM req_plan_decisions WHERE plan_id=? ORDER BY seq DESC", (plan_id,))
        return next((b for b in (json.loads(r["body"]) for r in rows) if b.get("stage") == stage), None)

    def _tto_latest(self, input_id: str, version: int) -> dict | None:
        """Delivery-internal status lookup (decision metadata only)."""
        row = self.db.one("SELECT body FROM tto_reviews WHERE input_id=? AND input_version=? ORDER BY seq DESC LIMIT 1",
                          (input_id, version))
        return json.loads(row["body"]) if row else None

    # -- statements
    def put_statements(self, p: Principal, input_id: str, version: int, statements: list[tuple[str, str]]) -> None:
        self._d(p, "put_statements")
        for sid, text in statements:
            self.db.execute("INSERT OR REPLACE INTO statements (statement_id, input_id, input_version, text) "
                            "VALUES (?,?,?,?)", (sid, input_id, version, text))

    def get_statements(self, p: Principal, input_id: str, version: int) -> list[tuple[str, str]]:
        self._d(p, "get_statements")
        self._require_input_visible(p, input_id, "get_statements")
        return [(r["statement_id"], r["text"]) for r in self.db.all(
            "SELECT statement_id, text FROM statements WHERE input_id=? AND input_version=? ORDER BY statement_id",
            (input_id, version))]

    # -- alignment runs (Delivery-only intermediate content)
    def put_alignment_run(self, p: Principal, run_id: str, input_id: str, version: int, created_at: str, body: dict):
        self._d(p, "put_alignment_run")
        self.db.execute("INSERT INTO alignment_runs (run_id, input_id, input_version, created_at, body) "
                        "VALUES (?,?,?,?,?)", (run_id, input_id, version, created_at, json.dumps(body)))

    def alignment_runs(self, p: Principal, input_id: str | None = None) -> list[dict]:
        self._d(p, "alignment_runs")
        if input_id:
            self._require_input_visible(p, input_id, "alignment_runs")
            rows = self.db.all("SELECT input_id, body FROM alignment_runs WHERE input_id=? ORDER BY created_at",
                               (input_id,))
        else:
            rows = self.db.all("SELECT input_id, body FROM alignment_runs ORDER BY created_at")
        return [json.loads(r["body"]) for r in rows
                if (m := self._latest(r["input_id"])) is None or self._sees(p, m)]


# ===================================================================== Approved-evidence repository

class EvidenceStore(_GuardedStore):
    store_name = "evidence_store"
    DDL = """
    CREATE TABLE IF NOT EXISTS evidence (
      record_id TEXT NOT NULL, record_version INTEGER NOT NULL, body TEXT NOT NULL,
      PRIMARY KEY (record_id, record_version));
    CREATE TABLE IF NOT EXISTS evidence_history (
      seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, record_id TEXT, record_version INTEGER,
      action TEXT, body TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS approval_events (
      event_id TEXT PRIMARY KEY, record_id TEXT, record_version INTEGER, received_at TEXT, body TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS approval_processing (
      seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, event_id TEXT, target_record_id TEXT,
      target_record_version INTEGER, outcome TEXT, reason TEXT, computed_snapshot TEXT);
    CREATE TABLE IF NOT EXISTS publication_events (
      seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, record_id TEXT, record_version INTEGER,
      approval_event_id TEXT, actor TEXT, outcome TEXT, reason TEXT, disposition TEXT);
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    INSERT OR IGNORE INTO meta (key, value) VALUES ('generation', '0');
    INSERT OR IGNORE INTO meta (key, value) VALUES ('record_seq', '0');
    """ + _append_only("evidence_history") + _append_only("approval_events") \
        + _append_only("approval_processing") + _append_only("publication_events")

    READERS = {"delivery", "shared"}

    def __init__(self, path: Path, audit: AuditLog, clock: Clock):
        super().__init__(audit)
        self.db = _Db(path, self.DDL)
        self.clock = clock
        self.visibility = None  # DeliveryVisibility; set by the Environment

    def _sees(self, p: Principal, rec: FilteredEvidence) -> bool:
        """Delivery principals see only records derived from MSRs they may see; the gateway checks separately."""
        return p.domain != "delivery" or self.visibility is None or self.visibility.can_see_record(p, rec)

    def _w(self, p: Principal, action: str):
        self._require(p, domains={"delivery"}, action=action)

    def _r(self, p: Principal, action: str):
        # Solutions principals are never readers; they must go through the retrieval gateway.
        if p.domain == "shared" and not p.has_role("retrieval_gateway"):
            self._require(p, domains=set(), action=action)
        self._require(p, domains=self.READERS, action=action)

    def _bump(self) -> None:
        self.db.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key='generation'")

    def generation(self, p: Principal) -> int:
        self._r(p, "generation")
        return int(self.db.one("SELECT value FROM meta WHERE key='generation'")["value"])

    def next_record_id(self, p: Principal, prefix: str = "SYN-EVID") -> str:
        self._w(p, "next_record_id")
        self.db.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key='record_seq'")
        n = int(self.db.one("SELECT value FROM meta WHERE key='record_seq'")["value"])
        return f"{prefix}-{n:03d}"

    # -- records
    def put_record(self, p: Principal, rec: FilteredEvidence, action: str) -> None:
        """Insert or replace one record version and append a history snapshot."""
        self._w(p, "put_record")
        body = rec.model_dump_json()
        leaked = find_delivery_only_keys(json.loads(body))
        if leaked:
            raise PermissionDenied(f"Delivery-only fields may not enter the evidence store: {leaked}")
        self.db.execute("INSERT OR REPLACE INTO evidence (record_id, record_version, body) VALUES (?,?,?)",
                        (rec.record_id, rec.record_version, body))
        self.db.execute("INSERT INTO evidence_history (ts, record_id, record_version, action, body) "
                        "VALUES (?,?,?,?,?)", (self.clock.now_str(), rec.record_id, rec.record_version, action, body))
        self._bump()

    def get_record(self, p: Principal, record_id: str, version: int | None = None) -> FilteredEvidence | None:
        self._r(p, "get_record")
        if version is None:
            row = self.db.one("SELECT body FROM evidence WHERE record_id=? ORDER BY record_version DESC LIMIT 1",
                              (record_id,))
        else:
            row = self.db.one("SELECT body FROM evidence WHERE record_id=? AND record_version=?",
                              (record_id, version))
        rec = FilteredEvidence.model_validate_json(row["body"]) if row else None
        return rec if rec is not None and self._sees(p, rec) else None

    def list_records(self, p: Principal) -> list[FilteredEvidence]:
        self._r(p, "list_records")
        return [rec for rec in self._all_records() if self._sees(p, rec)]

    def _all_records(self) -> list[FilteredEvidence]:
        return [FilteredEvidence.model_validate_json(r["body"])
                for r in self.db.all("SELECT body FROM evidence ORDER BY record_id, record_version")]

    def history(self, p: Principal, record_id: str) -> list[dict]:
        self._r(p, "history")
        return [dict(r) for r in self.db.all(
            "SELECT seq, ts, record_id, record_version, action FROM evidence_history WHERE record_id=? ORDER BY seq",
            (record_id,))]

    # -- approval events (append-only)
    def get_approval_event(self, p: Principal, event_id: str) -> PmoApprovalEvent | None:
        self._r(p, "get_approval_event")
        row = self.db.one("SELECT body FROM approval_events WHERE event_id=?", (event_id,))
        return PmoApprovalEvent.model_validate_json(row["body"]) if row else None

    def append_approval_event(self, p: Principal, event: PmoApprovalEvent) -> None:
        self._w(p, "append_approval_event")
        self.db.execute("INSERT INTO approval_events (event_id, record_id, record_version, received_at, body) "
                        "VALUES (?,?,?,?,?)", (event.event_id, event.record_id, event.record_version,
                                               self.clock.now_str(), event.model_dump_json()))
        self._bump()

    def approval_events_for(self, p: Principal, record_id: str, version: int | None = None) -> list[PmoApprovalEvent]:
        self._r(p, "approval_events_for")
        if version is None:
            rows = self.db.all("SELECT body FROM approval_events WHERE record_id=? ORDER BY received_at, rowid",
                               (record_id,))
        else:
            rows = self.db.all("SELECT body FROM approval_events WHERE record_id=? AND record_version=? "
                               "ORDER BY received_at, rowid", (record_id, version))
        return [PmoApprovalEvent.model_validate_json(r["body"]) for r in rows]

    def all_approval_events(self, p: Principal) -> list[PmoApprovalEvent]:
        self._r(p, "all_approval_events")
        return [PmoApprovalEvent.model_validate_json(r["body"])
                for r in self.db.all("SELECT body FROM approval_events ORDER BY received_at, rowid")]

    def append_processing(self, p: Principal, event_id: str, record_id: str, target_version: int,
                          outcome: str, reason: str, snapshot: dict) -> None:
        self._w(p, "append_processing")
        self.db.execute("INSERT INTO approval_processing (ts, event_id, target_record_id, target_record_version, "
                        "outcome, reason, computed_snapshot) VALUES (?,?,?,?,?,?,?)",
                        (self.clock.now_str(), event_id, record_id, target_version, outcome, reason,
                         json.dumps(snapshot)))

    def processing_log(self, p: Principal, event_id: str | None = None) -> list[dict]:
        self._r(p, "processing_log")
        if event_id:
            rows = self.db.all("SELECT * FROM approval_processing WHERE event_id=? ORDER BY seq", (event_id,))
        else:
            rows = self.db.all("SELECT * FROM approval_processing ORDER BY seq")
        return [dict(r) for r in rows]

    # -- publication events (append-only)
    def append_publication(self, p: Principal, record_id: str, version: int, approval_event_id: str,
                           outcome: str, reason: str, disposition: str) -> None:
        self._w(p, "append_publication")
        self.db.execute("INSERT INTO publication_events (ts, record_id, record_version, approval_event_id, actor, "
                        "outcome, reason, disposition) VALUES (?,?,?,?,?,?,?,?)",
                        (self.clock.now_str(), record_id, version, approval_event_id, p.user_id, outcome, reason,
                         disposition))

    def publications(self, p: Principal, record_id: str | None = None) -> list[dict]:
        self._r(p, "publications")
        if record_id:
            rows = self.db.all("SELECT * FROM publication_events WHERE record_id=? ORDER BY seq", (record_id,))
        else:
            rows = self.db.all("SELECT * FROM publication_events ORDER BY seq")
        return [dict(r) for r in rows]


# ===================================================================== Operations store

class OpsStore:
    """Escalations and Solutions responses. Holds identifiers and filtered content only."""
    DDL = """
    CREATE TABLE IF NOT EXISTS escalations (
      escalation_id TEXT PRIMARY KEY, created_at TEXT, correlation_id TEXT, kind TEXT, route_to TEXT,
      route_domain TEXT, status TEXT, summary TEXT, refs TEXT, resolved_by TEXT, resolved_at TEXT, resolution TEXT);
    CREATE TABLE IF NOT EXISTS responses (
      query_id TEXT PRIMARY KEY, created_at TEXT, correlation_id TEXT, requester_id TEXT, status TEXT,
      body TEXT, acknowledged_by TEXT, acknowledged_at TEXT);
    """

    def __init__(self, path: Path, clock: Clock):
        self.db = _Db(path, self.DDL)
        self.clock = clock

    def add_escalation(self, escalation_id: str, correlation_id: str, kind: str, route_to: str,
                       route_domain: str, summary: str, refs: dict) -> str:
        if find_delivery_only_keys(refs):
            raise ValueError("escalations may not carry Delivery-only fields")
        self.db.execute("INSERT INTO escalations (escalation_id, created_at, correlation_id, kind, route_to, "
                        "route_domain, status, summary, refs) VALUES (?,?,?,?,?,?,?,?,?)",
                        (escalation_id, self.clock.now_str(), correlation_id, kind, route_to, route_domain,
                         "open", summary, json.dumps(refs)))
        return escalation_id

    def escalations(self, route_to: str | None = None, status: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM escalations WHERE 1=1", []
        if route_to:
            sql += " AND route_to=?"
            params.append(route_to)
        if status:
            sql += " AND status=?"
            params.append(status)
        rows = self.db.all(sql + " ORDER BY created_at, escalation_id", params)
        out = []
        for r in rows:
            d = dict(r)
            d["refs"] = json.loads(d["refs"] or "{}")
            out.append(d)
        return out

    def resolve_escalation(self, escalation_id: str, resolved_by: str, resolution: str) -> None:
        self.db.execute("UPDATE escalations SET status='resolved', resolved_by=?, resolved_at=?, resolution=? "
                        "WHERE escalation_id=?", (resolved_by, self.clock.now_str(), resolution, escalation_id))

    def put_response(self, query_id: str, correlation_id: str, requester_id: str, status: str, body: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO responses (query_id, created_at, correlation_id, requester_id, "
                        "status, body) VALUES (?,?,?,?,?,?)",
                        (query_id, self.clock.now_str(), correlation_id, requester_id, status, json.dumps(body)))

    def get_response(self, query_id: str) -> dict | None:
        r = self.db.one("SELECT * FROM responses WHERE query_id=?", (query_id,))
        if not r:
            return None
        d = dict(r)
        d["body"] = json.loads(d["body"])
        return d

    def responses(self) -> list[dict]:
        out = []
        for r in self.db.all("SELECT * FROM responses ORDER BY created_at DESC"):
            d = dict(r)
            d["body"] = json.loads(d["body"])
            out.append(d)
        return out

    def acknowledge(self, query_id: str, user_id: str, status: str) -> None:
        self.db.execute("UPDATE responses SET status=?, acknowledged_by=?, acknowledged_at=? WHERE query_id=?",
                        (status, user_id, self.clock.now_str(), query_id))
