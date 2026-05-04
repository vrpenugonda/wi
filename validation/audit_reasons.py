"""
Single source of truth for taxonomy audit status codes and L4 null reason
codes.

Other modules (Req 1+3 validator, Req 2 coverage gates, Req 4 remediation)
must import from here so the codes stay consistent across the pipeline,
the audit tables, and the analytics dashboards.
"""

from __future__ import annotations

from typing import Final, Literal


UNCATEGORIZED_LABEL: Final[str] = "Uncategorized"
UNCLASSIFIED_LABEL: Final[str] = "Unclassified"


L123AuditStatus = Literal[
    "valid",
    "valid_after_repair",
    "missing",
    "invalid_label",
    "invalid_hierarchy",
    "swapped_levels_suspected",
    "pipeline_gap",
]


L123_AUDIT_STATUSES: Final[tuple[str, ...]] = (
    "valid",
    "valid_after_repair",
    "missing",
    "invalid_label",
    "invalid_hierarchy",
    "swapped_levels_suspected",
    "pipeline_gap",
)


L4NullReasonCode = Literal[
    "l4_invalid_category_cleaned",
    "l4_missing_after_l4_run",
    "l4_skipped",
    "l4_failed_batch",
    "l4_result_dropped_unmappable",
    "l123_invalid_blocks_l4",
]


L4_NULL_REASONS: Final[tuple[str, ...]] = (
    "l4_invalid_category_cleaned",
    "l4_missing_after_l4_run",
    "l4_skipped",
    "l4_failed_batch",
    "l4_result_dropped_unmappable",
    "l123_invalid_blocks_l4",
)
