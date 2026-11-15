# StrataCaster: a governed, autonomous multi-agent system for workforce evidence

**StrataCaster** turns the monthly status reports (MSRs) that employees already write into **governed, requirement-aligned evidence** that every part of an organization can use. AI agents run the workflow from end to end: they read each report against the employee's role requirements, draft evidence, answer requests and plan requirement changes. They **stop only at the gates where a person must decide**. Raw reports never leave the Delivery domain; other teams see only evidence that a human has approved.

This repository is **Prototype 1 (Delivery Evidence Core)**, my capstone for the Carnegie Mellon University Agentic AI program.

> **Synthetic data only.** Every person, contract and report here is fictional (a notional 270-employee organization). No real people, employers, customers or credentials. The system is advisory: it makes no employment decisions.

## The problem and the user

In a services company, employees write monthly reports, and managers read them, each through the lens of one team. Delivery asks whether the work meets the requirement; Growth asks who has done this before; Strategy asks where the work is going; the employee asks how their work is seen. The reports hold the answers, but no single reader can combine the views, and asking program office by program office never adds up to a shared picture.

**Intended users:** employees (feedback on their own report), team leads (TTO leads) and program offices (PMOs), who review and approve, and Solutions and Growth staff, who need cited evidence of expertise without ever seeing raw reports.

## Goal and scope

**Goal:** an autonomous pipeline that aligns each report statement to the employee's role requirements (REQ tasks), routes it through human approval, and publishes only minimized, approved evidence that downstream teams can query with citations.

**Success:**
- zero blocked records published;
- zero raw-report disclosures;
- correct alignment and coverage status against predeclared labels;
- every deny path enforced.

The thresholds were written down **before** the first live run (`data/eval/expected_outcomes.json`).

**Scope:** one contract-month (55 MSRs) in the demo slice, all 440 Delivery MSRs available. Simulated sign-in. No production use.

## Architecture

```
            DELIVERY permission domain                                SOLUTIONS permission domain
 ┌──────────────────────────────────────────────────┐          ┌─────────────────────────────────────┐
 │ delivery.db  raw MSRs, review events,            │          │ Solutions Orchestrator              │
 │              statements, alignment runs          │          │   ├─ validates requester/audience/  │
 │      ▲                                           │          │   │   purpose; denies raw requests  │
 │      │ DeliveryService (Workflow A)              │          │   ├─ RetrievalGateway ──────────────┼──┐
 │      │  ├─ employee feedback + review (human)    │          │   └─ Solutions Expertise Agent      │  │
 │      │  ├─ Role Execution Agent → REQ assessment │          │        (cited explanation only)     │  │
 │      │  ├─ content filter (minimization)         │          │ ops.db  responses, escalations      │  │
 │      │  ├─ TTO lead review (human gate)          │          └─────────────────────────────────────┘  │
 │      │  ├─ PMO approval event (human gate)       │                                                   │
 │      │  └─ publication (separate, rechecked)     │   reads only via gateway service identity         │
 │      ▼                                           │◄──────────────────────────────────────────────────┘
 │ evidence.db  filtered records, approval events,  │
 │              publication log (append-only)       │
 │ SourceStatusService → status metadata only ──────┼──► gateway (fails closed if unavailable)
 └──────────────────────────────────────────────────┘
 audit.db: append-only, correlated, rejects raw fields (all domains write; nothing edits)
```

**Two workflows, one system:**
- **Workflow A (publish evidence):** employee submits, with a free pre-submission feedback check → **Role Execution Agent** assesses each statement against the employee's REQ (aligned task, or *potential new task*), plus a contract-wide pass for similar work → content filter → **TTO lead review** → **PMO approval** (a recorded event bound to one record version) → separate **publication** step that rechecks every prerequisite.
- **Workflow B (retrieve and explain):** **Solutions Orchestrator** validates identity, audience and purpose → **Retrieval Gateway** returns only current, approved, permitted records → **Solutions Expertise Agent** writes a cited coverage explanation → gaps and conflicts escalate to named people → complete only after a person acknowledges.

**Agents:** the Role Execution Agent, Solutions Orchestrator and Solutions Expertise Agent, plus one pilot, the **TTO Coordination Agent**: REQ-change planning by Tree of Thought beam search, with TTO-lead endorsement and PMO approval. Agents hold no tools, permissions or approval authority. Deterministic policy code (`delivery.py`, `solutions.py`, `content_filter.py`) validates every model output before use and makes every safety decision.

**Memory and retrieval:** separate SQLite stores per domain, each checking the caller's principal on every method; an append-only audit log; a Chroma vector store (all-MiniLM-L6-v2) for semantic expertise search over requirement tasks and published records only, never raw text. Governance filters candidates **before** they are ranked.

## Run it

Requires Python 3.12. Everything below runs **offline** with a deterministic stand-in model; no API key needed.

```powershell
# Windows PowerShell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest -q                              # full test suite
.\.venv\Scripts\python.exe scripts\run_demo.py                       # scripted end-to-end run in the terminal
.\.venv\Scripts\python.exe -m streamlit run app\streamlit_app.py     # review app + Workflow and People monitors
.\.venv\Scripts\python.exe scripts\run_evaluation.py --executor "Your Name"   # writes a new run under evidence/runs/
```

```bash
# macOS / Linux
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pytest -q
python scripts/run_demo.py
streamlit run app/streamlit_app.py
```

The first semantic-search test downloads the embedding model (about 80 MB). `scripts/reset_env.py --load` resets the synthetic demo data.

**Model modes:** `config/prototype_config.json` → `llm.mode` (`auto`). With no credentials, the deterministic **mock** runs. With `ANTHROPIC_API_KEY` set, agents call **Claude** (`claude-opus-5-5`, structured outputs). Force either with `STRATACASTER_LLM_MODE=mock|claude`. A full live evaluation makes about 140 model calls and costs about $4.60.

**A quick tour:** run `scripts/run_demo.py --pace 2` with the **Workflow Monitor** page open (`http://localhost:8501/Workflow_Monitor`): each step lights as it executes; denied steps turn red. The **People Monitor** shows who is acting at each authority level.

## Evaluation and results

`scripts/run_evaluation.py` runs the test suite, then checks the system against the predeclared thresholds on two datasets: the Build Guide cases (12 labeled MSR inputs, including adversarial ones) and the 270-employee roster (10 adversarial test cases, 5 workflow checks). Each run is recorded under `evidence/runs/<stamp>/` (`run_record.json`, `summary.md`, traces).

| Recorded run | Mode | Result |
|---|---|---|
| `EVAL-20261031T211818Z` | Offline stand-in | **196/196 tests passed; 11/11 threshold checks met** |
| `EVAL-20261031T203347Z` | Live Claude | **10/10 applicable checks met**: 0 blocked records published, 0 raw-report disclosures, 10/10 test cases, 5/5 workflow checks, coverage-status accuracy 0.83; 138 model calls, about $4.60 |
| `EVAL-20261031T195018Z` | Live Claude (first run) | Safety held, but **3 checks not met**. Traced to two defects in my design (status inferred from summary wording; a check tied to mock keywords), fixed, then rerun as above |
| `EVAL-20261106T010846Z` | Offline stand-in | 232/232 tests, 11/11 checks, 7/7 feedback-check gates. Covers features added after 31 Oct (development evidence) |
| `EVAL-20261114T161516Z` | Offline stand-in | 232/232 tests, 11/11 checks on the current code (development evidence) |
| `EVAL-20261114T161848Z` | Live Claude, current agent | **9 of 10 checks met.** Alignment precision and recall 1.00, citations 1.00, 0 blocked records published, 0 disclosures, 10/10 test cases, 5/5 workflow checks. **Coverage-status accuracy 0.67, NOT MET** (bar 0.80): the Expertise Agent rated two supported requirements as *partial*, a cautious error that escalates to a person. Reported as is, not tuned away. 143 calls, about $5.27 |

Mock scores measure the **governance pipeline**, not model quality: the stand-in is keyword-based by design. The roster has no answer key, so I scored a fixed-seed sample of 60 live results by hand (`evidence/sample_review/20261114T161848Z/sample_review_completed.xlsx`): **new-work calls 27/28 correct (96%, ±7 points); alignments 24/30 (80%, ±14), below the 90% bar.** All six alignment misses matched general working habits (handoffs, documenting next checks) to deliverable tasks. Next fixes: a working-practice category in the assessment prompt, and a code rule for partial versus supported coverage.

## Safety, reliability and human oversight

- **Four human gates:** employee review, TTO lead review, PMO approval and separate publication. No agent can skip one, and approvals bind to a single record version (a changed record needs a new approval).
- **Boundary:** raw MSRs never leave Delivery; Solutions code is constructed without a Delivery store reference.
- **Deny paths** (each tested): raw-report requests, wrong audience, prohibited purposes, cross-PMO approval, restricted content, prompt injection, approval reuse across versions, revocation, expiration, source withdrawal, model failure, and status-service outage (fails closed).
- **Monitoring:** an append-only, correlated audit log drives the two live monitors; every semantic search writes a trace.
- **Fallbacks:** model failures and empty plans escalate to a person; nothing is published on error.

## Repository map

| Path | Contents |
|---|---|
| `stratacaster/` | Core system: schemas, Delivery and Solutions services, agents and model adapters, versioned prompts, content filter, stores, REQ planner, monitors |
| `app/` | Streamlit review app, Workflow Monitor, People Monitor |
| `scripts/` | Demo, evaluation, roster import, reset, retrieval trace, sample review, live smoke test; `governance_review.py` (a queued model-assisted review of the design corpus; its tests run offline, but running it needs the corpus, which is not included) |
| `tests/` | Pytest suite, one file per scenario group |
| `config/` | Prototype configuration and the agent registry (authorized vs not-yet-authorized agents) |
| `data/` | Synthetic fixtures, the 270-employee roster (`data/source/` workbook, `data/fixtures/roster/` import), predeclared labels and thresholds, data manifest |
| `evidence/` | Recorded evaluation runs, retrieval and planning traces, smoke tests, sample-review sheets |
| `docs/` | Requirement traceability, roadmap, deployment notes for the feedback check |

The design is specified in a twelve-volume operating-model corpus that is not included here; `docs/traceability.md` maps each prototype requirement to its component, workflow step, safeguard and test.

## Limitations and next steps

- **Data:** synthetic only, with simulated sign-in; a 270-person notional organization.
- **Model judgment:** a hand-scored sample of 60 live results (14 Nov) found new-work calls right 27 of 28 times but alignments right only 24 of 30, below the 90% bar; all six misses matched general working habits to deliverables. The current agent prompt (`role_execution_v3`) has one recorded live run, `EVAL-20261114T161848Z` (9 of 10 checks; coverage-status accuracy 0.67).
- **Scope:** one of five planned increments. Next come Delivery Completion (a PMO/Contract agent, versioned REQs), Strategy (lines-of-effort tracking), Growth and Foundry, and **Scala Lucida** (career pathways from approved evidence, gated on governance decisions). See `docs/roadmap.md`.
- No production-readiness, legal-compliance or institutional-approval claim is made.

## Author

David L. Louque, Jr. Concept, design and build. Developed with AI pair-programming assistance (Claude Code). Requirements, design decisions and every approval recorded here are my own.
