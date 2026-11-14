# Retrieval trace 20261031T135605Z

- Question: Who has confirmed that reported fixes were actually closed?
- Status: complete
- Vector store: Chroma 1.5.9 (in-process, rebuilt from the evidence store)
- Embedding model: all-MiniLM-L6-v2 (ONNX build via chromadb DefaultEmbeddingFunction; 384 dimensions)
- Corpus: {'requirement_tasks': 135, 'indexed_record_versions': 139, 'eligible_records': 138, 'excluded_records': 1, 'reference_fixture': 'roster-reference/3.0+875ff7420c3e'}
- Retry: {'missing': 'requirement', 'filter': {'req_task_id': 'SIM-REQ-1-01-07'}, 'found': ['SIM-REQ-1-01-07']}

## Candidates (8)

- SIM-EVID-025:v1 (evidence) similarity 0.3967
- SIM-EVID-028:v1 (evidence) similarity 0.3967
- SIM-EVID-031:v1 (evidence) similarity 0.3967
- SIM-EVID-034:v1 (evidence) similarity 0.3967
- SIM-EVID-037:v1 (evidence) similarity 0.3967
- SIM-EVID-013:v1 (evidence) similarity 0.3741
- SIM-EVID-016:v1 (evidence) similarity 0.3741
- SIM-EVID-022:v1 (evidence) similarity 0.3741

## Excluded before ranking

- SIM-EVID-136:v1 similarity 0.4046: disposition revoked, approval not current for this version, approval revoked or rejected

## Selected (4)

- evidence SIM-EVID-025 score 0.6379: Employee Felix Bennett (SIM-EMP-011, WR-002, SIM-TTO-1-01) reported work aligned to SIM-REQ-1-01-07: Closure evidence
- evidence SIM-EVID-028 score 0.6379: Employee Clara Bennett (SIM-EMP-012, WR-002, SIM-TTO-1-01) reported work aligned to SIM-REQ-1-01-07: Closure evidence
- evidence SIM-EVID-031 score 0.6379: Employee Micah Bennett (SIM-EMP-013, WR-002, SIM-TTO-1-01) reported work aligned to SIM-REQ-1-01-07: Closure evidence
- requirement SIM-REQ-1-01-07 score 0.5918: Closure evidence

## Prompt-only comparison (context, not model quality)

- Prompt-only: {'msrs_in_context': 61, 'raw_narratives_exposed': 61, 'not_employee_accepted': ['SIM-MSR-TEST-04', 'SIM-MSR-TEST-05'], 'flagged_by_content_filter': ['SIM-MSR-TEST-01', 'SIM-MSR-TEST-02', 'SIM-MSR-TEST-04'], 'sources_of_revoked_evidence': ['SIM-MSR-TEST-03'], 'citable_approved_records': 0}
- Governed: {'raw_narratives_exposed': 0, 'ineligible_records_ranked': 0, 'ineligible_records_excluded': 1, 'citable_approved_records': 3}
