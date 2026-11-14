# Employee feedback check: deployment options (PR-30)

Recorded 2026-11-01 at the product owner's request. The prototype runs the free option. The hybrid and model options are built, tested, and selectable by configuration, so the choice can be made at system deployment.

## What the check does

Before an MSR is submitted, the employee checks the draft against their work-role REQ. Each statement is shown as aligned to a REQ task, or as a potential new task. The employee responds to each statement, then edits and re-checks, or submits. The check stores and logs nothing; only the submission is recorded. See README, "Employee feedback before submission".

## The three modes

Set `feedback_check.mode` in `config/prototype_config.json`, or `STRATACASTER_FEEDBACK_MODE` for one run.

| | `local` (prototype default) | `hybrid` (deployment option) | `model` (deployment option) |
|---|---|---|---|
| Alignment in the check | Keyword hits plus local embedding similarity (all-MiniLM-L6-v2, threshold 0.45) | Same as `local` | Role Execution Agent (main model) |
| Recommended new task in the check | No; written at submission and shown to the TTO lead | Yes, by a small model (`claude-haiku-5-5`), for unmatched statements only | Yes, by the main model |
| Similar work elsewhere on the contract | Yes (local similarity) | Yes | Covered at submission by the contract-wide analytic |
| Model calls per check | 0 | 0 or 1 (only if a statement is unmatched) | 1 |
| Cost per check (estimate) | $0 | under $0.001 | about $0.03 (Opus 5.5, observed rate) |
| Calls at submission | 1 assessment (also writes the recommended tasks) | 1 assessment | 0 extra (the check is reused) |
| Check labeled | Preliminary | Preliminary | Final for an unchanged draft |
| Data sent to a model provider by the check | None | Unmatched draft statements | All draft statements |
| Works offline or air-gapped | Yes | Alignment only | No |

Estimates: Opus 5.5 is $4 / $20 per million input / output tokens, and the 2026-10-31 smoke test averaged about $0.03 per call. Haiku 5.5 is $0.10 / $0.50 for prompts of 100K tokens or fewer; a wording call is roughly 1–2K input and under 1K output tokens. Confirm current pricing before deployment.

## Tradeoffs

- **Quality.** `local` is the weakest check. Calibrated on the roster's October MSRs against the workbook's keyword alignments, the similarity part alone had precision 0.48 and recall 0.35 at 0.45. The combined check agreed on aligned versus new for about 85% of statements. Keyword labels flatter keyword matching, so measure against live assessments before relying on these numbers. Because the check is preliminary, any statement the assessment tags differently is flagged to the TTO lead ("assessed differently from the employee's preliminary check").
- **Employee experience.** `local` and `hybrid` allow unlimited free re-checks. Only `hybrid` and `model` show recommended task wording before the employee submits.
- **Data handling.** `local` keeps draft text on the server. `hybrid` sends only unmatched statements to the small model. `model` sends the whole draft, which is what submission does anyway.
- **Cost at scale.** For example, 270 employees submitting monthly with an average of three checks each: `local` ≈ $0 for checks; `hybrid` ≈ under $1 per month; `model` ≈ $25 per month. That's before the one assessment per submission, which all modes share.

## Recommendation for deployment

1. Start with `local` wherever data must stay on premises, or the network is disconnected.
2. Use `hybrid` where a small hosted model is approved for draft statements. It keeps re-checks almost free and gives employees recommended wording before they submit.
3. Use `model` only if check quality must match the assessment exactly and the cost of re-checks is acceptable.

## Before switching modes in deployment

- Re-run `tests/test_submission_feedback.py` (all three modes are covered).
- Measure agreement between the check and the assessment on a sample of real, approved submissions; tune `similarity_threshold`.
- For `hybrid`, confirm data-handling approval for the small model and its price; set `hybrid_llm` in the config.
- Prompt caching can cut the cost of repeated checks once the instructions plus REQ catalog exceed the model's minimum cacheable prompt length. The prototype's prompts are below that length, so caching is not enabled.
