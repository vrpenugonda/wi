"""
DEXter vs WALLE comparison metrics (no gold labels required).

Metric 1 implemented:
  - Coverage / completeness at L1-L4 for DEX vs WALLE

Input:
  - merged CSV output from scripts/join_dexter_with_walle_snowflake.py (DEX_* + WALLE_* columns)

Output:
  - Excel report with:
      - data: standardized, readable column names
      - metric_1_coverage: coverage % table

Run (example):
  uv run python scripts/dexter_vs_walle_metrics.py --in merged.csv --out report.xlsx
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl import load_workbook


def _is_usable_label(v, generic: set[str]) -> bool:
    if v is None or pd.isna(v):
        return False
    s = str(v).strip()
    if s == "":
        return False
    if s.lower() in generic:
        return False
    return True


def _normalize_label_series(s: pd.Series) -> pd.Series:
    """Normalize labels for comparison (stringify, strip, lower)."""
    return (
        s.fillna("")
        .astype(str)
        .map(lambda x: x.strip())
        .map(lambda x: x.lower())
    )


def _entropy_from_counts(counts: pd.Series) -> float:
    """Shannon entropy (base-2) from a value_counts series."""
    total = float(counts.sum())
    if total <= 0:
        return 0.0
    p = counts.astype(float) / total
    p = p[p > 0]
    import math

    return float(-sum(float(pi) * math.log2(float(pi)) for pi in p))


def _top_share(s: pd.Series, top_n: int) -> float:
    """Share (%) covered by top_n labels."""
    if s.empty:
        return 0.0
    vc = s.value_counts(dropna=False)
    total = float(vc.sum())
    if total <= 0:
        return 0.0
    return float(vc.head(top_n).sum() / total * 100.0)


def _usable_mask(s: pd.Series, generic: set[str]) -> pd.Series:
    """Boolean mask for usable labels."""
    return s.apply(lambda v: _is_usable_label(v, generic))


def _prepend_sheet_explainability(
    xlsx_path: Path,
    per_sheet_lines: dict[str, list[str]],
) -> None:
    """
    Prepend human-readable explainability blocks to each sheet.

    We write the metric DataFrames with pandas first, then reopen the workbook and
    insert rows at the top of each sheet so the guidance travels with the tab.
    """
    wb = load_workbook(xlsx_path)
    for sheet_name, lines in per_sheet_lines.items():
        if sheet_name not in wb.sheetnames:
            continue
        ws = wb[sheet_name]
        if not lines:
            continue

        # Insert guidance rows at top; keep a blank separator row.
        n_insert = len(lines) + 1
        ws.insert_rows(1, amount=n_insert)

        for i, line in enumerate(lines, start=1):
            ws.cell(row=i, column=1, value=line)

        # Make the first column readable for long lines.
        try:
            ws.column_dimensions["A"].width = max(ws.column_dimensions["A"].width or 0, 120)
        except Exception:
            pass

        # Freeze panes at first data row (after explainability + header row).
        # Data header row will now begin at row n_insert + 1.
        ws.freeze_panes = ws.cell(row=n_insert + 2, column=1)

    wb.save(xlsx_path)


def main() -> int:
    p = argparse.ArgumentParser(description="Compute DEXter vs WALLE metrics from merged CSV.")
    p.add_argument("--in", dest="input_csv", required=True, help="Path to merged DEX+WALLE CSV")
    p.add_argument("--out", dest="output_xlsx", required=True, help="Path to output Excel report")
    p.add_argument(
        "--generic",
        default="unknown,other,unclassified,unclassified_l4,n/a,na,none",
        help="Comma-separated list of generic labels to treat as non-usable (default: common unknown buckets)",
    )
    args = p.parse_args()

    input_path = Path(args.input_csv)
    output_path = Path(args.output_xlsx)
    if not input_path.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_path}")

    generic = {s.strip().lower() for s in str(args.generic).split(",") if s.strip()}

    df = pd.read_csv(input_path)

    # ---- Column contract normalization (readability) ----
    rename_map = {
        # single ID
        "DEX_INGEST_TICKET_ID": "INC_ID",
        "WALLE_IN_ID": "WALLE_IN_ID",

        # DEX hierarchy
        "DEX_CLASSIFICATION_DOMAIN": "DEX_L1",
        "DEX_CLASSIFICATION_CATEGORY": "DEX_L2",
        "DEX_CLASSIFICATION_SUBCATEGORY": "DEX_L3",
        "DEX_KEY_ISSUE_CATEGORY": "DEX_L4",

        # WALLE hierarchy
        "WALLE_AI_L1": "WALLE_L1",
        "WALLE_AI_L2": "WALLE_L2",
        "WALLE_AI_L3": "WALLE_L3",
        "WALLE_AI_L4": "WALLE_L4",

        # WALLE explainability fields (keep)
        "WALLE_VENDOR": "WALLE_VENDOR",
        "WALLE_AI_RATIONALE": "WALLE_AI_RATIONALE",
        "WALLE_AI_KEYWORDS": "WALLE_AI_KEYWORDS",
        "WALLE_AI_ROOT_CAUSE_INDICATOR": "WALLE_AI_ROOT_CAUSE_INDICATOR",
        "WALLE_AI_ROOT_CAUSE": "WALLE_AI_ROOT_CAUSE",
        "WALLE_AI_L4_CONFIDENCE": "WALLE_AI_L4_CONFIDENCE",
        "WALLE_AI_L4_RESOLUTION_ACTION": "WALLE_AI_L4_RESOLUTION_ACTION",
        "WALLE_AI_L4_ACTIONABLE": "WALLE_L4_ACTIONABLE",
        "WALLE_AI_L4_ACTIONABILITY_REASON": "WALLE_L4_ACTIONABILITY_REASON",
        "WALLE_AI_L4_RATIONALE": "WALLE_AI_L4_RATIONALE",

        # Incident context -> INC_ prefix (keep)
        "WALLE_BRIEF_DESCRIPTION": "INC_BRIEF_DESCRIPTION",
        "WALLE_ACTION": "INC_ACTION",
        "WALLE_RESOLUTION": "INC_RESOLUTION",
        "WALLE_UPDATE_ACTION_ESS": "INC_UPDATE_ACTION_ESS",
        "WALLE_UH_ESS_ERRORMSG": "INC_UH_ESS_ERRORMSG",
        "WALLE_UPDATE_ACTION": "INC_UPDATE_ACTION",
        "WALLE_COMMENTS": "INC_COMMENTS",
        "WALLE_UH_MONITORING_NOTES": "INC_UH_MONITORING_NOTES",
    }

    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

    # If both IDs exist, keep only INC_ID (but report mismatches).
    if "INC_ID" in df.columns and "WALLE_IN_ID" in df.columns:
        mism = (
            df["INC_ID"].astype(str).str.strip()
            != df["WALLE_IN_ID"].astype(str).str.strip()
        ).sum()
        print(f"[ID] mismatches INC_ID vs WALLE_IN_ID: {int(mism)}", flush=True)
        df = df.drop(columns=["WALLE_IN_ID"])

    # Keep only the columns we care about (contract).
    ordered_cols = [
        "INC_ID",
        "DEX_L1",
        "DEX_L2",
        "DEX_L3",
        "DEX_L4",
        "WALLE_L1",
        "WALLE_L2",
        "WALLE_L3",
        "WALLE_L4",
        "WALLE_VENDOR",
        "WALLE_AI_RATIONALE",
        "WALLE_AI_KEYWORDS",
        "WALLE_AI_ROOT_CAUSE_INDICATOR",
        "WALLE_AI_ROOT_CAUSE",
        "WALLE_AI_L4_CONFIDENCE",
        "WALLE_AI_L4_RESOLUTION_ACTION",
        "WALLE_L4_ACTIONABLE",
        "WALLE_L4_ACTIONABILITY_REASON",
        "WALLE_AI_L4_RATIONALE",
        "INC_BRIEF_DESCRIPTION",
        "INC_ACTION",
        "INC_RESOLUTION",
        "INC_UPDATE_ACTION_ESS",
        "INC_UH_ESS_ERRORMSG",
        "INC_UPDATE_ACTION",
        "INC_COMMENTS",
        "INC_UH_MONITORING_NOTES",
    ]
    cols_present = [c for c in ordered_cols if c in df.columns]
    df_view = df[cols_present].copy()

    # Sanitize illegal control characters for Excel (openpyxl restriction).
    # These can appear in free-text fields and will crash the write.
    obj_cols = [c for c in df_view.columns if df_view[c].dtype == "object"]
    for c in obj_cols:
        df_view[c] = df_view[c].apply(
            lambda x: ILLEGAL_CHARACTERS_RE.sub("", x) if isinstance(x, str) else x
        )

    # ---- Metric 1: coverage ----
    rows = len(df_view)
    out_rows: list[dict] = []
    for k in (1, 2, 3, 4):
        dex_col = f"DEX_L{k}"
        wal_col = f"WALLE_L{k}"
        dex_cov = None
        wal_cov = None
        if dex_col in df_view.columns:
            dex_cov = float(df_view[dex_col].apply(lambda v: _is_usable_label(v, generic)).mean() * 100)
        if wal_col in df_view.columns:
            wal_cov = float(df_view[wal_col].apply(lambda v: _is_usable_label(v, generic)).mean() * 100)
        out_rows.append(
            {
                "level": f"L{k}",
                "rows": rows,
                "dex_coverage_pct": dex_cov,
                "walle_coverage_pct": wal_cov,
            }
        )

    metric_1 = pd.DataFrame(out_rows)

    # ---- Metric 2: granularity / concentration / entropy ----
    # For each model + level, on usable labels only:
    # - unique label count
    # - top-10 share (%)
    # - entropy (higher means more spread; too low means overly concentrated)
    gran_rows: list[dict] = []
    top_labels_rows: list[dict] = []
    for model_prefix in ("DEX", "WALLE"):
        for k in (1, 2, 3, 4):
            col = f"{model_prefix}_L{k}"
            if col not in df_view.columns:
                continue
            usable = df_view.loc[_usable_mask(df_view[col], generic), col]
            norm = _normalize_label_series(usable)
            vc = norm.value_counts()
            unique_labels = int((vc > 0).sum())
            top10_share = _top_share(norm, 10)
            ent = _entropy_from_counts(vc)

            gran_rows.append(
                {
                    "model": model_prefix,
                    "level": f"L{k}",
                    "usable_rows": int(len(norm)),
                    "unique_labels": unique_labels,
                    "top10_share_pct": top10_share,
                    "entropy_bits": ent,
                }
            )

            # keep top 20 labels for explainability
            for label, cnt in vc.head(20).items():
                top_labels_rows.append(
                    {
                        "model": model_prefix,
                        "level": f"L{k}",
                        "label": label,
                        "count": int(cnt),
                        "pct_of_usable": float(cnt / len(norm) * 100.0) if len(norm) else 0.0,
                    }
                )

    metric_2 = pd.DataFrame(gran_rows)
    metric_2_top = pd.DataFrame(top_labels_rows)

    # ---- Metric 3: agreement + disagreements (DEX vs WALLE) ----
    # Exact match at each level, on rows where BOTH sides have usable labels.
    agree_rows: list[dict] = []
    disagree_rows: list[dict] = []
    for k in (1, 2, 3, 4):
        dex_col = f"DEX_L{k}"
        wal_col = f"WALLE_L{k}"
        if dex_col not in df_view.columns or wal_col not in df_view.columns:
            continue
        dex_usable = _usable_mask(df_view[dex_col], generic)
        wal_usable = _usable_mask(df_view[wal_col], generic)
        both = dex_usable & wal_usable
        both_n = int(both.sum())
        if both_n == 0:
            agree_rows.append(
                {
                    "level": f"L{k}",
                    "rows_total": rows,
                    "rows_both_usable": 0,
                    "exact_match_pct": None,
                }
            )
            continue

        dex_norm = _normalize_label_series(df_view.loc[both, dex_col])
        wal_norm = _normalize_label_series(df_view.loc[both, wal_col])
        match = (dex_norm == wal_norm)
        match_pct = float(match.mean() * 100.0)

        agree_rows.append(
            {
                "level": f"L{k}",
                "rows_total": rows,
                "rows_both_usable": both_n,
                "exact_match_pct": match_pct,
            }
        )

        # Top disagreement pairs
        pairs = pd.DataFrame({"dex": dex_norm, "walle": wal_norm})
        pairs = pairs[pairs["dex"] != pairs["walle"]]
        if not pairs.empty:
            pair_counts = pairs.value_counts().head(25)
            for (dex_label, wal_label), cnt in pair_counts.items():
                disagree_rows.append(
                    {
                        "level": f"L{k}",
                        "dex_label": dex_label,
                        "walle_label": wal_label,
                        "count": int(cnt),
                        "pct_of_both_usable": float(cnt / both_n * 100.0),
                    }
                )

    metric_3 = pd.DataFrame(agree_rows)
    metric_3_disagree = pd.DataFrame(disagree_rows)

    # ---- Per-sheet explainability blocks (inserted into each tab) ----
    usable_def = (
        "Usable label = non-NULL, non-blank, and not in generic buckets: "
        f"{', '.join(sorted(generic))}."
    )

    # Coverage deltas (computed from this run)
    cov_lines: list[str] = []
    if not metric_1.empty:
        for r in metric_1.itertuples(index=False):
            if r.dex_coverage_pct is None or r.walle_coverage_pct is None:
                continue
            delta = float(r.walle_coverage_pct) - float(r.dex_coverage_pct)
            cov_lines.append(f"- {r.level}: DEX={float(r.dex_coverage_pct):.1f}% | WALLE={float(r.walle_coverage_pct):.1f}% | WALLE-DEX={delta:+.1f} pp")

    # Granularity comparisons per level
    gran_lines: list[str] = []
    if not metric_2.empty:
        for lvl in ("L1", "L2", "L3", "L4"):
            d = metric_2[(metric_2["model"] == "DEX") & (metric_2["level"] == lvl)]
            w = metric_2[(metric_2["model"] == "WALLE") & (metric_2["level"] == lvl)]
            if d.empty or w.empty:
                continue
            d = d.iloc[0]
            w = w.iloc[0]
            gran_lines.append(
                f"- {lvl}: top10_share DEX={float(d['top10_share_pct']):.1f}% vs WALLE={float(w['top10_share_pct']):.1f}% | "
                f"entropy DEX={float(d['entropy_bits']):.2f} vs WALLE={float(w['entropy_bits']):.2f} | "
                f"unique_labels DEX={int(d['unique_labels'])} vs WALLE={int(w['unique_labels'])}"
            )

    # Agreement lines per level
    agree_lines: list[str] = []
    if not metric_3.empty:
        for r in metric_3.itertuples(index=False):
            if r.exact_match_pct is None:
                agree_lines.append(f"- {r.level}: rows_both_usable=0 (agreement not computed)")
            else:
                agree_lines.append(
                    f"- {r.level}: exact_match={float(r.exact_match_pct):.1f}% on rows_both_usable={int(r.rows_both_usable)} (of rows_total={int(r.rows_total)})"
                )

    per_sheet_lines: dict[str, list[str]] = {
        "data": [
            "HOW TO READ THIS SHEET (data)",
            "- This is the row-level joined dataset used by all metrics.",
            "- Use it for spot checks: filter where DEX_Lk != WALLE_Lk, then read INC_* text and WALLE_AI_* rationale/confidence.",
            f"- {usable_def}",
            "PITFALLS",
            "- Free text is noisy; disagreements can be taxonomy differences rather than model errors.",
        ],
        "metric_1_coverage": [
            "METRIC 1: COVERAGE / COMPLETENESS (metric_1_coverage)",
            "- Meaning: % of rows with a usable label at each level for DEX and WALLE.",
            "- Columns: level, rows, dex_coverage_pct, walle_coverage_pct.",
            f"- {usable_def}",
            "INFERENCES FROM THIS RUN (DEX vs WALLE)",
            *(cov_lines if cov_lines else ["- (no coverage comparisons available)"]),
            "HOW TO USE IT",
            "- If coverage drops sharply at L3/L4 for a model, downstream analytics/actionability at that level will suffer.",
            "PITFALLS",
            "- Higher coverage can be achieved by using broad catch-alls; validate with Metric 2.",
        ],
        "metric_2_granularity": [
            "METRIC 2: GRANULARITY / CONCENTRATION / ENTROPY (metric_2_granularity)",
            "- Meaning (usable labels only):",
            "  - unique_labels: number of distinct labels used (after normalization).",
            "  - top10_share_pct: % mass held by top 10 labels (higher = more concentrated).",
            "  - entropy_bits: spread of label distribution (higher = more diverse).",
            f"- {usable_def}",
            "INFERENCES FROM THIS RUN (DEX vs WALLE)",
            *(gran_lines if gran_lines else ["- (no granularity comparisons available)"]),
            "HOW TO USE IT",
            "- High top10_share + low entropy suggests a few dominant buckets (coarse taxonomy or weak differentiation).",
            "PITFALLS",
            "- More granularity is not more correctness. Validate via Metric 3 + spot checks in data.",
        ],
        "metric_2_top_labels": [
            "METRIC 2 (DETAIL): TOP LABELS (metric_2_top_labels)",
            "- Meaning: top labels per (model, level) with count and pct_of_usable.",
            "- Use it to understand what drives concentration seen in metric_2_granularity.",
            f"- {usable_def}",
            "HOW TO USE IT",
            "- If a small set of labels dominates L4, review those incidents to see if taxonomy is collapsing into a few buckets.",
            "PITFALLS",
            "- Labels are normalized (lower/strip). Formatting-only differences are intentionally collapsed.",
        ],
        "metric_3_agreement": [
            "METRIC 3: AGREEMENT (EXACT STRING MATCH) (metric_3_agreement)",
            "- Meaning: exact-match % between DEX and WALLE, computed only on rows where BOTH sides have usable labels.",
            "- Columns: level, rows_total, rows_both_usable, exact_match_pct.",
            f"- {usable_def}",
            "INFERENCES FROM THIS RUN (DEX vs WALLE)",
            *(agree_lines if agree_lines else ["- (no agreement comparisons available)"]),
            "HOW TO USE IT",
            "- Low agreement at L1/L2 suggests fundamental taxonomy mismatch; low mainly at L4 suggests different granularity/issue framing.",
            "PITFALLS",
            "- This is NOT semantic agreement; synonyms or near-matches count as disagreement.",
        ],
        "metric_3_disagreements": [
            "METRIC 3 (DETAIL): TOP DISAGREEMENT PAIRS (metric_3_disagreements)",
            "- Meaning: most frequent (DEX label -> WALLE label) mismatches among mutually usable rows.",
            "- Columns: level, dex_label, walle_label, count, pct_of_both_usable.",
            "HOW TO USE IT",
            "- High-frequency pairs usually indicate systematic mapping differences; candidates for taxonomy reconciliation/mapping tables.",
            "PITFALLS",
            "- Frequency does not imply which model is correct; validate by reading incidents in the data sheet.",
        ],
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output_path, engine="openpyxl") as xw:
        df_view.to_excel(xw, sheet_name="data", index=False)
        metric_1.to_excel(xw, sheet_name="metric_1_coverage", index=False)
        metric_2.to_excel(xw, sheet_name="metric_2_granularity", index=False)
        metric_2_top.to_excel(xw, sheet_name="metric_2_top_labels", index=False)
        metric_3.to_excel(xw, sheet_name="metric_3_agreement", index=False)
        metric_3_disagree.to_excel(xw, sheet_name="metric_3_disagreements", index=False)

    # Prepend explainability into each sheet (so guidance lives with the tab).
    _prepend_sheet_explainability(output_path, per_sheet_lines)

    print(f"Wrote report: {output_path}", flush=True)
    print(metric_1.to_string(index=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

