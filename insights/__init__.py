"""
WALLE Insights - Automated Incident Classification System

Two-step classification pipeline:
1. L1/L2/L3 Classification: Category -> Subcategory -> Product
2. L4 Classification: Detailed resolution buckets with actionability

Production-ready system designed to run every 15 minutes.

Usage:
    # As a module
    python -m walle run --input incidents.csv
    
    # As a library
    from insights import ClassificationPipeline
    pipeline = ClassificationPipeline()
    result = await pipeline.run("incidents.csv")
"""

__version__ = "1.0.0"
__author__ = "WALLE Insights Team"

# Key exports for library usage
from .config import settings
from .classifiers import L123Classifier, L4Classifier
from .pipeline import ClassificationPipeline, run_full_pipeline
from .models import (
    IncidentClassification,
    L4Classification,
    L4Taxonomy,
    PipelineResult,
    INCIDENT_TAXONOMY,
)

__all__ = [
    # Configuration
    "settings",
    # Classifiers
    "L123Classifier",
    "L4Classifier",
    # Pipeline
    "ClassificationPipeline",
    "run_full_pipeline",
    # Models
    "IncidentClassification",
    "L4Classification",
    "L4Taxonomy",
    "PipelineResult",
    "INCIDENT_TAXONOMY",
]
