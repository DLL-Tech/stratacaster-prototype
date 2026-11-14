# Future capabilities (not in the prototype)

Recorded 2026-10-30 at the product owner's request (Dave Louque) so they are not lost. FC-01 is not built in prototype 0.4.0; FC-02 was started on 2026-10-31 as a pilot, and FC-03 (added 2026-11-01) has tooling built but is queued for a live run (see below). Each entry says why, what the prototype already provides, and when to implement it.

## Build plan: five prototype increments and a production pilot

Recorded 2026-11-01 at the product owner's direction. The plan is in the corpus as change request CR-SC-2026-005: Volume 01 Section 1.1 controls the increments and gates, and Volume 08 Sections 2.3 and 6.1 cover agent authorization by increment. The volume owner accepted the change request on 2026-11-01; controlled-release approval is pending. The full plan, with a charter per increment, is in `Documentation/2 Prototype/00 — StrataCaster Build Plan and Document Structure.docx`.

Increments are grouped by domain and data, not one per agent. Each adds one new set of data and governance decisions, reuses what is proven, and ends with a proceed, revise, or stop decision recorded in its gate record (P<n>.08). Risk rises along the path.

| # | Increment | Agents added | Entry criteria | Exit criteria |
|---|---|---|---|---|
| 1 | Delivery Evidence Core (built, 0.4.0) | Role Execution, Solutions Orchestrator, Solutions Expertise, TTO Coordination (pilot) | — | Recorded evaluation met (196/196 tests; 11/11 mock; 10/10 live Claude); Cadre decision in P1.08; CR-SC-2026-004 accepted |
| 2 | Delivery Completion | PMO/Contract Agent | Prototype 1 proceed; its corpus CRs accepted | Contract-extraction precision and recall met on a labeled set; REQ versioning tests (FC-02 completed); live evaluation; P2.08 |
| 3 | Strategy | Strategy/TD Tracking Agent | Prototype 2 proceed (or a decision to run 3 first) | MOP calculation tests; strategy evaluation set; disclosure and aggregation tests; P3.08 |
| 4 | Growth and Foundry | Growth Discovery Agent; Foundry Build/Validation Agent | Prototype 3 proceed | Bid, pricing, and customer-contact deny tests; sandbox containment tests; Growth evaluation set; P4.08 |
| 5 | Career Pathways (Scala Lucida, FC-01) | Scala Lucida, with its own authorization | Prototype 4 proceed; the operating-model change resolving PR-12 approved | Fairness review; consent, notice, and access tests; HR human-review gates; P5.08 |
| Pilot | Production Pilot (one contract, real data) | Proven agents, as authorized | Gate decisions for the increments in scope; accreditation path agreed | Scorecard shows value, control effectiveness, and readiness; proceed to scale, revise, or stop |

Not separate prototypes: the governance review (FC-03) runs across every increment, and the Volume 08 capability services are built up across increments 1–4. The count can change: Prototype 2 may merge with 3, Growth may come before Strategy, and the path ends at four if the Scala Lucida decision is no.

**Document structure.** `Documentation/2 Prototype` holds the build plan and one folder per increment. Every increment produces the same set, named `P<n>.<nn> — <title>.docx` (pilot: `PP.<nn>`): .00 Increment Charter, .01 Scope, Requirements, and Governance, .02 Data, Knowledge, and Evidence, .03 Architecture and Agent Implementation, .04 Workflows and Human Approval, .05 Safety, Evaluation, and Test Results, .06 Setup, Demonstration, and Handoff Runbook, .07 Manual Demonstration Script, .08 Gate Decision Record. Prototype 1 keeps its names (P01–P07 are P1.01–P1.07; P00 is the frozen baseline). Each increment is a version of this repository (0.5, 0.6, …), with its evidence under `evidence/runs/`.

## Frameworks and tooling: decisions to make at each gate

Recorded 2026-11-05 at the product owner's direction. Prototype 1 is plain Python with the Anthropic SDK, and Checkpoint 4.1 describes how the tools below *would* map to the design; it does not commit to them. None is committed. Each is a decision at the gate named below, also listed in that increment's charter. Prototype 1 adds none before the capstone unless the rubric requires one.

| Tool | Expectation | Decide at | Why |
|---|---|---|---|
| MCP | Likely | Prototype 2 or 3, then the Production Pilot | A standard tool interface beats custom connectors once agents need contract files, shared plan state, or real enterprise systems. Start read-only. |
| LangGraph | Possible | Prototype 2 gate; most likely needed at Prototype 4 | Helps with long-running, branching work that pauses for human approval and resumes. Today's workflows do not need it. |
| LangChain | Unlikely | Prototype 2, for a document loader only | The model adapter, retrieval gateway, and schema validation already do its main jobs, and another layer makes the governance harder to audit. |
| Claude Agent Skills | Possible | Prototype 2 or later | Could package domain knowledge such as contract clause patterns, if each skill is versioned and tested like a prompt change. Not for Prototype 1. |
| CrewAI | Not planned | None | Its role-based autonomy pulls against registered, bounded, explicitly authorized agents. |
| A2A | Not planned | None | Agents do not message each other directly; they exchange typed records through stores and the orchestrator. |

**Adoption rule.** A framework comes in only if it (1) solves a problem the increment actually hit, (2) sits behind the deterministic policy rather than replacing it, (3) keeps the existing tests and evaluation passing, (4) enters through a change request and the agent registry, and (5) stays replaceable. An MCP server is a new way in, so it needs the same permission checks and audit as the gateway, plus defenses against tool poisoning.

## FC-01 · Scala Lucida: career pathways and expertise discovery

**Name.** Scala Lucida, "the bright scale" (Latin *scala*, a ladder and a musical scale; *lucida*, bright), adopted 2026-11-12 in place of the working name LITE-BRITE, which is a third-party trademark. It joins the sound-themed family of StrataCaster, Vox Probata and Tritone. CR-SC-2026-006 (accepted 2026-11-14) renames it in the corpus (Volume 01 Sections 1.1 and 11, Volume 08 Section 6.1, and the alignment review) and records the pronunciations: StrataCaster is STRAT-uh-kas-ter, and Scala Lucida is SKAH-luh LOO-sih-duh. Still to follow once it is accepted: the build plan generators and the course deck, which still say LITE-BRITE.

**Goal.** The task alignments that Solutions receives are used beyond requirement coverage. They feed career-development tables that show what each person is doing now and has done before, alongside resume and certification data. On that basis, a new capability, **Scala Lucida**, would:
- show pathways into new work-role pipelines;
- show areas of expertise that can support Growth, Strategy, and HR functions.

**Why not now.**
- **Missing data.** The prototype has no career-development tables, resume data, or certification data, and the roster workbook does not model them.
- **Conflicting rules.** The prototype deliberately prohibits staffing, employment, and ranking purposes (PR-12, purposes `personnel-ranking` and `staffing-recommendation`). Scala Lucida's HR use cuts directly across those rules. It needs a governance decision before any build:
  - permitted purposes;
  - consent and notice to employees;
  - human review of any people-related output;
  - retention and access rules for career data;
  - a fairness review.

**What the prototype already provides.**
- One published task-alignment record per task, carrying the employee's name, ID, work role, and TTO, with no MSR content (PR-22).
- Contract-wide signals for similar work and collaboration opportunities (PR-23).
- Append-only history of every alignment version, approval, and revocation.

Together these give Scala Lucida a governed, traceable stream of "what this person did, against which requirement, approved by whom".

**Fit with the operating model.** Much of this is already target state in the corpus:
- Volume 03 (career-pipeline analysis: profiles, task similarity, gaps, development actions);
- Volume 05 (work-role summaries and career alignment);
- Volume 06 (work-role career pipelines).

What the corpus does not have is the Scala Lucida name and capability record (Volumes 01 and 08), or explicit permission for employee identity in filtered records (Volume 01 field list; Volume 04 §14 minimization and notice). The conflict noted above is with the prototype's scope, not with the operating model, which already permits career decision support with human approval.

**When.** In future system development (beyond the course prototype), after:
1. the Cadre's decision on this prototype;
2. the operating-model change (purposes, HR-facing audience, data protections) is approved.

Treat it as its own capability with its own agent authorization; it must not be an extension of the Solutions Expertise Agent.

## FC-02 · Continuous REQ review

**Goal.** Each REQ is reviewed continually. The review compares:
- the work previously performed in the position;
- the work currently performed;
- recommendations for potential new requirements.

This keeps an ongoing assessment of both the employees' expertise and capabilities and the actual work, past and present. It also points to ways to expand a REQ, or to recommend a whole new REQ in the assigned office.

**Why not now.** The prototype assesses one reporting period at a time. It has no trend view across months, and the REQ is not versioned.

**Started (2026-10-31, CR-SC-2026-003): the REQ-change planning pilot.** The TTO Coordination Agent now proposes REQ changes for one REQ and period using a Tree of Thought beam search (`stratacaster/req_planner.py`, review app tab 8, PR-29):
- it clusters potential-new-task statements from TTO-approved MSRs;
- it proposes no-change, widen, route elsewhere, add task, or new REQ for each cluster;
- deterministic code checks constraints, scores, prunes, and ranks complete plans;
- the TTO lead (or a PMO peer for PMO staff REQs) endorses one plan or rejects all;
- the contract's PMO, the approval authority for REQ changes (decided 2026-10-31), approves, returns, or rejects it, and approval creates a REQ change record.

This covers scope item 1 (clustering and cross-REQ signals) and item 2 (a human-approved REQ change recommendation, with the PMO as approver). Not yet built: the trend view across periods, applying an approved change as a new REQ version, and REQ versions cited by alignments (item 3). The prototype never rewrites the live REQ baseline.

**What the prototype already provides.**
- Per-work-role REQs, generated from the workbook's Requirement Tasks (PR-21).
- Each statement tagged as aligned or as a *potential new task requirement* (PR-21).
- Collaboration-opportunity signals that show where a "new" task is already done elsewhere on the contract (PR-23).
- The roster dataset holds several months of MSRs (`env.load_all_cases(scope="all")` loads all 440), so previous vs current periods are available as input.

**When.** It is the natural next prototype iteration, because it stays inside Delivery and reuses the existing assessment. Suggested scope:
1. A per-REQ report covering tasks with work this period and in prior periods, tasks with no reported work, clustered potential-new-task statements, and cross-REQ signals.
2. A human-approved "REQ change recommendation" record, versioned like evidence, with the PMO as approver.
3. The REQ itself becomes versioned, so alignments cite the REQ version they used.

Recommendations about individual people stay out of scope until FC-01's governance decision.

## FC-03 · Model-assisted governance assurance review (queued)

Recorded 2026-11-01 at the product owner's request so it is not lost. The tooling is built and tested offline; no live run has been made.

**Goal.** Show StrataCaster's AI governance with evidence, not assertion. Claude reviews every controlled document against one set of governance criteria and produces findings, a control matrix (where each control is defined in the corpus, where the prototype implements it, the evidence, and the gap), cross-document conflicts, and top risks. The review applies StrataCaster's own rule to itself: the model gives decision support, and the volume owner decides every finding.

**Scope.**
- Corpus Volumes 00–12 (`Documentation/1 Corpus`), with tracked changes read as if accepted.
- Prototype documents P01–P06 (P00, the frozen baseline, only with `--include-baseline`).
- Prototype governance artifacts: agent registry, traceability, roadmap, README, and agent prompts.

**Criteria.** G-01 human oversight (the canonical clause); G-02 approval authority and separation of duties; G-03 data minimization and containment; G-04 agent authorization and scope; G-05 prohibited purposes and fairness; G-06 evaluation and evidence integrity; G-07 failure handling; G-08 traceability and change control; G-09 model and vendor posture.

**Controls on the review itself.**
- Plan-only by default; `--run` is required for model calls. A spending cap (`--max-usd`, default $15) is checked before each call.
- Versioned prompts (`governance_review_v1`, `governance_synthesis_v1`), with document text treated as data, not instructions.
- Each finding's quote is verified deterministically against the source. Unverified quotes are flagged for a human check.
- Every finding starts with disposition `pending`. No finding changes a controlled document by itself. Accepted findings go through the normal tracked-change and change-request process.

**Estimated cost.** The plan for the current documents (corpus, the build plan and every increment folder, and the prototype artifacts) is 35 calls, about 462k input and 144k output tokens, or about $5 at `claude-opus-5-5` prices. Output tokens are estimated conservatively.

**When.**
1. After the human-oversight-clause tracked changes (2026-11-01) are accepted, so the review sees the settled text.
2. Before the Cadre demonstration, so the control matrix can be shown as governance evidence.
3. Again after each volume revision, to compare finding counts between runs.

**How to run.** See README, "Queued: model-assisted governance review".
