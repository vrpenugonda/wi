"""
L1/L2/L3 Incident Classifier

Classifies incidents into Category (L1), Subcategory (L2), and Product (L3)
using the hierarchical taxonomy defined in models/taxonomy.py.
"""

import asyncio
import logging
from typing import Any

from pydantic_ai import Agent

from ..config import settings
from ..models import (
    INCIDENT_TAXONOMY,
    BatchIncidentClassification,
)
from ..utils import get_azure_ad_token
from ..validation import TaxonomyValidator
from .base import BaseClassifier

logger = logging.getLogger(__name__)


def get_taxonomy_description() -> str:
    """Generate a formatted taxonomy description for the system prompt."""
    lines = ["INCIDENT CLASSIFICATION TAXONOMY:"]
    lines.append("=" * 50)
    
    for category, subcats in INCIDENT_TAXONOMY.items():
        lines.append(f"\nCATEGORY (L1): {category}")
        for subcat, products in subcats.items():
            lines.append(f"  SUBCATEGORY (L2): {subcat}")
            for product in products:
                lines.append(f"    PRODUCT (L3): {product}")
    
    return "\n".join(lines)


class L123Classifier(BaseClassifier):
    """
    Classifier for L1 (Category), L2 (Subcategory), and L3 (Product) levels.
    
    This is the first step in the classification pipeline. It assigns incidents
    to the appropriate category, subcategory, and product from the predefined
    taxonomy hierarchy.
    """
    
    def __init__(
        self,
        batch_size: int | None = None,
        workers: int | None = None,
        debug: bool = False,
        max_rpm: int = 550,
        disable_taxonomy_validation: bool = False,
    ):
        super().__init__(
            batch_size=batch_size or settings.batch_size,
            workers=workers or settings.workers,
            debug=debug,
            max_rpm=max_rpm,
        )
        self._agent: Agent[None, BatchIncidentClassification] | None = None
        self._token: str | None = None
        self._taxonomy_desc = get_taxonomy_description()

        validation_enabled = (
            getattr(settings, "enable_l123_taxonomy_validation", True)
            and not disable_taxonomy_validation
        )
        self._validator: TaxonomyValidator | None = None
        if validation_enabled:
            try:
                self._validator = TaxonomyValidator.from_files(
                    taxonomy=INCIDENT_TAXONOMY,
                    alias_map_path=getattr(
                        settings,
                        "l123_alias_map_path",
                        "insights/validation/aliases.json",
                    ),
                )
                logger.info("L123 taxonomy validator: enabled")
            except Exception as exc:
                logger.warning(
                    "L123 taxonomy validator construction failed (%s); "
                    "continuing without validation",
                    exc,
                )
                self._validator = None
        else:
            logger.info("L123 taxonomy validator: disabled")
    
    async def _ensure_agent(self) -> Agent[None, BatchIncidentClassification]:
        """Create or refresh the classification agent."""
        # Refresh token if needed
        new_token = get_azure_ad_token()
        
        if self._agent is None or new_token != self._token:
            self._token = new_token
            self._agent = await self._create_agent()
        
        return self._agent
    
    async def _create_agent(self) -> Agent[None, BatchIncidentClassification]:
        """Create the Pydantic AI agent for L1/L2/L3 classification."""
        model = self.get_model()
        model_settings = self.get_model_settings()
        
        system_prompt = f"""You are an IT incident classification assistant for Optum/UHG ServiceNow tickets.

CRITICAL: Follow the EXACT taxonomy hierarchy below. Do NOT reverse or mix up levels.

{self._taxonomy_desc}

RULES:
1. CATEGORY (L1) = Top level (e.g., Software, Hardware, Network)
2. SUBCATEGORY (L2) = Second level, must be a CHILD of the category
3. PRODUCT (L3) = Third level, must be a CHILD of the subcategory

WRONG: category="ConfigurationIssues", subcategory="Software" (REVERSED!)
RIGHT: category="Software", subcategory="ConfigurationIssues", product="Driver Issues"

CONFIDENCE SCORING:
- 0.9-1.0: Clear, unambiguous match with specific technical details
- 0.7-0.9: Good match but some ambiguity
- 0.5-0.7: Best guess, limited information
- <0.5: Very uncertain, consider "Other" categories

ROOT CAUSE ANALYSIS (Be Lenient - Always Try to Identify):
Identify what CAUSED the issue. Look for any of these patterns:

ROOT CAUSE INDICATORS (root_cause_indicator field):
- "User_Error" - User mistake, forgot password, wrong action, training issue
- "Configuration" - Settings, policies, misconfigured software/system
- "Software_Bug" - Application crash, glitch, known issue, defect
- "Infrastructure" - Server, network, hardware failure, outage
- "Third_Party" - External vendor, partner system, integration issue
- "Security" - Account locked, MFA, certificate, access denied
- "Change_Related" - Recent update, deployment, migration caused issue
- "Capacity" - Performance, timeout, resource exhaustion
- "Data_Issue" - Corrupt data, sync problem, missing records
- "Unknown" - ONLY if truly no indication in the description

ROOT CAUSE (root_cause field):
Provide a brief, specific description of what caused the issue, e.g.:
- "User entered wrong VPN credentials multiple times"
- "Citrix receiver version outdated"
- "Network timeout due to high latency"
- "MFA token not synced after phone replacement"
- "Software incompatible with Windows 11 update"

IMPORTANT: Be generous in identifying root causes. If you see ANY hint about why 
something happened, extract it. Don't leave root_cause empty unless the description 
provides zero context about causation.

Always provide a rationale explaining your classification decision.
"""
        
        agent = Agent(
            model,
            output_type=BatchIncidentClassification,
            model_settings=model_settings,
            system_prompt=system_prompt,
        )
        
        return agent
    
    @staticmethod
    def _resolve_input_id(inc: dict[str, Any], fallback_idx: int) -> str:
        """Resolve a stable id for an input incident dict.

        Mirrors the resolution order used by the prompt builder so the
        ids we capture for input/output reconciliation match the ids
        the LLM is asked to return.
        """
        return str(
            inc.get("incident_id")
            or inc.get("in_id")
            or inc.get("Incident ID")
            or inc.get("incident_number", f"UNK-{fallback_idx}")
        )

    def _synthesize_pipeline_gap_rows(
        self,
        ids: list[str],
        input_lookup: dict[str, dict[str, Any]],
        reason: str,
    ) -> list[dict[str, Any]]:
        """Build synthesized Uncategorized rows for incidents the L123
        classifier dropped or never produced output for.

        These rows carry the same sidecar shape as validator-bucketed
        rows (status=``pipeline_gap``) so the existing audit-emission
        pipelines (runner.py, walle-finalize.yaml) persist them without
        any extra plumbing.

        Skipped entirely when the validator/kill-switch is off — in
        that mode the pipeline contract is "model output flows through
        unchanged"; injecting Uncategorized rows would violate it.
        """
        if self._validator is None or not ids:
            return []
        rows: list[dict[str, Any]] = []
        try:
            import json as _json

            details_blob: str | None = _json.dumps(
                {"reason": reason}, ensure_ascii=False, default=str
            )
        except Exception:
            details_blob = None
        for in_id in ids:
            src = input_lookup.get(in_id, {})
            row: dict[str, Any] = {
                "incident_id": in_id,
                "category": "Uncategorized",
                "subcategory": "Uncategorized",
                "product": "Uncategorized",
                "confidence": 0.0,
                "rationale": f"L123 classifier produced no output ({reason})",
                "_validation_status": "pipeline_gap",
                "_original_l1": None,
                "_original_l2": None,
                "_original_l3": None,
                "_repair_applied": False,
                "_validation_details": details_blob,
            }
            # Carry through brief description if available so audit
            # downstream consumers can show a human-readable hint.
            brief = (
                src.get("brief_description")
                or src.get("Short Description")
                or src.get("description")
            )
            if brief:
                row["brief_description"] = brief
            rows.append(row)
        return rows

    async def classify_batch(
        self,
        batch: list[dict[str, Any]],
        **kwargs,
    ) -> list[dict[str, Any]]:
        """
        Classify a batch of incidents.
        
        Args:
            batch: List of incident dictionaries with at least:
                - incident_id or in_id
                - brief_description or Short Description
        
        Returns:
            List of classification result dicts. When the validator is
            enabled (default) every input incident produces exactly one
            output row: either a real classification (possibly bucketed
            by the validator) or a synthesized ``pipeline_gap`` row
            when the LLM dropped/failed it. With the kill switch on,
            failed batches still return ``[]`` to preserve the legacy
            contract.
        """
        if not batch:
            return []

        # Capture the input id set up-front so we can reconcile against
        # the LLM's output (Scenarios M and N).
        input_ids: list[str] = []
        input_lookup: dict[str, dict[str, Any]] = {}
        for idx, inc in enumerate(batch, 1):
            in_id = self._resolve_input_id(inc, idx)
            input_ids.append(in_id)
            input_lookup[in_id] = inc

        # Build prompt for batch processing
        incident_texts = []
        for idx, (inc, in_id) in enumerate(zip(batch, input_ids), 1):
            brief_desc = (
                inc.get("brief_description") or 
                inc.get("Short Description") or 
                inc.get("description", "")
            )
            # Truncate to save tokens
            brief_desc = brief_desc[:150] if brief_desc else "No description"
            incident_texts.append(f"{idx}. {in_id}: {brief_desc}")
        
        prompt = f"""Classify {len(batch)} IT incidents into Category (L1), Subcategory (L2), and Product (L3).

Return for each:
- incident_id: The ID from the incident
- category: L1 category from taxonomy
- subcategory: L2 subcategory (must be child of category)
- product: L3 product (must be child of subcategory)
- confidence: Float 0.0-1.0
- rationale: Brief explanation

INCIDENTS:
{chr(10).join(incident_texts)}"""

        if self.debug:
            print(f"[L123] Processing batch of {len(batch)} incidents")
        
        max_retries = 5  # More retries for transient errors
        auth_retries = 0  # Track auth-specific retries
        max_auth_retries = 3
        
        for attempt in range(max_retries):
            try:
                # Refresh agent if needed (token expires after 60 min)
                agent = await self._ensure_agent()
                
                result = await agent.run(prompt)
                
                # Track metrics
                if hasattr(result, "usage") and result.usage:
                    self.metrics.add_request(
                        input_tokens=getattr(result.usage, "input_tokens", 0) or 0,
                        output_tokens=getattr(result.usage, "output_tokens", 0) or 0,
                    )
                
                # Extract classifications and convert to dicts
                if hasattr(result, "output") and result.output:
                    batch_result = result.output
                    if hasattr(batch_result, "classifications") and batch_result.classifications:
                        # Only return valid classifications (with incident_id)
                        valid_classifications = [
                            c.model_dump() if hasattr(c, "model_dump") else dict(c)
                            for c in batch_result.classifications
                            if hasattr(c, 'incident_id') and c.incident_id
                        ]
                        if valid_classifications:
                            validated = self._apply_taxonomy_validation(
                                valid_classifications
                            )
                            # Scenario M: incidents the LLM omitted (or
                            # returned with empty incident_id) get a
                            # synthesized pipeline_gap row so finalize
                            # has audit coverage for them.
                            returned_ids = {
                                str(c.get("incident_id"))
                                for c in validated
                                if c.get("incident_id")
                            }
                            dropped = [i for i in input_ids if i not in returned_ids]
                            if dropped:
                                if self.debug:
                                    print(
                                        f"[L123] LLM dropped {len(dropped)}/{len(input_ids)} "
                                        f"incident(s); synthesizing pipeline_gap rows"
                                    )
                                validated.extend(
                                    self._synthesize_pipeline_gap_rows(
                                        dropped,
                                        input_lookup,
                                        reason="missing_in_response",
                                    )
                                )
                            return validated
                
                # Empty response - treat as error and retry
                if self.debug:
                    print(f"[L123] Empty response (attempt {attempt + 1}), retrying...")
                await asyncio.sleep(2)
                continue
                
            except Exception as e:
                error_str = str(e).lower()
                
                # Handle token expiry (Stargate token lasts 60 min)
                if "401" in error_str or "unauthorized" in error_str:
                    auth_retries += 1
                    if auth_retries > max_auth_retries:
                        print(f"[L123] Auth failed after {max_auth_retries} token refreshes")
                        # Scenario N: full-batch synthesized rows so
                        # nothing is silently dropped on auth exhaustion.
                        return self._synthesize_pipeline_gap_rows(
                            input_ids,
                            input_lookup,
                            reason="auth_failed_max_token_refreshes",
                        )
                    if self.debug:
                        print(f"[L123] Token expired, refreshing (auth retry {auth_retries})...")
                    self._agent = None  # Force agent recreation with new token
                    self.refresh_model()  # Also refresh the model/token
                    await asyncio.sleep(2)
                    continue
                
                # Handle rate limiting
                if "429" in error_str or "rate" in error_str:
                    wait_time = 30 * (attempt + 1)
                    if self.debug:
                        print(f"[L123] Rate limited, waiting {wait_time}s...")
                    await asyncio.sleep(wait_time)
                    continue
                
                # Other errors - retry with backoff
                if attempt < max_retries - 1:
                    if self.debug:
                        print(f"[L123] Error (attempt {attempt + 1}): {e}")
                    await asyncio.sleep(5 * (attempt + 1))
                else:
                    print(f"[L123] Failed after {max_retries} attempts: {e}")
                    # Scenario N: full-batch synthesized rows on
                    # retries-exhausted exit.
                    return self._synthesize_pipeline_gap_rows(
                        input_ids,
                        input_lookup,
                        reason="batch_failed_all_retries",
                    )

        # Scenario N: post-loop fall-through (all attempts returned
        # empty parseable responses). Emit synthesized rows so the
        # batch is observable rather than silently lost.
        return self._synthesize_pipeline_gap_rows(
            input_ids,
            input_lookup,
            reason="empty_response_all_retries",
        )
    
    def _apply_taxonomy_validation(
        self,
        classifications: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Run each classification through the taxonomy validator (Req 1+3).

        Replaces `category`, `subcategory`, `product` with either canonical
        taxonomy labels (possibly after deterministic repair) or the
        controlled bucket "Uncategorized" for all three levels.

        Attaches sidecar fields (`_validation_status`, `_original_l1/l2/l3`,
        `_repair_applied`, `_validation_details`) used by the pipeline
        runner to emit `WALLE_L123_TAXONOMY_AUDIT` rows. These sidecar
        fields are stripped before the final merge into
        `WALLE_CLASSIFIED_INCIDENTS`.

        If the validator is disabled (kill switch or construction failed),
        the input list is returned unchanged.
        """
        if self._validator is None:
            return classifications

        enriched: list[dict[str, Any]] = []
        for c in classifications:
            try:
                vr = self._validator.validate(
                    c.get("category"),
                    c.get("subcategory"),
                    c.get("product"),
                )
            except Exception as exc:
                logger.warning(
                    "L123 validator unexpected failure on incident %s: %s",
                    c.get("incident_id"),
                    exc,
                )
                enriched.append(c)
                continue

            c["category"] = vr.final_l1
            c["subcategory"] = vr.final_l2
            c["product"] = vr.final_l3
            c["_validation_status"] = vr.status
            c["_original_l1"] = vr.original_l1
            c["_original_l2"] = vr.original_l2
            c["_original_l3"] = vr.original_l3
            c["_repair_applied"] = vr.repair_applied
            try:
                import json as _json

                c["_validation_details"] = (
                    _json.dumps(vr.details, ensure_ascii=False, default=str)
                    if vr.details
                    else None
                )
            except Exception:
                c["_validation_details"] = None
            enriched.append(c)

        return enriched

    async def classify_single(
        self,
        incident: dict[str, Any],
    ) -> dict[str, Any]:
        """Classify a single incident."""
        results = await self.classify_batch([incident])
        return results[0] if results else {}


async def run_l123_classification(
    input_file: str,
    output_file: str | None = None,
    checkpoint_file: str | None = None,
    batch_size: int = 10,
    workers: int = 3,
    debug: bool = False,
    disable_taxonomy_validation: bool = False,
) -> str:
    """
    Run L1/L2/L3 classification on a CSV file.
    
    Args:
        input_file: Path to input CSV with incidents
        output_file: Path for output (default: auto-generated)
        checkpoint_file: Path for checkpoint (default: auto-generated)
        batch_size: Number of incidents per API call
        workers: Number of parallel workers
        debug: Enable debug output
        disable_taxonomy_validation: Req 1+3 kill switch. When True, the
            L123Classifier skips taxonomy validation/repair entirely and
            no `_validation_*` sidecar columns are written. Default False
            preserves the new Req 1+3 enforcement behavior.

    Returns:
        Path to the output file
    """
    from pathlib import Path
    from ..utils import DataLoader
    
    input_path = Path(input_file)
    
    if output_file is None:
        output_file = str(
            settings.output_dir / f"{input_path.stem}_l123_classified.csv"
        )
    
    if checkpoint_file is None:
        checkpoint_file = str(
            settings.checkpoint_dir / f"{input_path.stem}_l123_checkpoint.csv"
        )
    
    # Load data
    loader = DataLoader()
    df = loader.load_csv(input_file)
    
    if df is None or df.empty:
        raise ValueError(f"No data found in {input_file}")
    
    # Check for existing progress
    checkpoint_df = loader.load_checkpoint(checkpoint_file)
    pending_df = loader.get_pending_records(
        df, 
        checkpoint_df, 
        id_column="in_id"
    )
    
    print(f"[L123] Total records: {len(df)}")
    print(f"[L123] Already processed: {len(df) - len(pending_df)}")
    print(f"[L123] Pending: {len(pending_df)}")
    
    if pending_df.empty:
        print("[L123] All records already processed!")
        return output_file
    
    # Convert to list of dicts - cast keys to str
    incidents = [
        {str(k): v for k, v in record.items()}
        for record in pending_df.to_dict(orient="records")
    ]
    
    # Create classifier and run
    classifier = L123Classifier(
        batch_size=batch_size,
        workers=workers,
        debug=debug,
        disable_taxonomy_validation=disable_taxonomy_validation,
    )
    
    results = await classifier.classify_all(incidents)
    
    # Save results - results are already dicts
    valid_results = [r for r in results if r]
    if valid_results:
        loader.append_results(checkpoint_file, valid_results)

    # Best-effort: emit Req 1+3 audit rows for non-`valid` outcomes so the
    # standalone l123 CLI command stays consistent with the full pipeline.
    try:
        if getattr(settings, "l123_audit_persist", True):
            from ..utils.l123_audit_logging import L123AuditRow, record_l123_audit
            from ..utils import get_run_id

            run_id = get_run_id()
            audit_rows: list[L123AuditRow] = []
            for r in valid_results:
                status = r.get("_validation_status")
                if not status or status == "valid":
                    continue
                in_id = r.get("incident_id") or r.get("in_id")
                if not in_id:
                    continue
                audit_rows.append(
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
                    )
                )
            if audit_rows:
                record_l123_audit(
                    audit_rows,
                    persist_to_snowflake=True,
                    table_name=getattr(
                        settings, "l123_audit_table", "WALLE_L123_TAXONOMY_AUDIT"
                    ),
                )
    except Exception as exc:
        # Audit is best-effort; never fail the L123 run because of it.
        print(f"[L123] audit emission failed (non-fatal): {exc}")

    # Print summary
    summary = classifier.metrics.get_summary_dict()
    print(f"[L123] Metrics: {summary}")
    print(f"[L123] Successfully classified: {len(valid_results)}/{len(incidents)}")
    print(f"[L123] Output saved to: {checkpoint_file}")
    
    return checkpoint_file
