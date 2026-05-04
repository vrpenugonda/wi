"""
Centralized logging + optional persistence of L123 taxonomy audit rows.

Mirrors the design of `insights/utils/l4_null_logging.py`:
- One row per (in_id, walle_run_id) keyed by primary key.
- A single entrypoint for both Python logs and Snowflake persistence.
- Best-effort persistence: failures are logged but do not abort the run.

Audit rows are emitted by the runner immediately after the L123
classification stage completes, for any incident whose validation status
is not the default `valid` (per Req 1+3).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from insights.config import settings
from insights.validation import L123AuditStatus

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class L123AuditRow:
    """A single audit row for one incident."""

    in_id: str
    walle_run_id: str | None
    status: L123AuditStatus
    original_l1: str | None = None
    original_l2: str | None = None
    original_l3: str | None = None
    final_l1: str | None = None
    final_l2: str | None = None
    final_l3: str | None = None
    repair_applied: bool = False
    details: dict[str, Any] | None = None
    recorded_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        ts = self.recorded_at or datetime.now(timezone.utc)
        details_json: str | None = None
        if self.details:
            try:
                details_json = json.dumps(self.details, ensure_ascii=False, default=str)
            except Exception:
                details_json = None
        return {
            "in_id": str(self.in_id),
            "walle_run_id": self.walle_run_id,
            "status": self.status,
            "original_l1": self.original_l1,
            "original_l2": self.original_l2,
            "original_l3": self.original_l3,
            "final_l1": self.final_l1,
            "final_l2": self.final_l2,
            "final_l3": self.final_l3,
            "repair_applied": bool(self.repair_applied),
            "details": details_json,
            "recorded_at": ts.isoformat(),
        }


def _dedupe_rows(rows: Iterable[L123AuditRow]) -> list[L123AuditRow]:
    """Ensure at most one row per (in_id, walle_run_id). Last write wins."""
    by_key: dict[tuple[str, str | None], L123AuditRow] = {}
    for r in rows:
        if not r.in_id:
            continue
        by_key[(str(r.in_id), r.walle_run_id)] = r
    return list(by_key.values())


def record_l123_audit(
    rows: Iterable[L123AuditRow],
    *,
    persist_to_snowflake: bool = True,
    table_name: str = "WALLE_L123_TAXONOMY_AUDIT",
    log_each_incident: bool = False,
) -> dict[str, Any]:
    """Record L123 taxonomy audit rows.

    Args:
        rows: Iterable of `L123AuditRow` (one per incident-run pair).
        persist_to_snowflake: When True, write rows to Snowflake.
        table_name: Target Snowflake table name.
        log_each_incident: When True, emit one log line per row (noisy).

    Returns:
        Summary dict for caller logging.
    """
    final_rows = _dedupe_rows(rows)
    total = len(final_rows)

    summary: dict[str, int] = {}
    for r in final_rows:
        summary[r.status] = summary.get(r.status, 0) + 1

    print(
        f"[L123-AUDIT] total={total} persist={persist_to_snowflake} "
        f"table={table_name} breakdown={dict(sorted(summary.items(), key=lambda kv: (-kv[1], kv[0])))}",
        flush=True,
    )

    logger.info(
        "L123 taxonomy audit: total=%d table=%s persist=%s breakdown=%s",
        total,
        table_name,
        persist_to_snowflake,
        dict(sorted(summary.items(), key=lambda kv: (-kv[1], kv[0]))),
    )

    if log_each_incident and final_rows:
        for r in final_rows:
            logger.info(
                "L123 audit incident=%s status=%s original=(%s,%s,%s) final=(%s,%s,%s) repair=%s run_id=%s",
                r.in_id,
                r.status,
                r.original_l1,
                r.original_l2,
                r.original_l3,
                r.final_l1,
                r.final_l2,
                r.final_l3,
                r.repair_applied,
                r.walle_run_id,
            )

    persisted = 0
    persist_error: str | None = None

    if persist_to_snowflake and final_rows:
        try:
            from .snowflake import (
                ensure_l123_taxonomy_audit_table_exists,
                get_snowflake_connection,
                upsert_l123_taxonomy_audit,
            )

            conn = None
            try:
                conn = get_snowflake_connection()
                ensure_l123_taxonomy_audit_table_exists(conn, table_name=table_name)
                payload = [r.as_dict() for r in final_rows]
                persisted = upsert_l123_taxonomy_audit(
                    conn, payload, table_name=table_name
                )
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
        except Exception as exc:
            persist_error = str(exc)
            print(f"[L123-AUDIT] Snowflake persistence FAILED: {persist_error}", flush=True)
            logger.warning("Failed to persist L123 audit rows to Snowflake: %s", exc)

    return {
        "total": total,
        "persisted": persisted,
        "persist_error": persist_error,
        "breakdown": summary,
        "snowflake_table": table_name,
        "snowflake_target": (
            f"{settings.snowflake_database}.{settings.snowflake_schema}.{table_name}"
            if settings.snowflake_database and settings.snowflake_schema
            else table_name
        ),
    }
