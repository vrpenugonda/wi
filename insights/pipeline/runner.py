"""
Classification Pipeline Runner

Orchestrates the two-step classification process:
1. L1/L2/L3 classification (Category, Subcategory, Product)
2. L4 classification (Resolution categories with actionability)

This module provides a unified pipeline that can be run manually
or scheduled to run every 15 minutes.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from ..config import settings
from ..classifiers import L123Classifier, L4Classifier
from ..models import PipelineResult, ClassificationStatus
from ..utils import (
    DataLoader,
    upload_taxonomy_to_s3,
    download_taxonomy_from_s3,
    get_run_id,
    upload_run_artifacts,
    cleanup_old_artifact_runs,
    upload_to_snowflake,
    get_rate_limiter,
    reset_rate_limiter,
)


@dataclass
class PipelineStats:
    """Statistics from a pipeline run."""
    
    start_time: datetime = field(default_factory=datetime.now)
    end_time: datetime | None = None
    
    # L123 stats
    l123_total: int = 0
    l123_processed: int = 0
    l123_success: int = 0
    l123_failed: int = 0
    
    # L4 stats
    l4_total: int = 0
    l4_processed: int = 0
    l4_success: int = 0
    l4_failed: int = 0
    l4_actionable: int = 0
    l4_non_actionable: int = 0
    
    # Subcategory breakdown
    subcategory_stats: dict[str, dict] = field(default_factory=dict)
    
    @property
    def duration_seconds(self) -> float:
        """Get pipeline duration in seconds."""
        end = self.end_time or datetime.now()
        return (end - self.start_time).total_seconds()
    
    def summary(self) -> str:
        """Generate a summary string."""
        lines = [
            "=" * 60,
            "PIPELINE RUN SUMMARY",
            "=" * 60,
            f"Duration: {self.duration_seconds:.1f}s",
            "",
            "L1/L2/L3 Classification:",
            f"  Total: {self.l123_total}",
            f"  Processed: {self.l123_processed}",
            f"  Success: {self.l123_success}",
            f"  Failed: {self.l123_failed}",
            "",
            "L4 Classification:",
            f"  Total: {self.l4_total}",
            f"  Processed: {self.l4_processed}",
            f"  Success: {self.l4_success}",
            f"  Failed: {self.l4_failed}",
            f"  Actionable: {self.l4_actionable}",
            f"  Non-actionable: {self.l4_non_actionable}",
        ]
        
        if self.subcategory_stats:
            lines.extend(["", "By Subcategory:"])
            for subcat, stats in sorted(self.subcategory_stats.items()):
                lines.append(
                    f"  {subcat}: {stats.get('success', 0)}/{stats.get('total', 0)} "
                    f"(actionable: {stats.get('actionable', 0)})"
                )
        
        lines.append("=" * 60)
        return "\n".join(lines)


class ClassificationPipeline:
    """
    Two-step classification pipeline.
    
    This orchestrates:
    1. L123 classification - assigns Category, Subcategory, Product
    2. L4 classification - assigns resolution categories per subcategory
    
    The pipeline:
    - Tracks progress with checkpoints
    - Derives L4 taxonomies per subcategory (or loads existing)
    - Handles errors gracefully with retries
    - Provides detailed statistics
    """
    
    def __init__(
        self,
        batch_size: int | None = None,
        workers: int | None = None,
        debug: bool = False,
        disable_taxonomy_validation: bool = False,
    ):
        self.batch_size = batch_size or settings.batch_size
        self.workers = workers or settings.workers
        self.debug = debug
        self.disable_taxonomy_validation = disable_taxonomy_validation
        self.loader = DataLoader()
        self.stats = PipelineStats()
    
    async def run(
        self,
        input_file: str,
        output_dir: str | Path | None = None,
        l4_subcategories: list[str] | None = None,
        skip_l123: bool = False,
        skip_l4: bool = False,
        l4_sample_size: int | None = None,
        generate_taxonomy: bool = False,
        taxonomy_days: int = 7,
    ) -> PipelineResult:
        """
        Run the full classification pipeline.
        
        Args:
            input_file: Path to input CSV with raw incidents
            output_dir: Directory for outputs (default: settings.output_dir)
            l4_subcategories: Specific subcategories for L4 (default: all)
            skip_l123: Skip L123 classification (use existing)
            skip_l4: Skip L4 classification
            l4_sample_size: Sample size for L4 taxonomy derivation
            generate_taxonomy: Force regeneration of L4 taxonomies (otherwise use S3 cache)
            taxonomy_days: Days of data to use for taxonomy generation
        
        Returns:
            PipelineResult with status and file paths
        """
        self.stats = PipelineStats()
        self._generate_taxonomy = generate_taxonomy
        self._taxonomy_days = taxonomy_days
        output_path = Path(output_dir) if output_dir else settings.output_dir
        output_path.mkdir(parents=True, exist_ok=True)
        
        input_path = Path(input_file)
        base_name = input_path.stem
        
        # Define output file paths
        l123_output = output_path / f"{base_name}_l123_classified.csv"
        l123_checkpoint = settings.checkpoint_dir / f"{base_name}_l123_checkpoint.csv"
        
        logger.info(f"Pipeline starting: {input_file} -> {output_path}")
        
        # Step 1: L1/L2/L3 Classification
        if not skip_l123:
            l123_file = await self._run_l123(
                input_file=input_file,
                checkpoint_file=str(l123_checkpoint),
            )
        else:
            # Use existing L123 output
            if l123_checkpoint.exists():
                l123_file = str(l123_checkpoint)
                logger.info(f"Using existing L123 output: {l123_file}")
            else:
                raise ValueError(f"skip_l123=True but no checkpoint found: {l123_checkpoint}")
        
        if skip_l4:
            self.stats.end_time = datetime.now()
            return PipelineResult(
                status=ClassificationStatus.COMPLETED,
                l123_output=l123_file,
                l4_outputs={},
                stats=self.stats.summary(),
            )
        
        # Step 2: L4 Classification
        l4_outputs = await self._run_l4(
            l123_file=l123_file,
            original_file=input_file,
            output_dir=output_path,
            subcategories=l4_subcategories,
            sample_size=l4_sample_size,
        )
        
        self.stats.end_time = datetime.now()
        
        # Create merged final output with all original fields + classifications
        from datetime import datetime as dt
        import pandas as pd
        
        timestamp = dt.now().strftime("%Y%m%d_%H%M%S")
        
        # Load original data
        original_df = self.loader.load_csv(input_file)
        if original_df is None:
            original_df = pd.DataFrame()
        
        # Load L123 results and merge
        l123_df = self.loader.load_csv(l123_file)
        if l123_df is not None and not l123_df.empty:
            # Strip Req 1+3 sidecar columns so they do not propagate into
            # the final dataset. They are only used between the classifier
            # and the audit emission step.
            sidecar_cols = [c for c in l123_df.columns if c.startswith("_validation") or c.startswith("_original_l") or c == "_repair_applied"]
            if sidecar_cols:
                l123_df = l123_df.drop(columns=sidecar_cols, errors="ignore")
            # Rename L123 columns with ai_l1/l2/l3 naming
            l123_rename = {
                'category': 'ai_l1',
                'subcategory': 'ai_l2', 
                'product': 'ai_l3',
                'confidence_score': 'ai_confidence',
                'rationale': 'ai_rationale',
                'self_resolved': 'ai_self_resolved',
                'keywords_identified': 'ai_keywords',
                'root_cause': 'ai_root_cause',
                'root_cause_indicator': 'ai_root_cause_indicator',
            }
            l123_df = l123_df.rename(columns={k: v for k, v in l123_rename.items() if k in l123_df.columns})
            
            # Merge on incident_id -> in_id
            if 'incident_id' in l123_df.columns and 'in_id' in original_df.columns:
                merged_df = original_df.merge(
                    l123_df,
                    left_on='in_id',
                    right_on='incident_id',
                    how='left'
                )
            else:
                merged_df = original_df
        else:
            merged_df = original_df
        
        # Load and merge L4 results - first combine all L4 checkpoints into one DataFrame
        all_l4_dfs = []
        # Track per-incident invalid values we clean to NULL (final-reason logging).
        # Must be defined even if no L4 checkpoints exist.
        invalid_cleaned_original: dict[str, str] = {}
        logger.info(f"Merging L4 results from {len(l4_outputs)} subcategories: {list(l4_outputs.keys())}")
        for subcat, checkpoint in l4_outputs.items():
            checkpoint_path = Path(checkpoint)
            if not checkpoint_path.exists():
                logger.warning(f"  {subcat}: checkpoint not found, skipping")
                continue
            l4_df = self.loader.load_csv(checkpoint)
            if l4_df is not None and not l4_df.empty:
                logger.info(f"  {subcat}: {len(l4_df)} records, columns: {list(l4_df.columns)}")
                all_l4_dfs.append(l4_df)
        
        if all_l4_dfs:
            # Concatenate all L4 results
            combined_l4_df = pd.concat(all_l4_dfs, ignore_index=True)
            logger.debug(f"Combined L4 df: {len(combined_l4_df)} records")
            
            # Clean up invalid L4 categories - but KEEP Unclassified (it's a valid classification)
            # Only remove truly invalid categories the model invents
            if 'l4_category' in combined_l4_df.columns:
                # Normalize any legacy "Unclassified_L4" rows (from older runs) to canonical "Unclassified"
                legacy_mask = combined_l4_df['l4_category'].astype(str).str.lower() == 'unclassified_l4'
                if legacy_mask.any():
                    combined_l4_df.loc[legacy_mask, 'l4_category'] = 'Unclassified'
                    logger.info(f"Normalized {int(legacy_mask.sum())} legacy 'Unclassified_L4' rows to 'Unclassified'")

                # Note: Do NOT remove "unclassified" - it's a valid category indicating insufficient info
                # Only remove invented categories like "insufficient_information", "unknown", etc.
                invalid_pattern = r'^(insufficient|unknown|missing|unable_to_classify|pending_details|n/a|none)$|insufficient_|_unknown$|_missing$'
                invalid_mask = combined_l4_df['l4_category'].str.lower().str.match(invalid_pattern, na=False)
                invalid_count = invalid_mask.sum()
                if invalid_count > 0:
                    # Capture original invalid values for final per-incident reason logging
                    if "incident_id" in combined_l4_df.columns:
                        try:
                            tmp = combined_l4_df.loc[invalid_mask, ["incident_id", "l4_category"]].dropna()
                            for _in_id, _val in tmp.itertuples(index=False, name=None):
                                invalid_cleaned_original[str(_in_id)] = str(_val)
                        except Exception:
                            # Best-effort only; do not fail the pipeline on logging metadata capture
                            pass
                    # Set invalid categories to None - they will show as blank
                    combined_l4_df.loc[invalid_mask, 'l4_category'] = None
                    logger.info(f"Removed {invalid_count} invalid L4 categories (not Unclassified)")

                # Log Unclassified count for monitoring
                unclassified_count = (combined_l4_df['l4_category'].str.lower() == 'unclassified').sum()
                total_count = len(combined_l4_df)
                logger.info(f"L4 Classification: {total_count - unclassified_count}/{total_count} ({100*(total_count - unclassified_count)/total_count:.1f}%) classified, {unclassified_count} ({100*unclassified_count/total_count:.1f}%) Unclassified")
            
            # Rename L4 columns with ai_l4 prefix
            l4_rename = {
                'l4_category': 'ai_l4',
                'l4_confidence': 'ai_l4_confidence',
                'is_actionable': 'ai_l4_actionable',
                'actionability_reason': 'ai_l4_actionability_reason',
                'resolution_action': 'ai_l4_resolution_action',
                'l4_rationale': 'ai_l4_rationale',
            }
            combined_l4_df = combined_l4_df.rename(columns={k: v for k, v in l4_rename.items() if k in combined_l4_df.columns})
            
            # Merge on incident_id -> in_id (single merge)
            if 'incident_id' in combined_l4_df.columns and 'in_id' in merged_df.columns:
                # Only keep L4 classification columns for merge (ai_l4 or ai_l4_*)
                l4_cols = [c for c in combined_l4_df.columns if c == 'ai_l4' or c.startswith('ai_l4_')]
                l4_subset = combined_l4_df[['incident_id'] + l4_cols].copy()
                # Drop duplicates in case same incident appears in multiple checkpoints
                l4_subset = l4_subset.drop_duplicates(subset=['incident_id'], keep='first')
                # Rename incident_id to match merge key
                l4_subset = l4_subset.rename(columns={'incident_id': 'in_id'})
                merged_df = merged_df.merge(
                    l4_subset,
                    on='in_id',
                    how='left',
                )
                l4_matched = merged_df['ai_l4'].notna().sum() if 'ai_l4' in merged_df.columns else 0
                logger.info(f"L4 merge: {l4_matched}/{len(merged_df)} incidents matched")
            else:
                logger.warning("Cannot merge L4 data: missing incident_id or in_id columns")
        
        # Clean up any duplicate columns that may have been created
        # Remove columns ending with _x or _y suffixes, and the duplicate incident_id column
        # Note: don't drop ai_l4 columns - only drop legacy _l4 suffixed columns like subcategory_l4
        cols_to_drop = [c for c in merged_df.columns if c.endswith('_x') or c.endswith('_y') or (c.endswith('_l4') and not c.startswith('ai_l4')) or c == 'incident_id']
        if cols_to_drop:
            merged_df = merged_df.drop(columns=cols_to_drop, errors='ignore')
        
        # Reorder columns: original fields first, then all AI classification fields grouped together
        original_cols = ['in_id', 'assignment', 'first_assignment_group', 'brief_description',
                         'opened_at', 'closed_at',
                         'action', 'resolution', 'update_action_ess', 'uh_ess_errormsg', 
                         'update_action', 'comments', 'uh_monitoring_notes']
        # Group all AI category columns together: L1, L2, L3, L4
        ai_category_cols = ['ai_l1', 'ai_l2', 'ai_l3', 'ai_l4']
        # L123 additional fields
        l123_detail_cols = ['vendor', 'ai_confidence', 'ai_self_resolved', 'ai_rationale', 
                            'ai_keywords', 'ai_root_cause_indicator', 'ai_root_cause']
        # L4 additional fields
        l4_detail_cols = ['ai_l4_confidence', 'ai_l4_resolution_action',
                          'ai_l4_actionable', 'ai_l4_actionability_reason', 'ai_l4_rationale']
        
        # Build ordered column list, only including columns that exist
        ordered_cols = []
        for col in original_cols + ai_category_cols + l123_detail_cols + l4_detail_cols:
            if col in merged_df.columns:
                ordered_cols.append(col)
        # Add any remaining columns not in the ordered list
        remaining_cols = [c for c in merged_df.columns if c not in ordered_cols]
        merged_df = merged_df[ordered_cols + remaining_cols]
        
        # Save final merged output
        final_output = output_path / f"classified_incidents_{timestamp}.csv"
        merged_df.to_csv(final_output, index=False)
        logger.info(f"Final output: {final_output} ({len(merged_df):,} records)")
        
        # Use a single run_id for all downstream side effects (artifacts, Snowflake, NULL-reason audit)
        run_id = get_run_id()

        # Final-state AI_L4 NULL logging (one row per incident whose AI_L4 is missing)
        try:
            from ..utils.l4_null_logging import L4NullRow, record_l4_nulls
            import pandas as pd

            def _is_missing_l4(v) -> bool:
                # pandas often represents missing strings as NaN, not None
                if v is None or pd.isna(v):
                    return True
                s = str(v).strip()
                return s == "" or s.lower() == "none"

            if "in_id" in merged_df.columns:
                l4_series = merged_df["ai_l4"] if "ai_l4" in merged_df.columns else None
                if l4_series is not None:
                    missing_mask = l4_series.apply(_is_missing_l4)
                else:
                    # If ai_l4 column is absent, treat all as missing for auditing
                    missing_mask = merged_df["in_id"].apply(lambda _: True)

                null_rows: list[L4NullRow] = []
                for row in merged_df.loc[missing_mask].itertuples(index=False):
                    # Access by attribute if possible; fall back to dict-like via _asdict().
                    d = row._asdict() if hasattr(row, "_asdict") else {}
                    in_id = str(d.get("in_id") or "")
                    if not in_id:
                        continue
                    subcat = d.get("ai_l2")

                    if in_id in invalid_cleaned_original:
                        null_rows.append(
                            L4NullRow(
                                in_id=in_id,
                                reason="l4_invalid_category_cleaned",
                                cause="invalid_pattern_cleanup",
                                subcategory=str(subcat) if subcat is not None else None,
                                walle_run_id=run_id,
                                original_value=invalid_cleaned_original.get(in_id),
                            )
                        )
                    else:
                        null_rows.append(
                            L4NullRow(
                                in_id=in_id,
                                reason="l4_missing_after_l4_run",
                                cause="ai_l4_missing_in_final_merge",
                                subcategory=str(subcat) if subcat is not None else None,
                                walle_run_id=run_id,
                            )
                        )

                if null_rows:
                    record_l4_nulls(null_rows, persist_to_snowflake=True, log_each_incident=False)
        except Exception as e:
            logger.warning("AI_L4 NULL final-reason logging failed (non-fatal): %s", e)

        # Upload artifacts to S3
        logger.info(f"Uploading artifacts to S3 (run: {run_id})")
        
        # Find log file if it exists
        log_file = None
        if settings.logs_dir.exists():
            log_files = list(settings.logs_dir.glob("walle*.log"))
            if log_files:
                log_file = max(log_files, key=lambda p: p.stat().st_mtime)  # Most recent
        
        upload_run_artifacts(
            run_id=run_id,
            incidents_file=Path(input_file),
            classified_file=final_output,
            log_file=log_file,
        )
        
        # Cleanup old runs (keep last 5)
        cleanup_old_artifact_runs()
        
        # Upload to Snowflake
        logger.info("Uploading to Snowflake")
        sf_result = upload_to_snowflake(merged_df, run_id)
        if sf_result.get("success"):
            logger.info(f"Snowflake upload complete: {sf_result.get('rows_uploaded', 0)} rows")
        else:
            logger.warning(f"Snowflake upload issue: {sf_result.get('error', 'Unknown error')}")
        
        # Log summary
        logger.info(f"Pipeline complete. Duration: {self.stats.duration_seconds:.1f}s, Success: {self.stats.l123_success + self.stats.l4_success}, Failed: {self.stats.l123_failed + self.stats.l4_failed}")
        
        return PipelineResult(
            status=ClassificationStatus.COMPLETED,
            l123_output=str(final_output),
            l4_outputs=l4_outputs,
            stats=self.stats.summary(),
        )
    
    async def _run_l123(
        self,
        input_file: str,
        checkpoint_file: str,
    ) -> str:
        """Run L1/L2/L3 classification step."""
        logger.info("Starting L1/L2/L3 classification")
        
        # Load input data
        df = self.loader.load_csv(input_file)
        if df is None or df.empty:
            raise ValueError(f"No data in {input_file}")
        
        self.stats.l123_total = len(df)
        
        # Check existing progress
        checkpoint_df = self.loader.load_checkpoint(checkpoint_file)
        pending_df = self.loader.get_pending_records(df, checkpoint_df, id_column="in_id")
        
        already_processed = len(df) - len(pending_df)
        logger.info(f"L123: {len(df):,} total, {already_processed:,} done, {len(pending_df):,} pending")
        
        self.stats.l123_processed = already_processed
        
        if pending_df.empty:
            logger.info("L123: All records already classified")
            return checkpoint_file
        
        # Create classifier
        classifier = L123Classifier(
            batch_size=self.batch_size,
            workers=self.workers,
            debug=self.debug,
            disable_taxonomy_validation=self.disable_taxonomy_validation,
        )
        
        # Run classification
        incidents: list[dict[str, Any]] = pending_df.to_dict(orient="records")  # type: ignore[assignment]
        results = await classifier.classify_all(incidents)
        
        # Save results
        valid_results = [r for r in results if r is not None]
        if valid_results:
            self.loader.append_results(
                checkpoint_file,
                valid_results,
            )

        # Emit L123 taxonomy audit rows for any non-`valid` outcomes
        # produced in this invocation (Req 1+3). Best-effort; never raises.
        try:
            self._emit_l123_audit(valid_results)
        except Exception as exc:
            logger.warning("L123 audit emission failed (non-fatal): %s", exc)

        # Update stats
        self.stats.l123_processed += len(results)
        self.stats.l123_success = already_processed + len(valid_results)
        self.stats.l123_failed = len(results) - len(valid_results)
        
        logger.info(f"L123: Classified {len(valid_results)}/{len(incidents)} ({self.stats.l123_success} total success)")
        
        return checkpoint_file

    def _emit_l123_audit(self, results: list[dict[str, Any]]) -> None:
        """Build and persist L123 audit rows for a batch of classifier results.

        Only rows whose `_validation_status` is NOT `valid` are persisted.
        `valid_after_repair` rows ARE persisted so we can track the impact
        of the alias map / normalization. Rows missing the sidecar field
        (e.g., produced by an older classifier or with the validator
        disabled) are skipped.
        """
        if not results:
            return
        if not getattr(settings, "l123_audit_persist", True):
            logger.info("L123 audit persistence disabled by settings; skipping")
            return

        from ..utils.l123_audit_logging import L123AuditRow, record_l123_audit
        from ..utils import get_run_id

        run_id = get_run_id()

        rows: list[L123AuditRow] = []
        for r in results:
            status = r.get("_validation_status")
            if not status or status == "valid":
                continue
            in_id = (
                r.get("incident_id")
                or r.get("in_id")
                or r.get("Incident ID")
            )
            if not in_id:
                continue
            details: dict[str, Any] | None = None
            details_raw = r.get("_validation_details")
            if isinstance(details_raw, str) and details_raw:
                try:
                    import json as _json

                    details = _json.loads(details_raw)
                except Exception:
                    details = {"raw": details_raw}

            rows.append(
                L123AuditRow(
                    in_id=str(in_id),
                    walle_run_id=run_id,
                    status=status,
                    original_l1=r.get("_original_l1"),
                    original_l2=r.get("_original_l2"),
                    original_l3=r.get("_original_l3"),
                    final_l1=r.get("category"),
                    final_l2=r.get("subcategory"),
                    final_l3=r.get("product"),
                    repair_applied=bool(r.get("_repair_applied")),
                    details=details,
                )
            )

        if not rows:
            return

        record_l123_audit(
            rows,
            persist_to_snowflake=getattr(settings, "l123_audit_persist", True),
            table_name=getattr(
                settings, "l123_audit_table", "WALLE_L123_TAXONOMY_AUDIT"
            ),
        )

    def _build_uncategorized_l4_checkpoint(
        self,
        uncategorized_df,
        id_col: str,
    ) -> str:
        """Synthesize an L4 checkpoint for the L123-Uncategorized cohort.

        Each row gets `l4_category = "Unclassified"` plus stub values for
        the other L4 fields. The checkpoint is written to the standard
        checkpoint directory and its path is returned so it can be added
        to `l4_outputs` and picked up by the normal final merge.

        Also emits one `WALLE_L4_NULL_REASONS` row per incident with
        reason `l123_invalid_blocks_l4` so analytics can separate
        blocked-by-policy from pipeline-failure cohorts.
        """
        import pandas as pd

        if uncategorized_df is None or uncategorized_df.empty:
            return ""

        ids = uncategorized_df[id_col].astype(str).tolist()
        synth_rows = []
        for in_id in ids:
            synth_rows.append(
                {
                    "incident_id": in_id,
                    "l4_category": "Unclassified",
                    "l4_subcategory": None,
                    "l4_confidence": 0.0,
                    "resolution_action": "Not attempted (L123 was Uncategorized)",
                    "is_actionable": False,
                    "actionability_reason": (
                        "L123 classification was Uncategorized; L4 not attempted "
                        "by design (Req 1+3 path integrity)"
                    ),
                    "l4_rationale": "l123_invalid_blocks_l4",
                }
            )
        synth_df = pd.DataFrame(synth_rows)

        checkpoint_path = settings.checkpoint_dir / "l4_uncategorized_synthetic_checkpoint.csv"
        # Deduplicate against any existing rows in this run so resume is safe
        if checkpoint_path.exists():
            try:
                existing = pd.read_csv(checkpoint_path)
                if "incident_id" in existing.columns:
                    existing_ids = set(existing["incident_id"].astype(str).tolist())
                    synth_df = synth_df[~synth_df["incident_id"].astype(str).isin(existing_ids)]
                    if not synth_df.empty:
                        combined = pd.concat([existing, synth_df], ignore_index=True)
                    else:
                        combined = existing
                else:
                    combined = synth_df
            except Exception:
                combined = synth_df
        else:
            combined = synth_df

        combined.to_csv(checkpoint_path, index=False)

        # Emit WALLE_L4_NULL_REASONS rows with the new reason code.
        try:
            from ..utils.l4_null_logging import L4NullRow, record_l4_nulls
            from ..utils import get_run_id

            run_id = get_run_id()
            null_rows: list[L4NullRow] = []
            for in_id in ids:
                null_rows.append(
                    L4NullRow(
                        in_id=str(in_id),
                        reason="l123_invalid_blocks_l4",
                        cause=(
                            "L123 classification was Uncategorized; L4 not attempted "
                            "by design (Req 1+3 path integrity)"
                        ),
                        subcategory="Uncategorized",
                        walle_run_id=run_id,
                        original_value=None,
                    )
                )
            if null_rows:
                record_l4_nulls(
                    null_rows,
                    persist_to_snowflake=True,
                    log_each_incident=False,
                )
        except Exception as exc:
            logger.warning(
                "Failed to record l123_invalid_blocks_l4 reasons (non-fatal): %s",
                exc,
            )

        return str(checkpoint_path)
    
    async def _run_l4(
        self,
        l123_file: str,
        original_file: str,
        output_dir: Path,
        subcategories: list[str] | None = None,
        sample_size: int | None = None,
    ) -> dict[str, str]:
        """Run L4 classification step for each subcategory in parallel."""
        import asyncio
        import pandas as pd
        
        logger.info("Starting L4 classification (parallel)")
        
        # Reset rate limiter for this run
        reset_rate_limiter()
        rate_limiter = get_rate_limiter(550)  # 550 RPM limit
        
        # Load L123 classified data
        l123_df = self.loader.load_csv(l123_file)
        if l123_df is None or l123_df.empty:
            raise ValueError(f"No L123 data in {l123_file}")
        
        # Load original incident data (has brief_description, resolution, etc.)
        original_df = self.loader.load_csv(original_file)
        if original_df is None or original_df.empty:
            raise ValueError(f"No original data in {original_file}")
        
        # Find the ID column in both dataframes
        l123_id_col = None
        for col in ['incident_id', 'in_id', 'Incident ID']:
            if col in l123_df.columns:
                l123_id_col = col
                break
        if l123_id_col is None:
            raise ValueError(f"No ID column found in L123 file. Columns: {list(l123_df.columns)}")
        
        orig_id_col = None
        for col in ['incident_id', 'in_id', 'Incident ID']:
            if col in original_df.columns:
                orig_id_col = col
                break
        
        if orig_id_col is None:
            raise ValueError(f"No ID column found in original file. Columns: {list(original_df.columns)}")
        
        # Merge L123 results with original data so L4 has access to all fields
        # L123 has classification results, original has brief_description, resolution, etc.
        logger.info(f"Merging L123 ({len(l123_df)} rows) with original ({len(original_df)} rows)")
        
        # Rename original ID column to match
        if orig_id_col != l123_id_col:
            original_df = original_df.rename(columns={orig_id_col: l123_id_col})
        
        # Merge - keep all L123 rows, add original columns
        df = l123_df.merge(original_df, on=l123_id_col, how='left', suffixes=('', '_orig'))
        
        logger.info(f"Merged data: {len(df)} rows with {len(df.columns)} columns")
        
        # Log key column availability for debugging L4 data issues
        key_cols = ['brief_description', 'resolution', 'action', 'comments', 
                    'category', 'subcategory', 'product', 'rationale', 'root_cause']
        available_key_cols = [c for c in key_cols if c in df.columns]
        missing_key_cols = [c for c in key_cols if c not in df.columns]
        if missing_key_cols:
            logger.warning(f"L4 merge: missing columns {missing_key_cols}")
        logger.debug(f"L4 merge: key columns available: {available_key_cols}")
        
        self.stats.l4_total = len(df)
        
        # Normalize column names - L123 checkpoint uses 'subcategory', final output uses 'ai_l2'
        subcategory_col = None
        category_col = None
        for col in ["ai_l2", "subcategory"]:
            if col in df.columns:
                subcategory_col = col
                break
        for col in ["ai_l1", "category"]:
            if col in df.columns:
                category_col = col
                break
        
        # === Pre-L4 path-integrity gate (Req 1+3) ===
        # Incidents whose L123 result was bucketed to "Uncategorized" must
        # NOT be sent to L4 classification — they have no valid subcategory
        # the L4 model can classify under. We synthesize an L4 checkpoint
        # for this cohort with `l4_category = "Unclassified"` and emit
        # `WALLE_L4_NULL_REASONS` rows with reason `l123_invalid_blocks_l4`.
        #
        # Scenario O: NULL/empty/'none' subcategory values must be treated
        # the same as the literal "Uncategorized" string. NaN != string in
        # pandas, so a strict equality mask used to leak those rows past
        # the gate; they then ended up with NULL ai_l4 and zero audit
        # coverage.
        def _is_uncat_or_null(v: Any) -> bool:
            if v is None:
                return True
            try:
                if pd.isna(v):
                    return True
            except Exception:
                pass
            s = str(v).strip()
            return s == "" or s == "Uncategorized" or s.lower() == "none"

        uncategorized_synthetic_checkpoint: str | None = None
        if subcategory_col is not None:
            try:
                uncategorized_mask = df[subcategory_col].apply(_is_uncat_or_null)
            except Exception:
                uncategorized_mask = None  # type: ignore[assignment]
            if uncategorized_mask is not None and bool(uncategorized_mask.any()):
                uncategorized_df = df[uncategorized_mask].copy()
                df = df[~uncategorized_mask].copy()
                # Normalize the cohort's L1/L2/L3 to the canonical
                # Uncategorized bucket so downstream merges and the
                # synthetic checkpoint see consistent values for rows
                # that arrived NULL/empty/'none'.
                for _norm_col in ("ai_l1", "ai_l2", "ai_l3", "category", "subcategory", "product"):
                    if _norm_col in uncategorized_df.columns:
                        try:
                            uncategorized_df[_norm_col] = "Uncategorized"
                        except Exception:
                            pass
                try:
                    uncategorized_synthetic_checkpoint = self._build_uncategorized_l4_checkpoint(
                        uncategorized_df,
                        id_col=l123_id_col,
                    )
                    print(
                        f"[L4] Pre-L4 gate: routed {len(uncategorized_df):,} "
                        f"Uncategorized-L123 incidents to ai_l4='Unclassified'"
                    )
                    logger.info(
                        "Pre-L4 path-integrity gate: %d incidents bucketed to Unclassified "
                        "(reason=l123_invalid_blocks_l4)",
                        len(uncategorized_df),
                    )
                except Exception as exc:
                    logger.warning(
                        "Pre-L4 path-integrity gate failed (non-fatal): %s; "
                        "Uncategorized cohort will fall through to standard flow",
                        exc,
                    )
                    uncategorized_synthetic_checkpoint = None

        # Determine subcategories to process
        if subcategories:
            target_subcats = subcategories
        else:
            # Get all unique subcategories
            if subcategory_col:
                target_subcats = df[subcategory_col].dropna().unique().tolist()
                # Defensive: never include the controlled Uncategorized bucket
                target_subcats = [s for s in target_subcats if str(s) != "Uncategorized"]
                print(f"[L4] Found {len(target_subcats)} unique subcategories in '{subcategory_col}' column")
            else:
                print("[L4] Warning: No subcategory column found, treating as single group")
                target_subcats = ["All"]
        
        print(f"[L4] Processing {len(target_subcats)} subcategories IN PARALLEL")
        print(f"[L4] Rate limit: {rate_limiter.max_rpm} RPM")
        
        # Determine ID column for checkpointing
        id_col_for_checkpoint = l123_id_col  # Use whatever ID column the merged data has
        
        # Prepare subcategory data
        subcat_data = {}
        for subcat in target_subcats:
            if subcat == "All":
                subcat_df = df
            elif subcategory_col:
                subcat_df = df[df[subcategory_col] == subcat]
            else:
                subcat_df = df
            
            if not subcat_df.empty:
                subcat_data[subcat] = {
                    'df': subcat_df,
                    'category': "Unknown"
                }
                if category_col and category_col in subcat_df.columns and not subcat_df[category_col].empty:
                    subcat_data[subcat]['category'] = subcat_df[category_col].mode().iloc[0]
        
        print(f"[L4] Subcategories with data: {len(subcat_data)}")
        for subcat, data in subcat_data.items():
            print(f"   - {subcat}: {len(data['df']):,} records")
        
        # Shared state for results
        l4_outputs: dict[str, str] = {}
        # Seed l4_outputs with the synthetic Uncategorized checkpoint so the
        # standard final merge picks up these rows (Req 1+3 path integrity).
        if uncategorized_synthetic_checkpoint:
            l4_outputs["_uncategorized_synthetic"] = uncategorized_synthetic_checkpoint
        results_lock = asyncio.Lock()
        stats_lock = asyncio.Lock()
        
        # Chunk size for incremental checkpointing within each subcategory
        L4_CHECKPOINT_CHUNK = 1000

        async def process_subcategory(subcat: str, data: dict) -> tuple[str, str | None]:
            """Process a single subcategory - runs in parallel with others.
            
            Uses incremental checkpointing: processes records in chunks of
            L4_CHECKPOINT_CHUNK and saves after each chunk. This allows
            resuming from the last completed chunk if the process crashes.
            """
            subcat_df = data['df']
            category = data['category']
            
            # Create output paths
            safe_name = subcat.lower().replace(" ", "_").replace("/", "_")
            checkpoint_file = str(
                settings.checkpoint_dir / f"l4_{safe_name}_checkpoint.csv"
            )
            taxonomy_file = settings.taxonomy_dir / f"l4_{safe_name}_taxonomy.json"
            
            # Check existing progress
            checkpoint_df = self.loader.load_checkpoint(checkpoint_file)
            pending_df = self.loader.get_pending_records(
                subcat_df, checkpoint_df, id_column=id_col_for_checkpoint
            )
            
            already_done = len(subcat_df) - len(pending_df)
            
            if pending_df.empty:
                print(f"[L4] {subcat}: Already complete ({len(subcat_df)} records)")
                logger.info(f"{subcat}: Already complete ({len(subcat_df)} records)")
                return subcat, checkpoint_file
            
            print(f"[L4] {subcat}: Starting — {len(pending_df)} pending (of {len(subcat_df)}, {already_done} done)")
            logger.info(f"{subcat}: {len(pending_df)} pending (of {len(subcat_df)}, {already_done} checkpointed)")
            
            # Create classifier (uses shared rate limiter)
            classifier = L4Classifier(
                batch_size=self.batch_size,
                workers=self.workers,
                debug=self.debug,
                max_rpm=550,  # Global limit, shared via singleton
            )
            
            # Load or derive taxonomy
            taxonomy = None
            
            if not getattr(self, '_generate_taxonomy', False):
                # Try local file first
                if taxonomy_file.exists():
                    taxonomy = classifier.load_taxonomy(taxonomy_file)
                else:
                    # Try S3
                    s3_taxonomy = download_taxonomy_from_s3(subcat)
                    if s3_taxonomy:
                        from ..models import L4Taxonomy
                        taxonomy = L4Taxonomy(**s3_taxonomy)
                        classifier._taxonomy = taxonomy
                        classifier.save_taxonomy(taxonomy_file)
            
            if not taxonomy:
                # Need to generate taxonomy
                taxonomy_days = getattr(self, '_taxonomy_days', 7)
                if taxonomy_days > 0:
                    from ..utils import load_incidents_from_database
                    incidents_for_taxonomy: list[dict[str, Any]] = subcat_df.to_dict(orient="records")  # type: ignore[assignment]
                else:
                    incidents_for_taxonomy: list[dict[str, Any]] = subcat_df.to_dict(orient="records")  # type: ignore[assignment]
                
                taxonomy = await classifier.derive_taxonomy(
                    incidents_for_taxonomy,
                    category=category,
                    subcategory=subcat,
                    sample_size=sample_size,
                )
                classifier.save_taxonomy(taxonomy_file)
                upload_taxonomy_to_s3(subcat, taxonomy.model_dump())
            
            # Run classification in chunks with incremental checkpointing
            all_pending: list[dict[str, Any]] = pending_df.to_dict(orient="records")  # type: ignore[assignment]
            total_pending = len(all_pending)
            chunk_success = 0
            chunk_failed = 0
            
            for chunk_start in range(0, total_pending, L4_CHECKPOINT_CHUNK):
                chunk_end = min(chunk_start + L4_CHECKPOINT_CHUNK, total_pending)
                chunk = all_pending[chunk_start:chunk_end]
                chunk_num = (chunk_start // L4_CHECKPOINT_CHUNK) + 1
                total_chunks = (total_pending + L4_CHECKPOINT_CHUNK - 1) // L4_CHECKPOINT_CHUNK
                
                print(f"[L4] {subcat}: Processing chunk {chunk_num}/{total_chunks} ({len(chunk)} records)...")
                logger.info(f"{subcat}: Processing chunk {chunk_num}/{total_chunks} ({len(chunk)} records)")
                
                results = await classifier.classify_all(chunk)
                
                # Save checkpoint immediately after each chunk
                valid_results = [r for r in results if r is not None]
                if valid_results:
                    self.loader.append_results(checkpoint_file, valid_results)
                
                chunk_success += len(valid_results)
                chunk_failed += len(results) - len(valid_results)
                
                print(f"[L4] {subcat}: Chunk {chunk_num}/{total_chunks} done — {chunk_success}/{chunk_start + len(chunk)} classified")
                logger.info(
                    f"{subcat}: Chunk {chunk_num}/{total_chunks} saved — "
                    f"{chunk_success}/{chunk_start + len(chunk)} cumulative"
                )
            
            # Update stats (thread-safe)
            actionable_count = 0
            # Re-read the full checkpoint to get accurate actionable count
            final_checkpoint = self.loader.load_checkpoint(checkpoint_file)
            if final_checkpoint is not None and 'is_actionable' in final_checkpoint.columns:
                actionable_count = int(final_checkpoint['is_actionable'].sum())
            
            async with stats_lock:
                self.stats.l4_processed += total_pending
                self.stats.l4_success += chunk_success
                self.stats.l4_failed += chunk_failed
                self.stats.l4_actionable += actionable_count
                self.stats.l4_non_actionable += chunk_success - actionable_count
                
                self.stats.subcategory_stats[subcat] = {
                    "total": len(subcat_df),
                    "success": already_done + chunk_success,
                    "actionable": actionable_count,
                }
            
            print(f"[L4] {subcat}: COMPLETE — {chunk_success}/{total_pending} classified")
            logger.info(f"{subcat}: Complete — {chunk_success}/{total_pending} classified")
            
            return subcat, checkpoint_file
        
        # Process all subcategories in parallel
        tasks = [
            process_subcategory(subcat, data)
            for subcat, data in subcat_data.items()
        ]
        
        logger.info(f"Starting {len(tasks)} parallel L4 tasks")
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Collect results
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"L4 task error: {result}")
            elif isinstance(result, tuple):
                subcat, checkpoint = result
                if checkpoint:
                    l4_outputs[subcat] = checkpoint
        
        # Log final rate limiter stats
        stats = rate_limiter.get_stats()
        logger.info(f"L4 Final RPM: {stats['current_rpm']:.0f}/{stats['max_rpm']}, Completed: {len(l4_outputs)}/{len(subcat_data)} subcategories")
        
        return l4_outputs


async def run_full_pipeline(
    input_file: str,
    output_dir: str | None = None,
    batch_size: int | None = None,
    workers: int | None = None,
    l4_subcategories: list[str] | None = None,
    skip_l123: bool = False,
    skip_l4: bool = False,
    debug: bool = False,
    generate_taxonomy: bool = False,
    taxonomy_days: int = 7,
    max_rpm: int = 550,
    disable_taxonomy_validation: bool = False,
) -> PipelineResult:
    """
    Convenience function to run the full classification pipeline.
    
    Args:
        input_file: Path to input CSV
        output_dir: Output directory (default: settings.output_dir)
        batch_size: Incidents per API call
        workers: Parallel workers
        l4_subcategories: Specific subcategories for L4
        skip_l123: Skip L123 step
        skip_l4: Skip L4 step
        debug: Enable debug output
        generate_taxonomy: Force regeneration of L4 taxonomies
        taxonomy_days: Days of data to use for taxonomy generation
        max_rpm: Maximum requests per minute limit
    
    Returns:
        PipelineResult with status and outputs
    """
    # Initialize rate limiter with max RPM
    reset_rate_limiter()
    get_rate_limiter(max_rpm)
    
    pipeline = ClassificationPipeline(
        batch_size=batch_size,
        workers=workers,
        debug=debug,
        disable_taxonomy_validation=disable_taxonomy_validation,
    )
    
    return await pipeline.run(
        input_file=input_file,
        output_dir=output_dir,
        l4_subcategories=l4_subcategories,
        skip_l123=skip_l123,
        skip_l4=skip_l4,
        generate_taxonomy=generate_taxonomy,
        taxonomy_days=taxonomy_days,
    )
