"""
WALLE Pipeline - Finalize Step

Merges L123 and L4 classification results into a single output CSV,
then optionally uploads to S3 and Snowflake.

Can be run locally:
    uv run walle finalize --artifacts-dir artifacts/ --run-id 20260225-120000
    uv run walle finalize --artifacts-dir data/ --run-id local --skip-s3 --skip-snowflake
"""

import json
import os
from pathlib import Path
from typing import Any

import pandas as pd


# Column name mappings for normalization
# L123 checkpoint columns -> Final schema columns
L123_COLUMN_MAP = {
    "incident_id": "in_id",
    "category": "ai_l1",
    "subcategory": "ai_l2",
    "product": "ai_l3",
    "confidence_score": "ai_confidence",
    "self_resolved": "ai_self_resolved",
    "rationale": "ai_rationale",
    "keywords_identified": "ai_keywords",
    "root_cause_indicator": "ai_root_cause_indicator",
    "root_cause": "ai_root_cause",
}

# L4 checkpoint columns -> Final schema columns
L4_COLUMN_MAP = {
    "incident_id": "in_id",
    "l4_category": "ai_l4",
    "l4_subcategory": "ai_l4_subcategory",
    "resolution_action": "ai_l4_resolution_action",
    "l4_confidence": "ai_l4_confidence",
    "keywords": "ai_l4_keywords",
    "is_actionable": "ai_l4_actionable",
    "actionability_reason": "ai_l4_actionability_reason",
    "l4_rationale": "ai_l4_rationale",
}


def normalize_columns(df: pd.DataFrame, column_map: dict[str, str], file_type: str = "unknown") -> pd.DataFrame:
    """Normalize column names using the provided mapping."""
    df = df.copy()
    rename_dict = {}
    for old_name, new_name in column_map.items():
        if old_name in df.columns:
            rename_dict[old_name] = new_name
        elif old_name.lower() in [c.lower() for c in df.columns]:
            actual = next(c for c in df.columns if c.lower() == old_name.lower())
            rename_dict[actual] = new_name

    if rename_dict:
        print(f"    Normalizing {file_type} columns: {list(rename_dict.keys())} -> {list(rename_dict.values())}")
        df = df.rename(columns=rename_dict)
    return df


def discover_csv_files(artifacts_dir: str | Path) -> dict[str, list[str]]:
    """Walk the artifacts directory and categorize CSV files by type.
    
    Returns dict with keys: l4_checkpoint, l123_checkpoint, l123_merged, input, other
    """
    artifacts_dir = Path(artifacts_dir)
    all_csvs: list[str] = []

    for root, _dirs, files in os.walk(artifacts_dir):
        for f in files:
            if f.endswith(".csv"):
                full_path = os.path.join(root, f)
                all_csvs.append(full_path)
                try:
                    df_peek = pd.read_csv(full_path, nrows=0)
                    cols = list(df_peek.columns)
                    print(f"  Found: {full_path}")
                    print(f"    -> Columns ({len(cols)}): {cols[:8]}{'...' if len(cols) > 8 else ''}")
                except Exception:
                    print(f"  Found: {full_path} (could not read)")

    if not all_csvs:
        print(f"WARNING: No CSV files found in {artifacts_dir}")

    l4_checkpoint = [f for f in all_csvs if "l4" in f.lower() and "checkpoint" in f.lower()]
    l123_checkpoint = [f for f in all_csvs if "l123" in f.lower() and "checkpoint" in f.lower()]
    l123_merged = [
        f
        for f in all_csvs
        if "classified" in f.lower() and "l4" not in f.lower() and "checkpoint" not in f.lower()
    ]
    input_files = [
        f
        for f in all_csvs
        if "input" in f.lower() or ("incidents_" in f.lower() and "classified" not in f.lower())
    ]
    other = [
        f
        for f in all_csvs
        if f not in l4_checkpoint and f not in l123_checkpoint and f not in l123_merged and f not in input_files
    ]

    categories = {
        "l4_checkpoint": l4_checkpoint,
        "l123_checkpoint": l123_checkpoint,
        "l123_merged": l123_merged,
        "input": input_files,
        "other": other,
    }

    print(f"\nFile categories:")
    print(f"  L123 merged files ({len(l123_merged)}): {l123_merged}")
    print(f"  L123 checkpoint files ({len(l123_checkpoint)}): {l123_checkpoint[:3]}{'...' if len(l123_checkpoint) > 3 else ''}")
    print(f"  L4 checkpoint files ({len(l4_checkpoint)}): {l4_checkpoint[:3]}{'...' if len(l4_checkpoint) > 3 else ''}")
    print(f"  Input files ({len(input_files)}): {input_files}")
    print(f"  Other files ({len(other)}): {other[:3]}{'...' if len(other) > 3 else ''}")

    return categories


def _emit_l123_backfill_audit(
    backfilled_ids: list[str],
    run_id: str,
    reason: str = "null_in_finalize",
) -> None:
    """Persist `WALLE_L123_TAXONOMY_AUDIT` rows for the cohort whose
    L1/L2/L3 were NULL/empty/'none' at finalize time and got backfilled
    to "Uncategorized" by ``_finalize_backfill_null_l123``.

    Status code is the existing ``pipeline_gap`` (already in
    `L123AuditStatus`/`L123_AUDIT_STATUSES`). Originals are NULL because
    we never had them at this stage; finals are "Uncategorized" matching
    what was just written into the merged CSV.

    Best-effort: any failure is logged and swallowed - this audit must
    never block the main-table upload.
    """
    if not backfilled_ids:
        return
    try:
        from insights.config import settings
        from insights.utils.l123_audit_logging import L123AuditRow, record_l123_audit

        if not getattr(settings, "l123_audit_persist", True):
            print(
                f"[REQ-1+3] L123 backfill audit skipped "
                f"(settings.l123_audit_persist=False) for {len(backfilled_ids)} incident(s)",
                flush=True,
            )
            return

        rows: list[L123AuditRow] = [
            L123AuditRow(
                in_id=str(in_id),
                walle_run_id=run_id,
                status="pipeline_gap",
                original_l1=None,
                original_l2=None,
                original_l3=None,
                final_l1="Uncategorized",
                final_l2="Uncategorized",
                final_l3="Uncategorized",
                repair_applied=False,
                details={"reason": reason, "source": "finalize_backfill"},
            )
            for in_id in backfilled_ids
            if in_id
        ]
        record_l123_audit(
            rows,
            persist_to_snowflake=True,
            table_name=getattr(
                settings, "l123_audit_table", "WALLE_L123_TAXONOMY_AUDIT"
            ),
            log_each_incident=False,
        )
    except Exception as exc:
        print(
            f"[REQ-1+3] L123 backfill audit emission failed (non-fatal): {exc}",
            flush=True,
        )


def _finalize_backfill_null_l123(final_file: Path) -> list[str]:
    """Scan ``final_file`` for NULL/empty/'none' values in
    ``ai_l1``/``ai_l2``/``ai_l3`` and rewrite them to "Uncategorized"
    in place. Returns the de-duplicated list of in_ids whose row was
    affected (so callers can emit `WALLE_L123_TAXONOMY_AUDIT` rows).

    This is Scenario P1 - belt-and-suspenders for legacy/edge-case rows
    that arrive at finalize with NULL L123 even though the upstream gate
    should have caught them. Without this, the main table would contain
    NULL L1/L2/L3 in the new run's cohort, violating the Req 1+3
    invariant.
    """
    if not final_file.exists():
        return []

    df = pd.read_csv(final_file)
    if df.empty or "in_id" not in df.columns:
        return []

    affected_ids: set[str] = set()
    columns_changed = False
    for col in ("ai_l1", "ai_l2", "ai_l3"):
        if col not in df.columns:
            continue
        # NaN | empty-string | literal "none" (case-insensitive)
        try:
            string_view = df[col].astype("string")
        except Exception:
            string_view = df[col].astype(str)
        mask = (
            df[col].isna()
            | (string_view.str.strip() == "")
            | (string_view.str.lower() == "none")
        )
        if not bool(mask.any()):
            continue
        affected_ids.update(
            df.loc[mask, "in_id"].astype(str).str.strip().tolist()
        )
        df.loc[mask, col] = "Uncategorized"
        columns_changed = True

    affected_ids.discard("")

    if columns_changed:
        df.to_csv(final_file, index=False)
        print(
            f"[REQ-1+3] Finalize backfill: NULL/empty L1/L2/L3 -> 'Uncategorized' "
            f"for {len(affected_ids)} unique incident(s) in {final_file.name}",
            flush=True,
        )

    return sorted(affected_ids)


def _emit_final_l4_null_audit(final_file: Path, run_id: str) -> None:
    """Scan the finalized merged CSV for missing ai_l4 and persist
    `WALLE_L4_NULL_REASONS` rows.

    Mirrors the unified runner's logic in
    ``insights/pipeline/runner.py`` lines 375-429 so the decomposed
    GitHub-Actions path produces the same audit output as ``walle run``.

    Reason categorization (CI path only):

    - All missing-``ai_l4`` rows: ``reason="l4_missing_after_l4_run"``.
      The ``l4_invalid_category_cleaned`` distinction lives inside the
      per-subcategory ``walle l4`` workers and is not currently surfaced
      to finalize. That's a Phase-2 enhancement (L4 sidecar artifact).

    Best-effort: ``record_l4_nulls`` already auto-creates the target
    table and swallows Snowflake errors with a warning, so this helper
    never raises into the caller under normal operation.
    """
    from insights.utils.l4_null_logging import L4NullRow, record_l4_nulls

    if not final_file.exists():
        return
    df = pd.read_csv(final_file)
    if "in_id" not in df.columns:
        return

    def _is_missing(v: Any) -> bool:
        if v is None or pd.isna(v):
            return True
        s = str(v).strip()
        return s == "" or s.lower() == "none"

    if "ai_l4" not in df.columns:
        missing_mask = df["in_id"].apply(lambda _: True)
    else:
        missing_mask = df["ai_l4"].apply(_is_missing)

    null_rows: list[L4NullRow] = []
    for _, r in df.loc[missing_mask].iterrows():
        in_id = str(r.get("in_id") or "").strip()
        if not in_id:
            continue
        subcat = r.get("ai_l2")
        null_rows.append(
            L4NullRow(
                in_id=in_id,
                reason="l4_missing_after_l4_run",
                cause="ai_l4_missing_in_final_merge",
                subcategory=(
                    str(subcat)
                    if subcat is not None and not pd.isna(subcat)
                    else None
                ),
                walle_run_id=run_id,
            )
        )

    if not null_rows:
        print(f"[L4-NULL] no missing-ai_l4 rows for run_id={run_id}")
        return

    record_l4_nulls(null_rows, persist_to_snowflake=True, log_each_incident=False)


def _load_l123_merged(files: list[str]) -> pd.DataFrame | None:
    """Load and deduplicate L123 merged file(s)."""
    print(f"\nLoading L123 merged file(s) (preferred - has original columns)...")
    dfs: list[pd.DataFrame] = []
    for f in sorted(set(files)):
        try:
            df = pd.read_csv(f)
            print(f"  Loaded {f}: {len(df)} records, columns: {list(df.columns)[:12]}...")
            dfs.append(df)
        except Exception as e:
            print(f"  Warning: Could not read {f}: {e}")

    if not dfs:
        return None

    base_df = pd.concat(dfs, ignore_index=True)
    if "in_id" in base_df.columns:
        before = len(base_df)
        base_df = base_df.drop_duplicates(subset=["in_id"], keep="last")
        if before != len(base_df):
            print(f"  Deduplicated: {before} -> {len(base_df)} records")
    print(f"  L123 merged base ready: {len(base_df)} records")
    print(f"  Columns: {list(base_df.columns)}")
    return base_df


def _load_l123_from_checkpoints(
    l123_checkpoint_files: list[str],
    input_files: list[str],
) -> pd.DataFrame | None:
    """Fallback: merge L123 checkpoints with input files."""
    print(f"\nNo merged L123 file found, falling back to checkpoints + input...")

    # Load input files first (has original columns)
    input_df = None
    if input_files:
        input_dfs: list[pd.DataFrame] = []
        for f in sorted(set(input_files)):
            try:
                df = pd.read_csv(f)
                print(f"  Loaded input: {f}: {len(df)} records")
                input_dfs.append(df)
            except Exception as e:
                print(f"  Warning: Could not read {f}: {e}")
        if input_dfs:
            input_df = pd.concat(input_dfs, ignore_index=True)
            if "in_id" in input_df.columns:
                input_df = input_df.drop_duplicates(subset=["in_id"], keep="last")
            print(f"  Input base: {len(input_df)} records, columns: {list(input_df.columns)[:10]}...")

    base_df = None

    # Load L123 checkpoints
    if l123_checkpoint_files:
        checkpoint_dfs: list[pd.DataFrame] = []
        for f in sorted(set(l123_checkpoint_files)):
            try:
                df = pd.read_csv(f)
                df = normalize_columns(df, L123_COLUMN_MAP, "L123 checkpoint")
                checkpoint_dfs.append(df)
            except Exception as e:
                print(f"  Warning: Could not read {f}: {e}")

        if checkpoint_dfs:
            checkpoints_df = pd.concat(checkpoint_dfs, ignore_index=True)

            # Merge checkpoints into input if we have both
            if input_df is not None and "in_id" in input_df.columns:
                if "incident_id" in checkpoints_df.columns:
                    checkpoints_df = checkpoints_df.rename(columns={"incident_id": "in_id"})

                if "in_id" in checkpoints_df.columns:
                    checkpoints_df = checkpoints_df.drop_duplicates(subset=["in_id"], keep="last")
                    class_cols = [c for c in checkpoints_df.columns if c != "in_id"]
                    checkpoints_subset = checkpoints_df[["in_id"] + class_cols]
                    base_df = input_df.merge(checkpoints_subset, on="in_id", how="left")
                    print(f"  Merged checkpoints into input: {len(base_df)} records")
                else:
                    base_df = checkpoints_df
            else:
                base_df = checkpoints_df
                if "incident_id" in base_df.columns:
                    base_df = base_df.rename(columns={"incident_id": "in_id"})
    elif input_df is not None:
        base_df = input_df

    return base_df


def _merge_l4_into_base(base_df: pd.DataFrame, l4_checkpoint_files: list[str]) -> pd.DataFrame:
    """Merge L4 checkpoint files into the base dataframe."""
    print(f"\nMerging L4 checkpoint files into base...")

    for f in sorted(set(l4_checkpoint_files)):
        try:
            l4_df = pd.read_csv(f)
            print(f"  Processing {f}: {len(l4_df)} records, columns: {list(l4_df.columns)}")

            l4_df = normalize_columns(l4_df, L4_COLUMN_MAP, "L4 checkpoint")

            if "in_id" not in l4_df.columns:
                print(f"    Warning: No 'in_id' column after normalization, skipping")
                continue

            if "in_id" not in base_df.columns:
                print(f"    Warning: Base has no 'in_id' column, cannot merge")
                continue

            # Get L4-specific columns
            l4_specific_cols = ["in_id"] + [c for c in l4_df.columns if c.startswith("ai_l4")]
            if "ai_l4" in l4_df.columns and "ai_l4" not in l4_specific_cols:
                l4_specific_cols.append("ai_l4")

            l4_subset = l4_df[[c for c in l4_specific_cols if c in l4_df.columns]].drop_duplicates(
                subset=["in_id"], keep="last"
            )

            print(f"    L4 columns to merge: {[c for c in l4_specific_cols if c in l4_df.columns]}")

            before_rows = len(base_df)
            l4_ids = set(l4_subset["in_id"].dropna())
            base_ids = set(base_df["in_id"].dropna())
            matching = l4_ids & base_ids
            print(f"    Matching IDs: {len(matching)} of {len(l4_ids)} L4 records")

            base_df = base_df.merge(l4_subset, on="in_id", how="left", suffixes=("", "_l4_new"))

            # Handle column conflicts - prefer new value if not null
            for col in list(base_df.columns):
                if col.endswith("_l4_new"):
                    orig_col = col.replace("_l4_new", "")
                    if orig_col not in base_df.columns:
                        base_df = base_df.rename(columns={col: orig_col})
                    else:
                        base_df[orig_col] = base_df[col].combine_first(base_df[orig_col])
                        base_df = base_df.drop(columns=[col])

            print(f"    Merged: {len(base_df)} records (was {before_rows})")

        except Exception as e:
            import traceback

            print(f"  Error processing {f}: {e}")
            traceback.print_exc()

    return base_df


def merge_results(artifacts_dir: str | Path, output_file: str | Path) -> dict[str, Any]:
    """Merge all L123 and L4 classification results into a single output CSV.

    Args:
        artifacts_dir: Directory containing downloaded artifacts (CSV files).
        output_file: Path for the final merged CSV.

    Returns:
        Dict with merge stats: total_records, files_merged, output_file, columns.
    """
    artifacts_dir = Path(artifacts_dir)
    output_file = Path(output_file)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("WALLE Pipeline - Final Merge")
    print("=" * 60)

    print(f"\nSearching for CSV files in {artifacts_dir}/...")
    categories = discover_csv_files(artifacts_dir)

    l123_merged_files = categories["l123_merged"]
    l123_checkpoint_files = categories["l123_checkpoint"]
    l4_checkpoint_files = categories["l4_checkpoint"]
    input_files = categories["input"]

    # Build the base dataframe from L123 results
    base_df: pd.DataFrame | None = None

    if l123_merged_files:
        base_df = _load_l123_merged(l123_merged_files)

    if base_df is None and (l123_checkpoint_files or input_files):
        base_df = _load_l123_from_checkpoints(l123_checkpoint_files, input_files)

    if base_df is not None and "in_id" in base_df.columns:
        base_df = base_df.drop_duplicates(subset=["in_id"], keep="last")
        print(f"  Final L123 base: {len(base_df)} records, columns: {list(base_df.columns)[:10]}...")

    # Merge L4 checkpoint files
    if l4_checkpoint_files and base_df is not None:
        base_df = _merge_l4_into_base(base_df, l4_checkpoint_files)
    elif l4_checkpoint_files and base_df is None:
        print(f"\nNo L123 base found, loading L4 checkpoints as base...")
        l4_dfs: list[pd.DataFrame] = []
        for f in sorted(set(l4_checkpoint_files)):
            try:
                df = pd.read_csv(f)
                df = normalize_columns(df, L4_COLUMN_MAP, "L4 checkpoint")
                l4_dfs.append(df)
            except Exception as e:
                print(f"  Warning: Could not read {f}: {e}")
        if l4_dfs:
            base_df = pd.concat(l4_dfs, ignore_index=True)

    if base_df is None or base_df.empty:
        raise ValueError("No data loaded! Check that artifacts directory contains CSV files.")

    # Final deduplication
    if "in_id" in base_df.columns:
        before = len(base_df)
        base_df = base_df.drop_duplicates(subset=["in_id"], keep="last")
        after = len(base_df)
        if before != after:
            print(f"\nFinal dedup: removed {before - after} duplicates")

    merged = base_df

    # Verify key columns exist
    expected_cols = ["in_id", "ai_l1", "ai_l2", "ai_l3", "ai_l4", "ai_confidence", "ai_l4_confidence"]
    missing_cols = [c for c in expected_cols if c not in merged.columns]
    if missing_cols:
        print(f"\nWarning: Missing expected columns: {missing_cols}")

    # Print fill rates
    print(f"\nColumn fill rates:")
    for col in ["in_id", "ai_l1", "ai_l2", "ai_l3", "ai_l4", "ai_confidence", "ai_l4_confidence", "ai_l4_resolution_action"]:
        if col in merged.columns:
            non_null = merged[col].notna().sum()
            pct = 100 * non_null / len(merged) if len(merged) > 0 else 0
            print(f"  {col}: {non_null}/{len(merged)} ({pct:.1f}%)")

    # Save
    merged.to_csv(output_file, index=False)

    stats = {
        "total_records": len(merged),
        "files_merged": len(l123_merged_files) + len(l123_checkpoint_files) + len(l4_checkpoint_files),
        "output_file": str(output_file),
        "columns": list(merged.columns),
    }

    print(f"\n{'=' * 60}")
    print(f"Final merged file: {output_file}")
    print(f"Total records: {len(merged)}")
    print(f"Columns: {list(merged.columns)}")
    print(f"{'=' * 60}")

    return stats


def upload_to_s3(final_file: str | Path, run_id: str, artifacts_dir: str | Path | None = None) -> None:
    """Upload the final classified file and checkpoints to S3.

    Args:
        final_file: Path to the final merged CSV.
        run_id: Pipeline run identifier / timestamp.
        artifacts_dir: Optional directory to scan for checkpoint CSVs to upload.
    """
    from insights.utils.s3 import upload_artifact_to_s3

    final_file = Path(final_file)
    print("Uploading final results to S3...")

    result = upload_artifact_to_s3(final_file, run_id, "classified")
    if result:
        print(f"  Uploaded {final_file} to S3")
    else:
        print(f"  Failed to upload {final_file} to S3")

    # Also upload checkpoint files if artifacts_dir provided
    if artifacts_dir:
        for root, _dirs, files in os.walk(artifacts_dir):
            for f in files:
                if f.endswith(".csv") and "checkpoint" in f.lower():
                    path = Path(root) / f
                    upload_artifact_to_s3(path, run_id, "checkpoints")
                    print(f"  Uploaded checkpoint: {f}")

    print("S3 upload complete")


def upload_to_snowflake_db(final_file: str | Path, run_id: str) -> dict[str, Any]:
    """Upload the final classified file to Snowflake.

    Args:
        final_file: Path to the final merged CSV.
        run_id: Pipeline run identifier / timestamp.

    Returns:
        Dict with upload result (success, rows_uploaded, table, error).
    """
    from insights.utils.snowflake import test_snowflake_connection, upload_to_snowflake

    final_file = Path(final_file)

    print("Testing Snowflake connection...")
    if not test_snowflake_connection():
        print("WARNING: Snowflake connection failed, skipping upload")
        return {"success": False, "error": "Connection failed"}

    print(f"Loading data from {final_file}...")
    df = pd.read_csv(final_file)
    print(f"Loaded {len(df)} records")

    print("Uploading to Snowflake...")
    result = upload_to_snowflake(df, run_id)

    if result.get("success"):
        print(f"  Successfully uploaded {result.get('rows_uploaded', 0)} rows to Snowflake")
        print(f"  Table: {result.get('table', 'N/A')}")
    else:
        print(f"  Snowflake upload failed: {result.get('error', 'Unknown error')}")

    return result


def run_finalize(
    artifacts_dir: str | Path,
    run_id: str,
    output_file: str | Path | None = None,
    skip_s3: bool = False,
    skip_snowflake: bool = False,
    environment: str = "prod",
) -> dict[str, Any]:
    """Run the full finalize step: merge, upload to S3, upload to Snowflake.

    Args:
        artifacts_dir: Directory containing downloaded artifacts (CSV files).
        run_id: Pipeline run identifier / timestamp.
        output_file: Path for the final merged CSV. Auto-generated if not provided.
        skip_s3: Skip S3 upload.
        skip_snowflake: Skip Snowflake upload.
        environment: Target environment (dev/stage/prod).

    Returns:
        Dict with finalize results.
    """
    if output_file is None:
        output_file = Path("data/output") / f"walle_classified_incidents_{run_id}.csv"
    output_file = Path(output_file)

    print(f"Run ID: {run_id}")
    print(f"Environment: {environment}")
    print(f"Artifacts dir: {artifacts_dir}")
    print(f"Output file: {output_file}")
    print(f"S3 upload: {'skip' if skip_s3 else 'enabled'}")
    print(f"Snowflake upload: {'skip' if skip_snowflake else 'enabled'}")
    print()

    # Step 1: Merge
    merge_stats = merge_results(artifacts_dir, output_file)

    # Step 1b: Req 1+3 Scenario P1 path-integrity backfill.
    # Any row that arrives at finalize with NULL/empty/'none' L1/L2/L3
    # is rewritten to "Uncategorized" in the merged CSV BEFORE S3 and
    # Snowflake uploads, and we emit a `pipeline_gap` audit row per
    # affected incident. This guarantees the new-incident invariant:
    # `WALLE_CLASSIFIED_INCIDENTS` never receives NULL ai_l1/ai_l2/ai_l3
    # for rows produced by this run. Best-effort: backfill scan errors
    # never block the upload pipeline.
    try:
        backfilled_ids = _finalize_backfill_null_l123(Path(output_file))
        if backfilled_ids:
            _emit_l123_backfill_audit(backfilled_ids, run_id)
    except Exception as exc:
        print(f"[REQ-1+3] Finalize NULL-L123 backfill failed (non-fatal): {exc}")

    # Step 2: S3 upload
    s3_result = None
    if not skip_s3:
        try:
            upload_to_s3(output_file, run_id, artifacts_dir)
            s3_result = "success"
        except Exception as e:
            print(f"S3 upload error: {e}")
            s3_result = f"error: {e}"
    else:
        s3_result = "skipped"

    # Step 3: Snowflake upload
    sf_result: dict[str, Any] | str = "skipped"
    if not skip_snowflake:
        try:
            sf_result = upload_to_snowflake_db(output_file, run_id)
        except Exception as e:
            print(f"Snowflake upload error: {e}")
            sf_result = {"success": False, "error": str(e)}

    # Summary
    print()
    print("=" * 42)
    print("WALLE Pipeline Summary")
    print("=" * 42)
    print(f"Run Timestamp: {run_id}")
    print(f"Environment: {environment}")
    print(f"Final Records: {merge_stats['total_records']}")
    print(f"Final Output: {output_file}")
    print()
    print("Uploads:")
    print(f"  S3 Blob Storage: {s3_result}")
    sf_status = sf_result.get("success", False) if isinstance(sf_result, dict) else sf_result
    print(f"  Snowflake: {sf_status}")
    print("=" * 42)

    # Final-state AI_L4 NULL audit (parity with ClassificationPipeline.run()
    # in insights/pipeline/runner.py lines 375-429). Best-effort: never fail
    # the workflow on audit hiccups - the main-table upload above is the
    # source of truth for finalize's exit status.
    try:
        _emit_final_l4_null_audit(output_file, run_id)
    except Exception as exc:
        print(f"AI_L4 NULL final-reason logging failed (non-fatal): {exc}")

    # Write stats file alongside output
    stats_file = output_file.parent / "merge_stats.json"
    with open(stats_file, "w") as f:
        json.dump(merge_stats, f)

    return {
        "merge_stats": merge_stats,
        "s3_result": s3_result,
        "snowflake_result": sf_result,
        "output_file": str(output_file),
    }
