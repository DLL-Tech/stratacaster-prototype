# Evaluation run EVAL-20261114T161848Z

- Executor: Dave Louque · Reviewer: CMU Agentic AI Cadre · Decision: pending review
- Model service: claude (claude-opus-5-5)
- Dataset: prototype-eval-labels/1.0

- Alignment precision 1.00, recall 1.00
- Coverage-status accuracy 0.67; citation correctness 1.00
- Blocked inputs published: 0; raw-content disclosures: 0
- Model latency 35730 ms; cost $0.131644

## Roster dataset (roster-reference/3.0+875ff7420c3e)

- Inputs loaded 62, rejected 3; statements aligned 52% (146 of 279)
- Draft records 285, approved 279, published 279; blocked records published 0
- Test cases meeting expected outcomes: 10 of 10
- REQ assessment tags: {'aligned': 24, 'potential-new-task-requirement': 4, 'aligned+potential-new-task-requirement': 33}; TTO lead review: {'approved by a PMO teammate': 6, 'approved': 55, 'own': 3}
- Workflow checks met: 5 of 5
- Query coverage: {'SIM-REQ-1-01-01': 'supported', 'SIM-REQ-1-01-02': 'supported', 'SIM-REQ-1-01-03': 'supported', 'SIM-REQ-1-01-04': 'supported', 'SIM-REQ-1-01-05': 'unsupported', 'SIM-REQ-1-01-06': 'unsupported', 'SIM-REQ-1-01-07': 'conflict'}
- Escalations 3; raw-content disclosures 0
- Contract-wide signals: {'all_assessed': {'similar-work': 164, 'collaboration-opportunity': 36}, 'scripted_submission': {'similar-work': 2, 'collaboration-opportunity': 0}, 'related_work_records': 22}
- Roster model calls 135 (0 failed); latency 1580430 ms; cost $5.140816

## Semantic retrieval (report only; checkpoint 3.1)

- Status: complete; Chroma 1.5.9 (in-process, rebuilt from the evidence store) · all-MiniLM-L6-v2 (ONNX build via chromadb DefaultEmbeddingFunction; 384 dimensions)
- Selected: ['SIM-EVID-021', 'SIM-EVID-026', 'SIM-EVID-031', 'SIM-REQ-1-01-07']; excluded before ranking: [{'id': 'SIM-EVID-277:v1', 'reasons': ['disposition revoked', 'approval not current for this version', 'approval revoked or rejected']}]; retry: {'missing': 'requirement', 'filter': {'req_task_id': 'SIM-REQ-1-01-07'}, 'found': ['SIM-REQ-1-01-07']}
- Prompt-only context for the same question: {'msrs_in_context': 61, 'raw_narratives_exposed': 61, 'not_employee_accepted': ['SIM-MSR-TEST-04', 'SIM-MSR-TEST-05'], 'flagged_by_content_filter': ['SIM-MSR-TEST-01', 'SIM-MSR-TEST-02', 'SIM-MSR-TEST-04'], 'sources_of_revoked_evidence': ['SIM-MSR-TEST-03'], 'citable_approved_records': 0}

## REQ-change planning, Tree of Thought pilot (report only; checkpoint 4.1)

- Status: approved; SIM-REQ-1-01-WR-001 2026-10; clusters 6, searched ['C1', 'C2', 'C3'], flagged statements excluded 1
- Nodes generated 21 (budget exhausted: False); pruned by reason: {}
- Ranked plans: [{'rank': 1, 'score': 0.6671, 'key': 'C1:add-task:- | C2:no-change:- | C3:no-change:-'}, {'rank': 2, 'score': 0.6637, 'key': 'C1:add-task:- | C2:no-change:- | C3:route-elsewhere:SIM-REQ-1-PMO-02'}, {'rank': 3, 'score': 0.6637, 'key': 'C1:add-task:- | C2:route-elsewhere:SIM-REQ-1-PMO-06 | C3:no-change:-'}]
- Endorsed by SIM-EMP-002 (TTO lead); approved by SIM-EMP-001 (PMO); change SIM-RQC-478D0533
- Gate checks met: 4 of 4

## Employee feedback check (report only; PR-30)

- Status: completed; engine local; drafts submitted 2; model calls during the check 0
- Check agreed with the assessment on 4 of 6 statements
- Gate checks met: 7 of 7
  - The check writes nothing (no input, record, or audit entry): met
  - An edited draft is refused until it is checked again: met
  - A response is required for every statement: met
  - One employee cannot submit with another's check: met
  - The TTO lead sees the employee's responses: met
  - No draft statement text reaches the audit trail: met
  - The free check makes no model call: met

## Threshold checks

- alignment_precision: met
- alignment_recall: met
- citation_correctness: met
- coverage_status_accuracy: NOT MET
- blocked_inputs_published: met
- raw_content_disclosures: met
- roster_test_cases_met: met
- roster_blocked_records_published: met
- roster_raw_content_disclosures: met
- roster_workflow_checks_met: met
