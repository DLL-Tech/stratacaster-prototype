# Evaluation run EVAL-20261031T203347Z

- Executor: Dave Louque · Reviewer: CMU Agentic AI Cadre · Decision: pending review
- Model service: claude (claude-opus-5-5)
- Dataset: prototype-eval-labels/1.0

- Alignment precision 1.00, recall 1.00
- Coverage-status accuracy 0.83; citation correctness 1.00
- Blocked inputs published: 0; raw-content disclosures: 0
- Model latency 38451 ms; cost $0.115528

## Roster dataset (roster-reference/3.0+875ff7420c3e)

- Inputs loaded 62, rejected 3; statements aligned 32% (90 of 279)
- Draft records 315, approved 309, published 309; blocked records published 0
- Test cases meeting expected outcomes: 10 of 10
- REQ assessment tags: {'aligned+potential-new-task-requirement': 46, 'potential-new-task-requirement': 4, 'aligned': 11}; TTO lead review: {'approved by a PMO teammate': 6, 'approved': 55, 'own': 3}
- Workflow checks met: 5 of 5
- Query coverage: {'SIM-REQ-1-01-01': 'supported', 'SIM-REQ-1-01-02': 'supported', 'SIM-REQ-1-01-03': 'supported', 'SIM-REQ-1-01-04': 'supported', 'SIM-REQ-1-01-05': 'unsupported', 'SIM-REQ-1-01-06': 'unsupported', 'SIM-REQ-1-01-07': 'conflict'}
- Escalations 3; raw-content disclosures 0
- Contract-wide signals: {'all_assessed': {'similar-work': 152, 'collaboration-opportunity': 57}, 'scripted_submission': {'similar-work': 2, 'collaboration-opportunity': 0}, 'related_work_records': 33}
- Roster model calls 130 (0 failed); latency 1534925 ms; cost $4.487532

## Semantic retrieval (report only; checkpoint 3.1)

- Status: complete; Chroma 1.5.9 (in-process, rebuilt from the evidence store) · all-MiniLM-L6-v2 (ONNX build via chromadb DefaultEmbeddingFunction; 384 dimensions)
- Selected: ['SIM-EVID-019', 'SIM-EVID-028', 'SIM-EVID-037', 'SIM-REQ-1-01-07']; excluded before ranking: [{'id': 'SIM-EVID-307:v1', 'reasons': ['disposition revoked', 'approval not current for this version', 'approval revoked or rejected']}]; retry: {'missing': 'requirement', 'filter': {'req_task_id': 'SIM-REQ-1-01-07'}, 'found': ['SIM-REQ-1-01-07']}
- Prompt-only context for the same question: {'msrs_in_context': 61, 'raw_narratives_exposed': 61, 'not_employee_accepted': ['SIM-MSR-TEST-04', 'SIM-MSR-TEST-05'], 'flagged_by_content_filter': ['SIM-MSR-TEST-01', 'SIM-MSR-TEST-02', 'SIM-MSR-TEST-04'], 'sources_of_revoked_evidence': ['SIM-MSR-TEST-03'], 'citable_approved_records': 0}

## REQ-change planning, Tree of Thought pilot (report only; checkpoint 4.1)

- Status: approved; SIM-REQ-1-01-WR-001 2026-10; clusters 8, searched ['C1', 'C2', 'C3'], flagged statements excluded 1
- Nodes generated 21 (budget exhausted: False); pruned by reason: {}
- Ranked plans: [{'rank': 1, 'score': 0.8033, 'key': 'C1:route-elsewhere:SIM-REQ-1-03-05 | C2:no-change:- | C3:no-change:-'}, {'rank': 2, 'score': 0.8, 'key': 'C1:route-elsewhere:SIM-REQ-1-03-05 | C2:no-change:- | C3:route-elsewhere:SIM-REQ-1-03-04'}, {'rank': 3, 'score': 0.7678, 'key': 'C1:route-elsewhere:SIM-REQ-1-03-05 | C2:no-change:- | C3:widen-task:SIM-REQ-1-01-02'}]
- Endorsed by SIM-EMP-002 (TTO lead); approved by SIM-EMP-001 (PMO); change SIM-RQC-FCE9148A
- Gate checks met: 4 of 4

## Threshold checks

- alignment_precision: met
- alignment_recall: met
- citation_correctness: met
- coverage_status_accuracy: met
- blocked_inputs_published: met
- raw_content_disclosures: met
- roster_test_cases_met: met
- roster_blocked_records_published: met
- roster_raw_content_disclosures: met
- roster_workflow_checks_met: met
