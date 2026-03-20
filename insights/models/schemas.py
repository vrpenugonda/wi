"""
Pydantic schemas for incident classification
"""

from enum import Enum
from typing import Optional, List
from pydantic import BaseModel, Field
from datetime import datetime


# =============================================================================
# L1/L2/L3 Classification Models
# =============================================================================

class IncidentClassification(BaseModel):
    """Single incident L1/L2/L3 classification result"""
    incident_id: str = Field(description="Unique incident identifier")
    category: str = Field(description="L1 Category (e.g., Network, Software)")
    subcategory: str = Field(description="L2 Subcategory (e.g., VPN_RemoteAccess)")
    product: str = Field(description="L3 Product/Issue (e.g., Client Errors)")
    vendor: Optional[str] = Field(default=None, description="Vendor if applicable")
    confidence_score: float = Field(ge=0.0, le=1.0, description="Classification confidence 0-1")
    self_resolved: bool = Field(default=False, description="Was this self-resolved by user?")
    rationale: str = Field(default="", description="Brief explanation of classification")
    keywords_identified: List[str] = Field(default_factory=list, description="Key terms identified")
    root_cause_indicator: Optional[str] = Field(default=None, description="Root cause indicator")
    root_cause: Optional[str] = Field(default=None, description="Root cause category")


class BatchIncidentClassification(BaseModel):
    """Batch of L1/L2/L3 classification results"""
    classifications: List[IncidentClassification]


# =============================================================================
# L4 Classification Models
# =============================================================================

class L4TaxonomyCategory(BaseModel):
    """A single L4 category in the taxonomy"""
    name: str = Field(description="Category name in snake_case (e.g., 'Password_Reset')")
    description: str = Field(description="Brief description of what this category covers")
    examples: List[str] = Field(description="3-5 example keywords or phrases")
    estimated_frequency: str = Field(default="medium", description="Estimated frequency: high, medium, low, rare")
    is_actionable: bool = Field(default=True, description="Whether UHG leadership can take action to reduce this issue type")
    actionability_reason: str = Field(default="", description="Brief explanation of why this is/isn't actionable")


class L4Taxonomy(BaseModel):
    """AI-derived L4 taxonomy for a category/subcategory"""
    category: str = Field(description="The L1 category")
    subcategory: Optional[str] = Field(default=None, description="The L2 subcategory if specified")
    categories: List[L4TaxonomyCategory] = Field(description="List of L4 categories")
    rationale: str = Field(description="Brief explanation of how the taxonomy was derived")
    sample_size_analyzed: int = Field(description="Number of sample incidents analyzed")
    estimated_coverage: float = Field(description="Estimated percentage of incidents covered (0-100)")
    created_at: datetime = Field(default_factory=datetime.now, description="When taxonomy was created")


class L4Classification(BaseModel):
    """Classification result for a single incident at L4 level"""
    incident_id: str = Field(description="The incident number/ID")
    l4_category: str = Field(description="The L4 resolution category")
    l4_subcategory: Optional[str] = Field(default=None, description="Optional L4 subcategory")
    resolution_action: str = Field(description="Brief description of resolution")
    l4_confidence: float = Field(ge=0.0, le=1.0, description="Confidence score 0-1")
    keywords: List[str] = Field(default_factory=list, description="Key terms identified")
    is_actionable: bool = Field(default=True, description="Whether UHG leadership can take action")
    actionability_reason: str = Field(default="", description="Why this is/isn't actionable")
    l4_rationale: str = Field(description="REQUIRED: Explanation of why this L4 category was chosen, including key evidence from the ticket")


class BatchL4Classification(BaseModel):
    """Batch of L4 classification results"""
    classifications: List[L4Classification]


# =============================================================================
# Pipeline Models
# =============================================================================

class ClassificationStatus(str, Enum):
    """Status of a classification job"""
    PENDING = "pending"
    RUNNING = "running"
    L123_COMPLETE = "l123_complete"
    L4_COMPLETE = "l4_complete"
    COMPLETED = "completed"
    FAILED = "failed"


class PipelineResult(BaseModel):
    """Result from a pipeline run"""
    status: ClassificationStatus = Field(default=ClassificationStatus.PENDING)
    l123_output: Optional[str] = Field(default=None, description="L123 output file path")
    l4_outputs: dict = Field(default_factory=dict, description="L4 output files by subcategory")
    stats: Optional[str] = Field(default=None, description="Summary statistics")
    errors: List[str] = Field(default_factory=list, description="Error messages if any")
