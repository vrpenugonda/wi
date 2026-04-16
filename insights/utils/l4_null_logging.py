"""
Centralized logging + optional persistence for incidents with missing AI_L4.

Design goal:
- One (final) reason row per incident whose AI_L4 ends up missing.
- A single entrypoint for both Python logs and Snowflake persistence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Literal

from insights.config import settings

logger = logging.getLogger(__name__)


L4NullReason = Literal[
    # Final-state reason codes (stable for analytics)
    "l4_invalid_category_cleaned",
    "l4_missing_after_l4_run",
    "l4_skipped",
    "l4_failed_batch",
    "l4_result_dropped_unmappable",
]


@dataclass(frozen=True)
class L4NullRow:
    """A single (final) reason row for one incident."""

    in_id: str
    reason: L4NullReason
    cause: str | None = None
    subcategory: str | None = None
    walle_run_id: str | None = None
    recorded_at: datetime | None = None
    original_value: str | None = None

    def as_dict(self) -> dict[str, Any]:
        ts = self.recorded_at
        if ts is None:
            ts = datetime.now(timezone.utc)
        # Store as ISO string; Snowflake loader can parse timestamps robustly.
        return {
            "in_id": self.in_id,
            "reason": self.reason,
            "cause": self.cause,
            "subcategory": self.subcategory,
            "walle_run_id": self.walle_run_id,
            "recorded_at": ts.isoformat(),
            "original_value": self.original_value,
        }


def _dedupe_final_rows(rows: Iterable[L4NullRow]) -> list[L4NullRow]:
    """
    Ensure "final reason only" semantics: at most one row per incident.
    If duplicates are provided, last one wins.
    """
    by_id: dict[str, L4NullRow] = {}
    for r in rows:
        if not r.in_id:
            continue
        by_id[str(r.in_id)] = r
    return list(by_id.values())


def record_l4_nulls(
    rows: Iterable[L4NullRow],
    *,
    persist_to_snowflake: bool = True,
    table_name: str = "WALLE_L4_NULL_REASONS",
    log_each_incident: bool = False,
) -> dict[str, Any]:
    """
    Record (final) L4 null reasons.

    Args:
        rows: Iterable of L4NullRow (one per incident preferred).
        persist_to_snowflake: If True, write rows to Snowflake.
        table_name: Target Snowflake table name for persistence.
        log_each_incident: If True, emit one log line per incident (noisy).

    Returns:
        Summary dict for caller logging/tests.
    """
    final_rows = _dedupe_final_rows(rows)
    total = len(final_rows)

    summary: dict[str, int] = {}
    for r in final_rows:
        summary[r.reason] = summary.get(r.reason, 0) + 1

    # Always emit a stdout line so GitHub Actions captures it even if logging isn't configured.
    print(
        f"[L4-NULL] final reasons: total={total} persist={persist_to_snowflake} "
        f"table={table_name} breakdown={dict(sorted(summary.items(), key=lambda kv: (-kv[1], kv[0])))}",
        flush=True,
    )

    logger.info(
        "AI_L4 NULL reasons (final): total=%d table=%s persist=%s breakdown=%s",
        total,
        table_name,
        persist_to_snowflake,
        dict(sorted(summary.items(), key=lambda kv: (-kv[1], kv[0]))),
    )

    if log_each_incident and final_rows:
        for r in final_rows:
            logger.info(
                "AI_L4 NULL incident=%s reason=%s subcategory=%s cause=%s original=%s run_id=%s",
                r.in_id,
                r.reason,
                r.subcategory,
                r.cause,
                r.original_value,
                r.walle_run_id,
            )

    persisted = 0
    persist_error: str | None = None

    if persist_to_snowflake and final_rows:
        try:
            from .snowflake import (
                ensure_l4_null_reasons_table_exists,
                upsert_l4_null_reasons_final,
            )

            conn = None
            try:
                from .snowflake import get_snowflake_connection

                conn = get_snowflake_connection()
                ensure_l4_null_reasons_table_exists(conn, table_name=table_name)
                payload = [r.as_dict() for r in final_rows]
                persisted = upsert_l4_null_reasons_final(conn, payload, table_name=table_name)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
        except Exception as e:
            # Don't fail the pipeline on logging persistence; caller can choose to enforce.
            persist_error = str(e)
            print(f"[L4-NULL] Snowflake persistence FAILED: {persist_error}", flush=True)
            logger.warning("Failed to persist AI_L4 NULL reasons to Snowflake: %s", e)

    return {
        "total": total,
        "persisted": persisted,
        "persist_error": persist_error,
        "breakdown": summary,
        "snowflake_table": table_name,
        "snowflake_target": f"{settings.snowflake_database_test}.{settings.snowflake_schema_test}.{table_name}",
    }
