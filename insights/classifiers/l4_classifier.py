"""
L4 Classification Module

Classifies incidents into resolution categories (L4) based on dynamically
derived taxonomies per subcategory. Includes actionability assessment.
"""

import asyncio
import logging
import math
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from pydantic_ai import Agent

from ..config import settings
from ..models import (
    BatchL4Classification,
    L4Taxonomy,
    L4TaxonomyCategory,
)
from ..utils import get_azure_ad_token, DataLoader
from .base import BaseClassifier


def calculate_sample_size(
    population: int,
    confidence: float = 0.95,
    margin_error: float = 0.05,
) -> int:
    """
    Calculate statistically significant sample size using Cochran's formula.
    
    Args:
        population: Total population size
        confidence: Confidence level (0.95 = 95%)
        margin_error: Margin of error (0.05 = 5%)
    
    Returns:
        Required sample size
    """
    z_scores = {0.90: 1.645, 0.95: 1.96, 0.99: 2.576}
    z = z_scores.get(confidence, 1.96)
    
    # Assuming maximum variability (p = 0.5)
    p = 0.5
    
    # Cochran's formula for infinite population
    n0 = (z**2 * p * (1 - p)) / (margin_error**2)
    
    # Finite population correction
    n = n0 / (1 + ((n0 - 1) / population))
    
    return int(math.ceil(n))


class L4Classifier(BaseClassifier):
    """
    Classifier for L4 resolution categories.
    
    This is the second step in the classification pipeline. It:
    1. Derives a taxonomy of resolution categories from sample data
    2. Classifies all incidents into those categories
    3. Assesses actionability for leadership prioritization
    """
    
    def __init__(
        self,
        batch_size: int | None = None,
        workers: int | None = None,
        debug: bool = False,
        max_rpm: int = 550,
    ):
        super().__init__(
            batch_size=batch_size or settings.batch_size,
            workers=workers or settings.workers,
            debug=debug,
            max_rpm=max_rpm,
        )
        self._taxonomy: L4Taxonomy | None = None
        self._agent: Agent[None, BatchL4Classification] | None = None
        self._token: str | None = None
    
    @property
    def taxonomy(self) -> L4Taxonomy | None:
        """Get the current taxonomy."""
        return self._taxonomy
    
    @taxonomy.setter
    def taxonomy(self, value: L4Taxonomy):
        """Set the taxonomy and invalidate the agent."""
        self._taxonomy = value
        self._agent = None  # Force agent recreation with new taxonomy
    
    async def _ensure_agent(self) -> Agent[None, BatchL4Classification]:
        """Create or refresh the classification agent."""
        new_token = get_azure_ad_token()
        
        if self._agent is None or new_token != self._token:
            self._token = new_token
            self._agent = await self._create_agent()
        
        return self._agent
    
    async def _create_agent(self) -> Agent[None, BatchL4Classification]:
        """Create the Pydantic AI agent for L4 classification."""
        if self._taxonomy is None:
            raise ValueError("Taxonomy must be set before creating agent")
        
        model = self.get_model()
        model_settings = self.get_model_settings()
        
        # Build taxonomy string with actionability info
        taxonomy_str = "\n".join([
            f"- {cat.name}: {cat.description} "
            f"(examples: {', '.join(cat.examples[:3])}) "
            f"[Actionable: {cat.is_actionable}]"
            for cat in self._taxonomy.categories
        ])
        
        # Add the fallback Unknown category - but discourage its use
        taxonomy_str += "\n- Unclassified_L4: LAST RESORT ONLY - Use when ticket is truly empty, a ghost call, or completely unintelligible. (examples: blank ticket, ghost call, single word with no context) [Actionable: False]"
        
        # Get valid category names for reference (include fallback)
        valid_category_names = [cat.name for cat in self._taxonomy.categories]
        valid_category_names.append("Unclassified_L4")
        
        subcategory_context = self._taxonomy.subcategory or "General"
        
        system_prompt = f"""You are an IT incident classifier for the "{subcategory_context}" subcategory.

Your goal is to CLASSIFY AS MANY TICKETS AS POSSIBLE into meaningful categories. Target 85-95% classification rate.

Match each incident to ONE of the L4 categories below. You MUST use ONLY categories from this list.

VALID L4 CATEGORIES (use EXACT names):
{taxonomy_str}

CLASSIFICATION PHILOSOPHY - BE INCLUSIVE:
- ALWAYS try to find the BEST MATCHING category, even with partial information
- Use the application name, error type, or action taken to guide classification
- If an app name is mentioned (e.g., "COSMOS", "EEMS", "Facets"), use the app-specific category or a General category
- If a resolution describes a specific action, classify based on that action
- Low confidence classifications (0.3-0.5) are BETTER than Unclassified_L4
- When in doubt, pick the category that MOST CLOSELY matches the issue domain

CRITICAL RULES:
1. l4_category MUST be one of: {', '.join(valid_category_names)}
2. DO NOT invent categories - only use the exact names listed above
3. Use ALL available info: description, action, resolution, comments, error messages
4. Look for: error codes, application names, user actions, system names, symptoms in ANY field
5. If description is vague but resolution mentions a specific fix, classify based on the resolution
6. l4_confidence: 0.7+ if confident, 0.5-0.7 for reasonable match, 0.3-0.5 for best effort
7. resolution_action: Describe the fix from the ticket (or "Resolved per ticket" if unclear)
8. keywords: Extract any technical terms from the ticket
9. is_actionable: Use the category's defined actionability
10. l4_rationale: REQUIRED - Explain your reasoning and which clues led to this category choice

WHEN TO USE "Unclassified_L4" - STRICT CRITERIA (use sparingly, <15% of tickets):
- Ticket is COMPLETELY EMPTY or just punctuation
- Ghost call / hang-up with absolutely no other information
- Single unintelligible word with no context whatsoever
- Ticket explicitly says "test" or "ignore" with nothing else

DO NOT USE Unclassified_L4 when:
- An application name is mentioned (use app-specific or General category)
- Any action was taken (classify by the action type)
- Any error message or symptom is described
- The resolution field has any meaningful content
- You can make ANY reasonable inference about the issue type

CLASSIFICATION HINTS FOR BETTER COVERAGE:
- "Closed by Caller" with app name → User Self-Resolved or app-specific category
- Vague issues with a named app → use General/Other category for that app domain
- Generic errors → match to error-handling or troubleshooting category
- Routing/assignment issues → use Ticket Routing or Triage categories
- Data requests or updates → use Data Configuration or Request Processing categories

Respond with classifications for ALL incidents in the batch.
"""
        
        agent = Agent(
            model,
            output_type=BatchL4Classification,
            model_settings=model_settings,
            system_prompt=system_prompt,
        )
        
        return agent
    
    async def derive_taxonomy(
        self,
        incidents: list[dict[str, Any]],
        category: str,
        subcategory: str | None = None,
        sample_size: int | None = None,
    ) -> L4Taxonomy:
        """
        Derive an L4 taxonomy from sample incident data.
        
        Args:
            incidents: List of incident dictionaries
            category: The L3 category being analyzed
            subcategory: Optional subcategory filter
            sample_size: Number of samples (auto-calculated if None)
        
        Returns:
            L4Taxonomy with derived categories
        """
        population = len(incidents)
        
        # Calculate statistically significant sample size
        if sample_size is None:
            sample_size = calculate_sample_size(population, confidence=0.95, margin_error=0.05)
            # Cap sample size based on population to avoid token limits
            if population > 10000:
                sample_size = min(sample_size, 200)  # Large populations: smaller sample
            else:
                sample_size = min(sample_size, 300)  # Normal populations
            sample_size = max(sample_size, 50)   # Minimum for variety
        
        actual_sample_size = min(sample_size, population)
        
        logger.info(f"Deriving L4 taxonomy from {actual_sample_size} sample incidents (population: {population:,})")
        
        # Random sample
        import random
        sample = random.sample(incidents, actual_sample_size)
        
        # Build sample text
        sample_texts = []
        for inc in sample:
            desc = str(inc.get("brief_description", ""))[:250]
            resolution = str(inc.get("resolution", ""))[:250]
            # Support both L123 checkpoint column names and final output column names
            product = str(inc.get("ai_l3") or inc.get("product") or "N/A")
            keywords = str(inc.get("ai_keywords") or inc.get("keywords_identified") or "")[:100]
            inc_category = str(inc.get("ai_l1") or inc.get("category") or "")
            inc_subcategory = str(inc.get("ai_l2") or inc.get("subcategory") or "")
            sample_texts.append(
                f"- Category: {inc_category}\n"
                f"  Subcategory: {inc_subcategory}\n"
                f"  Product: {product}\n"
                f"  Description: {desc}\n"
                f"  Resolution: {resolution}\n"
                f"  Keywords: {keywords}"
            )
        
        # Process in batches for large samples
        # Reduced batch size to prevent token limits on large subcategories
        batch_size = 50
        all_patterns: list[L4TaxonomyCategory] = []
        
        model = self.get_model()
        model_settings = self.get_model_settings()
        
        # Build context string for prompts
        context = f"Category: {category}"
        if subcategory:
            context += f", Subcategory: {subcategory}"
        
        # Calculate number of batches for progress
        num_batches = (len(sample_texts) + batch_size - 1) // batch_size
        
        for batch_num, i in enumerate(range(0, len(sample_texts), batch_size), 1):
            batch = sample_texts[i:i + batch_size]
            samples_str = "\n\n".join(batch)
            
            logger.debug(f"Analyzing batch {batch_num}/{num_batches} ({len(batch)} samples)")
            
            system_prompt = f"""You are an IT service management expert creating a COMPREHENSIVE L4 resolution taxonomy.

CONTEXT: {context}
This taxonomy is SPECIFIC to the "{subcategory}" subcategory - categories must reflect the unique issues in this domain.

Population: {population:,} incidents | Sample: {len(batch)} of {actual_sample_size}

YOUR TASK: Create L4 categories that MAXIMIZE CLASSIFICATION COVERAGE while providing actionable insights.
TARGET: 85-95% of tickets should be classifiable using this taxonomy.

CRITICAL TAXONOMY DESIGN PRINCIPLES:

1. SPECIFIC CATEGORIES (create many of these - 60% of taxonomy):
   - Describe EXACT issue patterns (technology + specific problem)
   - Examples: "VPN_Certificate_Expired", "Citrix_Receiver_Outdated_Version"

2. BROADER ACTION-BASED CATEGORIES (include these - 25% of taxonomy):
   - Based on the TYPE OF RESOLUTION, not the specific technology
   - Examples: 
     - "Application_Reinstall_Resolved_Issue" - any app reinstall fix
     - "User_Self_Resolved_Before_Support" - closed by caller
     - "Configuration_Update_Applied" - any config change fix
     - "Access_Permission_Granted" - access/permission fixes
     - "Data_Correction_Or_Update" - data fix applied
     - "Ticket_Routed_To_Correct_Team" - routing/triage
     - "Reboot_Or_Restart_Resolved" - restart fixes
     - "Cache_Clear_Or_Browser_Reset" - browser/cache fixes

3. DOMAIN-SPECIFIC CATCH-ALL CATEGORIES (include 3-5 of these - 15% of taxonomy):
   - For this specific subcategory's domain when specifics are unclear
   - Format: "{{Domain}}_General_Troubleshooting" or "{{App}}_Generic_Issue_Resolved"
   - Examples for different domains:
     - "Network_Connectivity_Issue_Resolved_Unspecified" 
     - "Application_Error_Resolved_Generic"
     - "Healthcare_System_Issue_Addressed"
     - "Ticket_Completed_Insufficient_Details"

GOOD CATEGORY NAME EXAMPLES:
- "Password_Expired_During_VPN_Session" (specific)
- "Zscaler_Certificate_Chain_Invalid" (specific)
- "User_Self_Resolved_Closed_By_Caller" (action-based)
- "Application_Cache_Clear_Resolved_Issue" (action-based)
- "System_Configuration_Updated_Per_Request" (action-based)
- "{subcategory}_Generic_Issue_Resolved" (catch-all for this domain)

CREATE CATEGORIES THAT:
1. Name captures the issue pattern or resolution action
2. Description elaborates on symptoms and common scenarios
3. Examples list 3-5 actual phrases/keywords from incidents
4. Frequency estimate (high >10%, medium 2-10%, low 0.5-2%, rare <0.5%)
5. is_actionable: Can UHG leadership invest to reduce this? (True for most, False only for generic catch-alls)
6. actionability_reason: Specific actions leadership could take

CREATE 25-60 CATEGORIES including:
- Multiple specific categories for common issues
- Action-based categories for resolution patterns
- 3-5 catch-all categories for edge cases in this domain

SAMPLE INCIDENTS FROM {subcategory}:
{samples_str}
"""
            
            agent = Agent(
                model,
                output_type=L4Taxonomy,
                model_settings=model_settings,
            )
            
            try:
                result = await agent.run(system_prompt)
                self.metrics.add_request(
                    input_tokens=getattr(result.usage, "input_tokens", 0) or 0,
                    output_tokens=getattr(result.usage, "output_tokens", 0) or 0,
                )
                all_patterns.extend(result.output.categories)
                logger.debug(f"Batch {batch_num}: found {len(result.output.categories)} categories")
            except Exception as e:
                logger.warning(f"Batch {batch_num} error: {e}")
                continue
        
        # Consolidate if multiple batches
        if len(sample_texts) > batch_size:
            logger.debug(f"Consolidating {len(all_patterns)} categories")
            
            pattern_summary = "\n".join([
                f"- {p.name}: {p.description} [actionable={p.is_actionable}]" for p in all_patterns
            ])
            
            consolidate_prompt = f"""Consolidate these L4 categories into a final taxonomy for "{subcategory}":

{pattern_summary}

GOAL: Create a taxonomy that can classify 85-95% of tickets in this subcategory.

CONSOLIDATION RULES:
1. MERGE only truly duplicate categories (same underlying issue)
2. KEEP categories that describe different specific problems SEPARATE
3. PRESERVE specificity where it exists
4. ENSURE you have BROADER CATEGORIES to catch edge cases:
   - Include action-based categories (e.g., "User_Self_Resolved", "Reboot_Resolved_Issue")
   - Include 3-5 domain-specific catch-all categories
5. Target 30-60 final categories

REQUIRED CATEGORY TYPES (ensure you have all three):
1. SPECIFIC categories (60%) - technology + exact issue
2. ACTION-BASED categories (25%) - resolution type patterns:
   - "User_Self_Resolved_Closed_By_Caller"
   - "Application_Reinstall_Resolved"
   - "Configuration_Change_Applied"
   - "Access_Permission_Granted"
   - "Ticket_Routed_To_Correct_Team"
   - "Reboot_Or_Restart_Resolved"
3. CATCH-ALL categories (15%) - for this domain's edge cases:
   - "{subcategory}_Generic_Issue_Resolved"
   - "{subcategory}_Troubleshooting_Unspecified"
   - "Ticket_Closed_Insufficient_Details"

Context: {context}
Subcategory: {subcategory}
Total population: {population:,}
"""
            
            agent = Agent(
                model,
                output_type=L4Taxonomy,
                model_settings=model_settings,
            )
            
            result = await agent.run(consolidate_prompt)
            self.metrics.add_request(
                input_tokens=getattr(result.usage, "input_tokens", 0) or 0,
                output_tokens=getattr(result.usage, "output_tokens", 0) or 0,
            )
            taxonomy = result.output
            logger.info(f"Consolidated to {len(taxonomy.categories)} categories")
        else:
            # Single batch - use directly
            taxonomy = L4Taxonomy(
                category=category,
                subcategory=subcategory,
                categories=all_patterns,
                rationale="Derived from sample analysis",
                sample_size_analyzed=actual_sample_size,
                estimated_coverage=95.0,
            )
        
        taxonomy.sample_size_analyzed = actual_sample_size
        self._taxonomy = taxonomy
        
        # Log final result
        actionable = sum(1 for c in taxonomy.categories if c.is_actionable)
        logger.info(f"Final taxonomy: {len(taxonomy.categories)} categories ({actionable} actionable)")
        
        return taxonomy
    
    def load_taxonomy(self, file_path: str | Path) -> L4Taxonomy:
        """Load taxonomy from a JSON file."""
        loader = DataLoader()
        taxonomy_dict = loader.load_taxonomy(str(file_path))
        
        if taxonomy_dict is None:
            raise ValueError(f"Failed to load taxonomy from {file_path}")
        
        self._taxonomy = L4Taxonomy(**taxonomy_dict)
        return self._taxonomy
    
    def save_taxonomy(self, file_path: str | Path | None = None) -> str:
        """Save the current taxonomy to a JSON file."""
        if self._taxonomy is None:
            raise ValueError("No taxonomy to save")
        
        if file_path is None:
            # Generate filename from category/subcategory
            name_parts = [self._taxonomy.category]
            if self._taxonomy.subcategory:
                name_parts.append(self._taxonomy.subcategory)
            filename = "_".join(name_parts).lower().replace(" ", "_") + "_taxonomy.json"
            file_path = settings.taxonomy_dir / filename
        
        loader = DataLoader()
        loader.save_taxonomy(str(file_path), self._taxonomy.model_dump())
        
        return str(file_path)
    
    async def classify_batch(
        self,
        batch: list[dict[str, Any]],
        **kwargs,
    ) -> list[dict[str, Any]]:
        """
        Classify a batch of incidents into L4 categories.
        
        Args:
            batch: List of incident dictionaries
        
        Returns:
            List of classification result dicts (or empty dicts for failures)
        """
        if not batch:
            return []
        
        if self._taxonomy is None:
            raise ValueError("Taxonomy must be set before classification")
        
        # Debug: Log available fields in first incident
        if batch and self.debug:
            first_inc = batch[0]
            available_fields = [k for k, v in first_inc.items() if v and str(v) not in ('None', 'nan', '')]
            logger.debug(f"L4 batch: {len(batch)} incidents, fields available: {available_fields[:15]}")
            # Check critical fields
            has_desc = bool(first_inc.get('brief_description') and str(first_inc.get('brief_description')) not in ('None', 'nan', ''))
            has_resolution = bool(first_inc.get('resolution') and str(first_inc.get('resolution')) not in ('None', 'nan', ''))
            if not has_desc and not has_resolution:
                logger.warning(f"L4 batch missing critical fields! Keys: {list(first_inc.keys())[:10]}")
        
        # Build incident batch string with ALL available info
        incidents_str = []
        for inc in batch:
            inc_id = (
                inc.get("incident_id") or
                inc.get("in_id") or
                inc.get("Incident ID") or
                f"UNK-{len(incidents_str)}"
            )
            
            # Gather all available fields - don't truncate too aggressively
            desc = str(inc.get("brief_description", ""))[:500]
            resolution = str(inc.get("resolution", ""))[:500]
            action = str(inc.get("action", ""))[:400]
            comments = str(inc.get("comments", ""))[:400]
            update_action = str(inc.get("update_action", ""))[:300]
            update_action_ess = str(inc.get("update_action_ess", ""))[:300]
            monitoring_notes = str(inc.get("uh_monitoring_notes", ""))[:300]
            error_msg = str(inc.get("uh_ess_errormsg", ""))[:200]
            
            # L123 classification context - support both checkpoint column names and final output names
            product = str(inc.get("ai_l3") or inc.get("product") or "")
            category = str(inc.get("ai_l1") or inc.get("category") or "")
            subcategory = str(inc.get("ai_l2") or inc.get("subcategory") or "")
            l123_rationale = str(inc.get("ai_rationale") or inc.get("rationale") or "")[:200]
            root_cause = str(inc.get("ai_root_cause") or inc.get("root_cause") or "")
            # Check if L123 flagged this as self-resolved
            self_resolved = inc.get("ai_self_resolved") or inc.get("self_resolved")
            is_self_resolved = str(self_resolved).lower() in ('true', '1', 'yes')
            
            # Build comprehensive incident info
            inc_parts = [f"ID: {inc_id}"]
            
            # Add self-resolved flag prominently if set
            if is_self_resolved:
                inc_parts.append("*** SELF-RESOLVED: Yes (user fixed their own issue, no IT support provided) ***")
            
            if category:
                inc_parts.append(f"Category: {category}")
            if subcategory:
                inc_parts.append(f"Subcategory: {subcategory}")
            if product:
                inc_parts.append(f"Product: {product}")
            if desc and desc != "None" and desc != "nan":
                inc_parts.append(f"Description: {desc}")
            if action and action != "None" and action != "nan":
                inc_parts.append(f"Action Taken: {action}")
            if resolution and resolution != "None" and resolution != "nan":
                inc_parts.append(f"Resolution: {resolution}")
            if update_action and update_action != "None" and update_action != "nan":
                inc_parts.append(f"Update Action: {update_action}")
            if update_action_ess and update_action_ess != "None" and update_action_ess != "nan":
                inc_parts.append(f"ESS Action: {update_action_ess}")
            if comments and comments != "None" and comments != "nan":
                inc_parts.append(f"Comments: {comments}")
            if monitoring_notes and monitoring_notes != "None" and monitoring_notes != "nan":
                inc_parts.append(f"Monitoring Notes: {monitoring_notes}")
            if error_msg and error_msg != "None" and error_msg != "nan":
                inc_parts.append(f"Error Message: {error_msg}")
            if root_cause and root_cause != "None" and root_cause != "nan":
                inc_parts.append(f"Root Cause: {root_cause}")
            if l123_rationale and l123_rationale != "None" and l123_rationale != "nan":
                inc_parts.append(f"L123 Analysis: {l123_rationale}")
            
            incidents_str.append("\n".join(inc_parts))
        
        prompt = "Classify these incidents:\n\n" + "\n\n---\n\n".join(incidents_str)
        
        if self.debug:
            logger.debug(f"Processing batch of {len(batch)} incidents")
        
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
                        # Get valid category names (lowercase for comparison) - include fallback
                        valid_categories = {cat.name.lower() for cat in self._taxonomy.categories} if self._taxonomy else set()
                        valid_categories.add('unclassified_l4')  # Always allow the fallback category
                        
                        # Check all classifications for validity
                        valid_classifications = []
                        has_invalid = False
                        
                        for c in batch_result.classifications:
                            if not (hasattr(c, 'incident_id') and c.incident_id):
                                continue
                            
                            classification = c.model_dump() if hasattr(c, "model_dump") else dict(c)
                            
                            # Check if the l4_category is valid
                            l4_cat = classification.get('l4_category', '').lower()
                            
                            # Allow our official "Unclassified_L4" fallback category
                            if l4_cat == 'unclassified_l4':
                                valid_classifications.append(classification)
                                continue
                            
                            # Reject any category containing these substrings (model invents many variations)
                            # Note: "unclassified_l4" is allowed above, but other variations like "unclassified" are not
                            invalid_substrings = ['insufficient', 'unknown', 'missing', 'unable_to_classify', 'pending_details', 'n/a', 'none']
                            is_invalid = any(sub in l4_cat for sub in invalid_substrings)
                            
                            # Reject "unclassified" unless it's exactly "unclassified_l4"
                            if 'unclassif' in l4_cat and l4_cat != 'unclassified_l4':
                                is_invalid = True
                            
                            # Also invalid if not in the valid taxonomy categories
                            if valid_categories and l4_cat not in valid_categories:
                                is_invalid = True
                            
                            if is_invalid:
                                has_invalid = True
                                original_cat = classification.get('l4_category', 'unknown')
                                logger.debug(f"Invalid category detected: '{original_cat}' - will retry")
                            else:
                                valid_classifications.append(classification)
                        
                        # If ANY are invalid, retry the whole batch
                        if has_invalid:
                            logger.debug(f"Found invalid categories in batch, retrying...")
                            await asyncio.sleep(2)
                            continue  # Retry the API call
                        
                        if valid_classifications:
                            return valid_classifications
                
                # Empty response - treat as error and retry
                logger.debug(f"Empty response (attempt {attempt + 1}), retrying...")
                await asyncio.sleep(2)
                continue
                
            except Exception as e:
                error_str = str(e).lower()
                
                # Handle token expiry (Stargate token lasts 60 min)
                if "401" in error_str or "unauthorized" in error_str:
                    auth_retries += 1
                    if auth_retries > max_auth_retries:
                        logger.warning(f"Auth failed after {max_auth_retries} token refreshes")
                        return []
                    logger.debug(f"Token expired, refreshing (auth retry {auth_retries})...")
                    self._agent = None  # Force agent recreation with new token
                    self.refresh_model()  # Also refresh the model/token
                    await asyncio.sleep(2)
                    continue
                
                # Handle rate limiting
                if "429" in error_str or "rate" in error_str:
                    wait_time = 30 * (attempt + 1)
                    logger.debug(f"Rate limited, waiting {wait_time}s")
                    await asyncio.sleep(wait_time)
                    continue
                
                # Other errors - retry with backoff
                if attempt < max_retries - 1:
                    logger.debug(f"Error (attempt {attempt + 1}): {e}")
                    await asyncio.sleep(5 * (attempt + 1))
                else:
                    logger.warning(f"Failed after {max_retries} attempts: {e}")
                    return []
        
        return []


async def run_l4_classification(
    input_file: str,
    category: str | None = None,
    subcategory: str | None = None,
    taxonomy_file: str | None = None,
    output_file: str | None = None,
    checkpoint_file: str | None = None,
    derive_taxonomy: bool = True,
    sample_size: int | None = None,
    batch_size: int = 5,
    workers: int = 3,
    debug: bool = False,
) -> tuple[str, L4Taxonomy]:
    """
    Run L4 classification on a CSV file.
    
    Args:
        input_file: Path to input CSV (should have L1/L2/L3 classifications)
        category: Filter to specific category
        subcategory: Filter to specific subcategory
        taxonomy_file: Load existing taxonomy (skip derivation)
        output_file: Path for output (default: auto-generated)
        checkpoint_file: Path for checkpoint (default: auto-generated)
        derive_taxonomy: Whether to derive taxonomy from data
        sample_size: Sample size for taxonomy derivation
        batch_size: Number of incidents per API call
        workers: Number of parallel workers
        debug: Enable debug output
    
    Returns:
        Tuple of (output_file_path, taxonomy)
    """
    
    input_path = Path(input_file)
    
    # Load data
    loader = DataLoader()
    df = loader.load_csv(input_file)
    
    if df is None or df.empty:
        raise ValueError(f"No data found in {input_file}")
    
    # Handle column name variations (ai_l1/category, ai_l2/subcategory)
    if 'category' in df.columns and 'ai_l1' not in df.columns:
        df['ai_l1'] = df['category']
    if 'subcategory' in df.columns and 'ai_l2' not in df.columns:
        df['ai_l2'] = df['subcategory']
    if 'product' in df.columns and 'ai_l3' not in df.columns:
        df['ai_l3'] = df['product']
        
    # Filter by category/subcategory
    if category:
        if 'ai_l1' not in df.columns:
            raise ValueError(f"No 'ai_l1' or 'category' column found. Columns: {list(df.columns)}")
        df = df[df["ai_l1"] == category]
        logger.debug(f"Filtered to category '{category}': {len(df):,}")
    
    if subcategory:
        if 'ai_l2' not in df.columns:
            raise ValueError(f"No 'ai_l2' or 'subcategory' column found. Columns: {list(df.columns)}")
        df = df[df["ai_l2"] == subcategory]
        logger.debug(f"Filtered to subcategory '{subcategory}': {len(df):,}")
    
    if df.empty:
        raise ValueError("No data after filtering")
    
    # Generate file names
    name_parts = []
    if category:
        name_parts.append(category.lower().replace(" ", "_"))
    if subcategory:
        name_parts.append(subcategory.lower().replace(" ", "_"))
    name_suffix = "_".join(name_parts) if name_parts else "all"
    
    if output_file is None:
        output_file = str(
            settings.output_dir / f"{input_path.stem}_{name_suffix}_l4_classified.csv"
        )
    
    if checkpoint_file is None:
        checkpoint_file = str(
            settings.checkpoint_dir / f"{input_path.stem}_{name_suffix}_l4_checkpoint.csv"
        )
    
    # Create classifier
    classifier = L4Classifier(
        batch_size=batch_size,
        workers=workers,
        debug=debug,
    )
    
    # Load or derive taxonomy
    if taxonomy_file:
        taxonomy = classifier.load_taxonomy(taxonomy_file)
        logger.info(f"Loaded taxonomy with {len(taxonomy.categories)} categories")
    elif derive_taxonomy:
        # Cast dict keys to str
        incidents = [
            {str(k): v for k, v in record.items()}
            for record in df.to_dict(orient="records")
        ]
        taxonomy = await classifier.derive_taxonomy(
            incidents,
            category=category or "Unknown",
            subcategory=subcategory,
            sample_size=sample_size,
        )
        # Save taxonomy for reuse
        taxonomy_path = classifier.save_taxonomy()
        logger.info(f"Saved taxonomy to: {taxonomy_path}")
    else:
        raise ValueError("Must provide taxonomy_file or set derive_taxonomy=True")
    
    # Check for existing progress
    checkpoint_df = loader.load_checkpoint(checkpoint_file)
    pending_df = loader.get_pending_records(df, checkpoint_df, id_column="in_id")
    
    logger.info(f"L4: {len(df)} total, {len(df) - len(pending_df)} done, {len(pending_df)} pending")
    
    if pending_df.empty:
        logger.info("L4: All records already processed")
        return output_file, taxonomy
    
    # Convert to list of dicts - cast keys to str
    incidents = [
        {str(k): v for k, v in record.items()}
        for record in pending_df.to_dict(orient="records")
    ]
    
    # Run classification
    results = await classifier.classify_all(incidents)
    
    # Save results - results are already dicts
    valid_results = [r for r in results if r]
    if valid_results:
        loader.append_results(checkpoint_file, valid_results)
    
    # Log summary
    actionable = sum(1 for r in valid_results if r.get("is_actionable", True))
    non_actionable = len(valid_results) - actionable
    logger.info(f"L4: Classified {len(valid_results)}/{len(incidents)} (actionable: {actionable}, non-actionable: {non_actionable})")
    
    return checkpoint_file, taxonomy
