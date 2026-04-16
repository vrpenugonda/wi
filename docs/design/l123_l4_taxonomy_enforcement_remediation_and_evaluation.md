# WALLE Insights — Taxonomy Enforcement, Remediation, and Evaluation (Design Notes)

## Context and goals

This document describes a developer-ready solution design for these requirements:

- **Req 1**: L1/L2/L3 outputs must **stick to the approved taxonomy** (no invented labels; no hierarchy violations).
- **Req 2**: Reduce **uncategorized/NULL** rates for L1–L4, especially in **bulk date ranges** (20+ days).
- **Req 3**: Ensure **path alignment** across levels (L1→L4 consistency and integrity).
- **Req 4**: Separate remediation pipeline/workflow for **uncategorized L123** and **L4 NULLs** (includes L123 NULLs).
- **Req 5**: Evaluation metrics: **deterministic** and **probabilistic**.

The design is grounded in the current `insights/` implementation (classifiers + pipeline + Snowflake helpers).

## Non-negotiable constraint (Phase 1)

**Do not change any existing prompt truncation limits in Phase 1.**

Reason: current truncation (e.g., L123 `brief_desc[:150]`) is explicitly used to **control tokens/latency/rate-limit behavior**
under high batching and parallelism. Changing it without controlled experimentation can destabilize run reliability.

Evidence in code:
- `insights/classifiers/l123_classifier.py` truncates brief description to 150 chars **“to save tokens.”**
- L4 uses larger truncation caps (e.g., 400–500 chars) but typically runs with smaller batch sizing and has stricter validity handling.

Phase 1 improvements must therefore focus on:
- deterministic validation,
- retry-on-invalid,
- remediation workflows,
- stronger auditing/telemetry,
- bulk-run reliability mechanics,
**without requiring more context per record**.

## Current system snapshot (as-is)

### L123 classification
- **Module**: `insights/classifiers/l123_classifier.py`
- **Taxonomy source**: `insights/models/taxonomy.py` (`INCIDENT_TAXONOMY` dict)
- **Behavior today**:
  - Taxonomy is included in the system prompt.
  - Output is not hard-validated against `INCIDENT_TAXONOMY` after inference.
  - Per-incident input content is truncated to ~150 chars (brief description).

### L4 classification
- **Module**: `insights/classifiers/l4_classifier.py`
- **Taxonomy**: derived per subcategory and cached (local + S3).
- **Behavior today**:
  - L4 classification enforces “category must be in taxonomy” at runtime: invalid categories trigger a retry of the full batch.
  - Uses a strict fallback label `Unclassified_L4` (discouraged; tracked).

### Pipeline orchestration and merge
- **Module**: `insights/pipeline/runner.py`
- Two-stage pipeline:
  1) L123 checkpoint output
  2) L4 per-subcategory checkpoints (parallel)
- Final merge produces a consolidated CSV and uploads to Snowflake (`upload_to_snowflake`) and to S3 artifacts.
- Final-state L4 NULL auditing exists: `record_l4_nulls` is called for incidents with missing `ai_l4` after merge, persisting to `WALLE_L4_NULL_REASONS`.

### Snowflake helpers
- **Module**: `insights/utils/snowflake.py`
- Contains:
  - main table schema creation/upsert for `WALLE_CLASSIFIED_INCIDENTS`
  - L4 NULL reasons table DDL and upsert (`WALLE_L4_NULL_REASONS`)
  - helper query to fetch incidents that have L123 but are missing L4 (`get_incidents_with_l123_missing_l4`)

### Existing remediation entrypoint (partial)
- CLI: `insights/cli.py`
- `run --l4-only` fetches incidents missing L4 from Snowflake (with L123 fields) and runs pipeline with `skip_l123=True`.

## Definitions

### “Valid taxonomy” (deterministic)
Given the approved taxonomy `INCIDENT_TAXONOMY`:
- `L1` is valid if it is a key in `INCIDENT_TAXONOMY`
- `L2` is valid if it is a key in `INCIDENT_TAXONOMY[L1]`
- `L3` is valid if it is in `INCIDENT_TAXONOMY[L1][L2]`

### “Uncategorized / invalid L123”
This must be treated as a **policy decision**, not assumed.
This document uses the neutral term **L123-invalid** to include:
- NULL/missing values
- invented labels (not in taxonomy)
- hierarchy violations (L2 not child of L1, etc.)
- swapped levels (common failure mode)

### “Path integrity” (end-to-end)
An incident has **path integrity** if:
- L123 is valid (per above), and
- L4 is either:
  - a valid L4 category from the derived taxonomy for that incident’s L2/subcategory, or
  - the explicit fallback `Unclassified_L4` (considered “present but low-signal”), or
  - NULL with a recorded final reason in `WALLE_L4_NULL_REASONS` (audited missingness)

## Design overview (two-phase delivery)

### Phase 1 (implement now, no truncation-limit changes)
1) Add **hard deterministic validation** for L123 outputs, plus **retry-on-invalid** behavior (modeled after L4’s validity gate).
2) Add a dedicated **remediation workflow** that can re-run L123 and/or L4 on remediation-eligible IDs with safe upsert guardrails.
3) Expand audit/telemetry tables and metrics so bulk-run failure modes are visible and attributable.

### Phase 2 (optional, gated experiment)
If Phase 1 telemetry demonstrates that invalid/NULL outputs are predominantly due to insufficient input signal:
- add a controlled “more context on retry only” mechanism behind a config flag and token budget,
- validate impact through the deterministic/probabilistic evaluation suite.

## Requirement-by-requirement solution design

## Req 1 + Req 3 (combined): L123 taxonomy enforcement + path integrity

### 1.1 Design intent
Convert L123 from “prompt-enforced only” to “prompt + code-enforced,” so the system guarantees:
- no taxonomy drift reaches persisted outputs unless explicitly allowed by policy,
- invalid outputs are retried deterministically and then routed into remediation,
- end-to-end path completeness is measurable and enforceable.

### 1.2 New module: `TaxonomyValidator`
Create a pure-Python validator that:
- validates and normalizes `(L1, L2, L3)` against `INCIDENT_TAXONOMY`,
- detects hierarchy violations and swapped levels,
- returns a structured result used both for:
  - in-run retry, and
  - audit persistence for later remediation.

Proposed contract:
- Input:
  - `l1: str | None`, `l2: str | None`, `l3: str | None`
  - `taxonomy: dict[str, dict[str, list[str]]]`
- Output:
  - `is_valid: bool`
  - `status: Literal["valid", "missing", "invalid_label", "invalid_hierarchy", "swapped_levels_suspected"]`
  - `details: dict[str, Any]` (best-effort; never throws)
  - optional `repair_suggestion: dict[str, str] | None` (only if deterministic)

Normalization rules (deterministic only):
- `strip()` whitespace
- treat empty string / `"None"` / pandas NaN as missing
- **Do not** introduce fuzzy matching in Phase 1 (avoid assumptions/false positives).

### 1.3 L123 retry-on-invalid
Add a retry loop similar to L4’s invalid-category retry:
- If any classification in a batch fails validation:
  - retry the whole batch with a stricter contract:
    - model must choose from enumerated allowed values (for that batch) OR return explicit missing markers (policy).
- After `N` retries:
  - mark the incident as L123-invalid (for remediation) and persist an audit row.

Important:
- Phase 1 does not change input truncation; retry uses the same truncated fields.

### 1.4 Pipeline-level path integrity gates
After pipeline merge:
- If L123-invalid/missing, L4 should be treated as unreliable and remediation-eligible.
- Persist:
  - L123 audit row (new table) and
  - final L4 missing reason if L4 is missing.

### 1.5 Data persistence: new L123 audit table
Add a Snowflake audit table (name is a decision; recommended):
- `WALLE_L123_TAXONOMY_AUDIT`

Proposed schema:
- `in_id VARCHAR`
- `walle_run_id VARCHAR`
- `l1 VARCHAR`, `l2 VARCHAR`, `l3 VARCHAR`
- `status VARCHAR` (from validator)
- `details VARCHAR` (JSON string; best-effort)
- `recorded_at TIMESTAMP_NTZ`
- Primary key: `(in_id, walle_run_id)`

Purpose:
- quantify taxonomy drift and remediation needs,
- enable cohort slicing by day/subcategory/run,
- prevent “silent” invalid labels being written to the main table.

## Req 2: Reduce uncategorized/NULL (bulk windows) — without changing limits

### 2.1 What Phase 1 can improve (without adding more text)
Bulk-window issues can arise from:
- partial L4 subcategory task completion (timeouts/rate limiting),
- taxonomy caching differences,
- invalid L4 categories being cleaned to NULL,
- L123 invalid outputs cascading into L4 missingness.

Phase 1 levers:
- **Coverage contracts** and “degraded run” classification:
  - L123 validity rate
  - L4 present rate (including `Unclassified_L4` separately)
  - L4 missing final reasons breakdown
- **Fail-loud option** in CI/workflows (policy decision):
  - fail run if coverage below threshold
  - or continue but emit explicit degraded status + artifacts
- **Retry pool** for failed subcategory tasks:
  - if an L4 task fails, re-queue remaining IDs within the same run (bounded by attempt count)
- **Taxonomy reuse**:
  - default to cached per-subcategory taxonomy from S3 unless `--generate-taxonomy`

### 2.2 Telemetry required to diagnose bulk-window behavior
Persist run-level summary (table name decision; recommended):
- `WALLE_PIPELINE_RUN_SUMMARY`
  - `walle_run_id`
  - time range processed
  - total incidents
  - L123 valid/invalid counts
  - L4 present/unclassified/missing counts
  - per-subcategory breakdown
  - recorded timestamps

This supports:
- comparing 1-day vs 30-day runs directly,
- attributing missingness to specific subcategories/tasks.

## Req 4: Separate remediation workflow/pipeline

### 4.1 Remediation-eligible definition (Phase 1)
An incident is remediation-eligible if any is true:
- L123-invalid (audit status not `valid`)
- L123 missing (policy)
- L4 missing (`ai_l4` NULL/empty after processing)
- L4 invalid-cleaned to NULL (already tracked via final reasons)

### 4.2 Remediation architecture
Add a dedicated CLI command (recommended) e.g.:
- `insights run --remediate ...` or `insights remediate ...`

Flow:
1) Query Snowflake for remediation-eligible IDs and required incident text fields.
2) Split into cohorts:
   - cohort A: L123 remediation required
   - cohort B: L4 remediation required (only if L123 valid after remediation)
3) Run reprocessing:
   - L123-only pass for cohort A
   - L4-only pass for cohort B
4) Upsert to `WALLE_CLASSIFIED_INCIDENTS` with **column allowlist guardrails**
5) Persist audit outputs:
   - `WALLE_L123_TAXONOMY_AUDIT` rows for remediation run
   - `WALLE_L4_NULL_REASONS` updates for remaining missingness
   - run summary table

### 4.3 Guardrails (no assumptions; decisions required)
Remediation must not unintentionally degrade good existing rows.
Guardrails to implement:
- **Column allowlist** per remediation stage:
  - L123 remediation updates only L123-related columns (`AI_L1/L2/L3`, confidence, rationale, etc.)
  - L4 remediation updates only L4-related columns
- **Overwrite rules** (open decision):
  - can remediation overwrite non-null values?
  - can it overwrite `Unclassified_L4`?
- **Idempotency**:
  - remediation is safe to re-run; merge keys include `IN_ID` and run metadata is updated deterministically.

### 4.4 Workflow integration
Create a dedicated GitHub Actions workflow (name decision) e.g.:
- `.github/workflows/walle-remediate.yaml`

Inputs:
- date range or minutes-back
- target environments
- max records / sampling controls (debug)
- toggles for L123 remediation, L4 remediation

## Req 5: Evaluation metrics (deterministic + probabilistic)

### 5.1 Deterministic metrics (always-on)
Use `INCIDENT_TAXONOMY` and/or `INCIDENT_TAXONOMY_REF` to compute:
- **L123 validity**: % valid paths and breakdown by failure status
- **Hierarchy violations**: common invalid (L1,L2) / (L2,L3) combinations
- **Coverage**:
  - % non-null per level (L1–L4)
  - % `Unclassified_L4` separate from NULL L4
- **Stability / drift**:
  - label entropy and top-share by week/month
- **Path completeness**:
  - % valid L123 and present L4

Recommended implementation location:
- a dedicated script under `scripts/` or a module under `insights/metrics/` with CI entrypoint.

### 5.2 Probabilistic metrics (sampled, rate-limited)
Re-use the LLM-judge harness style already implemented for DEX vs WALLE comparisons (in `scripts/dexter_vs_walle_metrics.py`):
- judge WALLE outputs against incident record only (single-model judge mode), or
- judge WALLE vs previous WALLE run (A/B over time), if needed.

Key requirements for probabilistic evaluation:
- parallelism must respect rate limits (similar semaphore + max RPM constraints)
- store per-incident judge result rows for reproducibility

### 5.3 “Phase 1 justifies Phase 2” metric
Add a specific analysis:
- Among L123-invalid incidents after max retries, what fraction have:
  - sparse/empty incident text vs
  - richly described incidents

If richly described incidents still fail, input length is not the primary issue.
If sparse incidents dominate, Phase 2 can consider controlled context expansion for retries only.

## Implementation plan (no truncation changes)

### Step A — L123 validation + retry
- Add `TaxonomyValidator` module
- Integrate into `L123Classifier.classify_batch` post-processing:
  - validate every returned classification row
  - if any invalid → retry batch with stricter contract
- Persist audit rows (Snowflake)

### Step B — Remediation command/workflow
- Add Snowflake queries for remediation cohorts:
  - invalid/missing L123
  - missing L4 (already exists)
- Implement remediation CLI and a GitHub Actions workflow
- Implement upsert guardrails

### Step C — Deterministic metrics suite
- Add metrics script/module
- Run it post-pipeline and post-remediation
- Emit artifacts (CSV/Excel) and optionally persist summary rows to Snowflake

### Step D — Probabilistic metrics suite
- Reuse judge harness pattern with rate limiting and sampling
- Ensure sampling is deterministic and reproducible (seeded)

## Open decisions (must be answered; do not assume)

1) **What constitutes “uncategorized L123”?**
   - Is it strictly NULL, or can it be an explicit controlled bucket label?
   - Are “Other” labels allowed at L2/L3 in taxonomy, and should the model prefer them?

2) **Overwrite policy during remediation**
   - Can remediation overwrite non-null L1/L2/L3/L4?
   - Can remediation overwrite `Unclassified_L4`?
   - Should remediation be “fill-only” by default?

3) **Fail-loud policy**
   - Should CI fail when validity/coverage thresholds are breached, or only emit degraded-run status?
   - What are the threshold values (per env)?

4) **Snowflake table names and ownership**
   - Confirm audit/summary table names and target schema/database.

5) **Execution location**
   - Should metrics run in the main pipeline workflow, a separate workflow, or both?

## Appendix: Relevant files (current code)

- L123 classifier: `insights/classifiers/l123_classifier.py`
- L4 classifier: `insights/classifiers/l4_classifier.py`
- Base classifier concurrency + headers: `insights/classifiers/base.py`
- Pipeline runner/merge: `insights/pipeline/runner.py`
- CLI entrypoints: `insights/cli.py`
- Taxonomy source: `insights/models/taxonomy.py`
- Snowflake helpers: `insights/utils/snowflake.py`

