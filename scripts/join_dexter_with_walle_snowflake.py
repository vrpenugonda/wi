"""
Join DEXter model output with WALLE Snowflake table by ticket ID.

DEXter CSV columns (input):
  - INGEST_TICKET_ID
  - CLASSIFICATION_DOMAIN
  - CLASSIFICATION_CATEGORY
  - CLASSIFICATION_SUBCATEGORY
  - KEY_ISSUE_CATEGORY

WALLE Snowflake table (lookup by IN_ID):
  - Fetches ALL columns for matched IN_IDs

Output:
  - DEX columns are prefixed with DEX_
  - WALLE columns are prefixed with WALLE_
  - INNER JOIN only (rows with no Snowflake match are excluded)

Designed for large DEXter CSVs:
  - Streams input via pandas chunks
  - Queries Snowflake in IN-list batches
  - Appends merged rows to output CSV incrementally

Run (example):
  uv run python scripts/join_dexter_with_walle_snowflake.py --dex-csv dex.csv --out merged.csv
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable

import pandas as pd

from insights.config import settings
from insights.utils.snowflake import get_snowflake_connection


DEX_REQUIRED_COLS = [
    "INGEST_TICKET_ID",
    "CLASSIFICATION_DOMAIN",
    "CLASSIFICATION_CATEGORY",
    "CLASSIFICATION_SUBCATEGORY",
    "KEY_ISSUE_CATEGORY",
]


def _batched(items: list[str], batch_size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), batch_size):
        yield items[i : i + batch_size]


def main() -> int:
    parser = argparse.ArgumentParser(description="Join DEXter CSV with WALLE Snowflake records by ticket ID.")
    parser.add_argument("--dex-csv", required=True, help="Path to DEXter output CSV")
    parser.add_argument("--out", required=True, help="Path to write merged CSV")
    parser.add_argument(
        "--table",
        default=getattr(settings, "snowflake_table", "WALLE_CLASSIFIED_INCIDENTS"),
        help="Snowflake table name (default from SNOWFLAKE_TABLE / settings)",
    )
    parser.add_argument("--chunk-rows", type=int, default=200_000, help="Rows per pandas chunk (default: 200000)")
    parser.add_argument("--sf-batch", type=int, default=1000, help="IDs per Snowflake IN(...) batch (default: 1000)")
    parser.add_argument(
        "--dedupe",
        action="store_true",
        default=True,
        help="Dedupe INGEST_TICKET_ID (default: on). Use --no-dedupe to keep duplicates.",
    )
    parser.add_argument(
        "--no-dedupe",
        dest="dedupe",
        action="store_false",
        help="Disable dedupe and keep duplicate INGEST_TICKET_ID rows.",
    )
    args = parser.parse_args()

    dex_path = Path(args.dex_csv)
    out_path = Path(args.out)
    table_name: str = args.table
    chunk_rows: int = int(args.chunk_rows)
    sf_batch: int = int(args.sf_batch)

    if not dex_path.exists():
        raise FileNotFoundError(f"DEX CSV not found: {dex_path}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    seen_ids: set[str] = set()

    conn = get_snowflake_connection()
    cur = conn.cursor()
    try:
        # Discover all columns once so we can SELECT explicitly.
        cur.execute(f"DESCRIBE TABLE {table_name}")
        table_cols = [row[0] for row in cur.fetchall()]
        if not table_cols:
            raise RuntimeError(f"No columns discovered for Snowflake table: {table_name}")

        select_cols_sql = ", ".join([f'"{c}"' for c in table_cols])

        first_write = True
        total_out = 0
        total_chunks = 0
        total_dex_rows_seen = 0
        total_dex_ids_used = 0

        for dex_chunk in pd.read_csv(dex_path, chunksize=chunk_rows):
            total_chunks += 1
            total_dex_rows_seen += len(dex_chunk)

            missing = [c for c in DEX_REQUIRED_COLS if c not in dex_chunk.columns]
            if missing:
                raise ValueError(
                    f"DEX CSV missing required columns: {missing}. Found columns: {list(dex_chunk.columns)}"
                )

            dex_chunk = dex_chunk.dropna(subset=["INGEST_TICKET_ID"]).copy()
            dex_chunk["INGEST_TICKET_ID"] = dex_chunk["INGEST_TICKET_ID"].astype(str).str.strip()
            dex_chunk = dex_chunk[dex_chunk["INGEST_TICKET_ID"] != ""]

            # Dedupe within chunk and across chunks (optional)
            if args.dedupe:
                dex_chunk = dex_chunk.drop_duplicates(subset=["INGEST_TICKET_ID"], keep="first")
                before = len(dex_chunk)
                dex_chunk = dex_chunk[~dex_chunk["INGEST_TICKET_ID"].isin(seen_ids)]
                for _id in dex_chunk["INGEST_TICKET_ID"].tolist():
                    seen_ids.add(_id)
                after = len(dex_chunk)
                print(f"[DEX] chunk {total_chunks}: dedupe {before:,} -> {after:,}")

            if dex_chunk.empty:
                print(f"[DEX] chunk {total_chunks}: empty after filtering/dedupe, skipping")
                continue

            # Prefix DEX columns (only required ones; keep other cols if present but still DEX_-prefix them)
            dex_rename = {c: f"DEX_{c}" for c in dex_chunk.columns}
            dex_chunk = dex_chunk.rename(columns=dex_rename)

            ids = dex_chunk["DEX_INGEST_TICKET_ID"].tolist()
            total_dex_ids_used += len(ids)

            # Fetch Snowflake rows for these ids
            walle_rows = []
            for batch in _batched(ids, sf_batch):
                in_list = ", ".join([f"'{x}'" for x in batch])
                sql = f"""
                SELECT {select_cols_sql}
                FROM {table_name}
                WHERE IN_ID IN ({in_list})
                """
                cur.execute(sql)
                walle_rows.extend(cur.fetchall())

            if not walle_rows:
                print(f"[WALLE] chunk {total_chunks}: 0 Snowflake matches")
                continue

            walle_df = pd.DataFrame(walle_rows, columns=table_cols)
            if "IN_ID" not in walle_df.columns:
                raise RuntimeError(f"Expected IN_ID column in Snowflake table {table_name}, got: {list(walle_df.columns)}")

            walle_df["IN_ID"] = walle_df["IN_ID"].astype(str).str.strip()
            walle_df = walle_df.drop_duplicates(subset=["IN_ID"], keep="last")
            walle_df = walle_df.rename(columns={c: f"WALLE_{c}" for c in walle_df.columns})

            merged = dex_chunk.merge(
                walle_df,
                left_on="DEX_INGEST_TICKET_ID",
                right_on="WALLE_IN_ID",
                how="inner",
            )

            if merged.empty:
                print(f"[JOIN] chunk {total_chunks}: 0 inner-join matches")
                continue

            merged.to_csv(out_path, mode="w" if first_write else "a", header=first_write, index=False)
            first_write = False
            total_out += len(merged)

            print(
                f"[JOIN] chunk {total_chunks}: wrote {len(merged):,} rows "
                f"(total written: {total_out:,})"
            )

        print(
            f"Done.\n"
            f"  DEX rows read: {total_dex_rows_seen:,}\n"
            f"  DEX IDs used (post-filter/dedupe): {total_dex_ids_used:,}\n"
            f"  Output rows written: {total_out:,}\n"
            f"  Output file: {out_path}"
        )
        return 0
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())

