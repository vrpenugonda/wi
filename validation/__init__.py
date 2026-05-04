"""
Taxonomy validation and audit primitives.

This subpackage hosts deterministic validation logic that runs *after*
the L123 classifier returns and *before* anything is written to the
checkpoint or persisted to Snowflake. It is intentionally I/O-free and
pure-Python so it is easy to reason about and reuse.
"""

from .audit_reasons import (
    L123_AUDIT_STATUSES,
    L123AuditStatus,
    L4_NULL_REASONS,
    L4NullReasonCode,
    UNCATEGORIZED_LABEL,
    UNCLASSIFIED_LABEL,
)
from .taxonomy_validator import TaxonomyValidator, ValidationResult

__all__ = [
    "L123_AUDIT_STATUSES",
    "L123AuditStatus",
    "L4_NULL_REASONS",
    "L4NullReasonCode",
    "UNCATEGORIZED_LABEL",
    "UNCLASSIFIED_LABEL",
    "TaxonomyValidator",
    "ValidationResult",
]
