# Evaluation run EVAL-20261031T195018Z

- Executor: Dave Louque · Reviewer: CMU Agentic AI Cadre · Decision: pending review
- Model service: claude (claude-opus-5-5)
- Dataset: prototype-eval-labels/1.0

- Alignment precision 1.00, recall 1.00
- Coverage-status accuracy 0.67; citation correctness 1.00
- Blocked inputs published: 0; raw-content disclosures: 0
- Model latency 36385 ms; cost $0.10848

## Roster dataset (roster-reference/3.0+875ff7420c3e)

- Inputs loaded 62, rejected 3; statements aligned 37% (104 of 279)
- Draft records 314, approved 309, published 309; blocked records published 0
- Test cases meeting expected outcomes: 9 of 10
- REQ assessment tags: {'aligned+potential-new-task-requirement': 45, 'potential-new-task-requirement': 4, 'aligned': 12}; TTO lead review: {'approved by a PMO teammate': 6, 'approved': 55, 'own': 3}
- Workflow checks met: 4 of 5
- Query coverage: {'SIM-REQ-1-01-01': 'conflict', 'SIM-REQ-1-01-02': 'supported', 'SIM-REQ-1-01-03': 'supported', 'SIM-REQ-1-01-04': 'supported', 'SIM-REQ-1-01-05': 'unsupported', 'SIM-REQ-1-01-06': 'unsupported', 'SIM-REQ-1-01-07': 'supported'}
- Escalations 3; raw-content disclosures 0

## Semantic retrieval (report only; checkpoint 3.1)

- Status: complete; Chroma 1.5.9 (in-process, rebuilt from the evidence store) · all-MiniLM-L6-v2 (ONNX build via chromadb DefaultEmbeddingFunction; 384 dimensions)
- Selected: ['SIM-EVID-064', 'SIM-EVID-072', 'SIM-EVID-079', 'SIM-REQ-1-01-07']; excluded before ranking: [{'id': 'SIM-EVID-306:v1', 'reasons': ['disposition revoked', 'approval not current for this version', 'approval revoked or rejected']}]; retry: {'missing': 'requirement', 'filter': {'req_task_id': 'SIM-REQ-1-01-07'}, 'found': ['SIM-REQ-1-01-07']}
- Prompt-only context for the same question: {'msrs_in_context': 61, 'raw_narratives_exposed': 61, 'not_employee_accepted': ['SIM-MSR-TEST-04', 'SIM-MSR-TEST-05'], 'flagged_by_content_filter': ['SIM-MSR-TEST-01', 'SIM-MSR-TEST-02', 'SIM-MSR-TEST-04'], 'sources_of_revoked_evidence': ['SIM-MSR-TEST-03'], 'citable_approved_records': 0}

## REQ-change planning, Tree of Thought pilot (report only; checkpoint 4.1)

- Status: approved; SIM-REQ-1-01-WR-001 2026-10; clusters 7, searched ['C1', 'C2', 'C3'], flagged statements excluded 1
- Nodes generated 21 (budget exhausted: False); pruned by reason: {}
- Ranked plans: [{'rank': 1, 'score': 0.7837, 'key': 'C1:add-task:- | C2:no-change:- | C3:no-change:-'}, {'rank': 2, 'score': 0.7804, 'key': 'C1:add-task:- | C2:no-change:- | C3:route-elsewhere:SIM-REQ-1-02-06'}, {'rank': 3, 'score': 0.7482, 'key': 'C1:add-task:- | C2:no-change:- | C3:widen-task:SIM-REQ-1-01-02'}]
- Endorsed by SIM-EMP-002 (TTO lead); approved by SIM-EMP-001 (PMO); change SIM-RQC-5EBC889C
- Gate checks met: 4 of 4

## Threshold checks

- alignment_precision: met
- alignment_recall: met
- citation_correctness: met
- coverage_status_accuracy: NOT MET
- blocked_inputs_published: met
- raw_content_disclosures: met
- roster_test_cases_met: NOT MET
- roster_blocked_records_published: met
- roster_raw_content_disclosures: met
- roster_workflow_checks_met: NOT MET
