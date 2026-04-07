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

    # ---- Notes / explainability ----
    # Keep this sheet structured so it reads like a "how to interpret this report" doc.
    # (Avoids needing to infer meaning from sheet names / column names.)
    notes_rows: list[dict] = []

    usable_def = (
        "A label is treated as 'usable' if it is not NULL/blank and not one of the configured "
        f"generic buckets: {', '.join(sorted(generic))}."
    )

    notes_rows.append(
        {
            "section": "Report overview",
            "sheet": "",
            "what_it_is": "This workbook compares DEXter vs WALLE classifications without a gold/correct label set.",
            "how_to_read": (
                "Use metric_1_coverage to understand missingness, metric_2_granularity to understand label spread "
                "and concentration, and metric_3_agreement/disagreements to understand where the two systems diverge."
            ),
            "key_columns": "",
            "inferences_to_draw": "Identify gaps (coverage), overly-broad buckets (granularity), and systematic mapping differences (disagreements).",
            "pitfalls": "These metrics do NOT measure correctness; they measure completeness, diversity, and string-level agreement.",
        }
    )
    notes_rows.append(
        {
            "section": "Definitions",
            "sheet": "",
            "what_it_is": "Usable label definition",
            "how_to_read": usable_def,
            "key_columns": "",
            "inferences_to_draw": "Coverage/Agreement are computed only over usable labels (see each metric).",
            "pitfalls": "If your data uses additional 'unknown-like' strings, add them to --generic or metrics will overstate coverage.",
        }
    )

    notes_rows.append(
        {
            "section": "Sheet guide",
            "sheet": "data",
            "what_it_is": "Row-level joined data (DEX_* + WALLE_* + incident context) for manual inspection.",
            "how_to_read": (
                "Filter by mismatches (DEX_Lk vs WALLE_Lk), then read incident text columns (INC_*) and "
                "WALLE explainability columns (WALLE_AI_*) to understand why WALLE chose its label."
            ),
            "key_columns": (
                "INC_ID; DEX_L1..DEX_L4; WALLE_L1..WALLE_L4; WALLE_AI_L4_CONFIDENCE; "
                "WALLE_AI_L4_RATIONALE; WALLE_AI_RATIONALE; WALLE_AI_KEYWORDS; INC_BRIEF_DESCRIPTION/INC_ACTION/INC_RESOLUTION"
            ),
            "inferences_to_draw": (
                "Spot recurring disagreement patterns; identify whether disagreements are taxonomy differences "
                "(synonyms/mappings) vs genuinely different interpretation of incident text."
            ),
            "pitfalls": "Free-text fields can be noisy; use the sampled CSV upstream for faster qualitative review.",
        }
    )

    notes_rows.append(
        {
            "section": "Metric 1",
            "sheet": "metric_1_coverage",
            "what_it_is": "Coverage/completeness at L1-L4 for DEX vs WALLE (usable labels only).",
            "how_to_read": (
                "Each row is a level (L1..L4). dex_coverage_pct is the % rows where DEX label is usable at that level; "
                "walle_coverage_pct is the same for WALLE."
            ),
            "key_columns": "level; rows; dex_coverage_pct; walle_coverage_pct",
            "inferences_to_draw": (
                "Lower coverage indicates missing labels or 'unknown/other' usage. If one system has materially lower "
                "coverage at L4, its downstream actionability/analytics will be limited at that level."
            ),
            "pitfalls": (
                "Coverage depends on the generic bucket list. Also, higher coverage can be achieved by overusing a single "
                "catch-all label—use Metric 2 to validate that."
            ),
        }
    )

    notes_rows.append(
        {
            "section": "Metric 2",
            "sheet": "metric_2_granularity",
            "what_it_is": "Granularity / concentration metrics for each model and level (usable labels only).",
            "how_to_read": (
                "unique_labels counts distinct labels used (after normalization). top10_share_pct measures how much mass "
                "the top 10 labels hold (higher = more concentrated). entropy_bits measures spread (higher = more diverse)."
            ),
            "key_columns": "model; level; usable_rows; unique_labels; top10_share_pct; entropy_bits",
            "inferences_to_draw": (
                "Very high top10_share_pct + low entropy suggests a few dominant buckets (coarse taxonomy or weak model). "
                "Very low top10_share_pct + very high unique_labels might indicate over-fragmentation or noisy labeling."
            ),
            "pitfalls": (
                "Granularity is not accuracy. A model can be 'more granular' but wrong. Compare with Metric 3 and spot-check "
                "in the data sheet."
            ),
        }
    )

    notes_rows.append(
        {
            "section": "Metric 2 (details)",
            "sheet": "metric_2_top_labels",
            "what_it_is": "Top labels per model/level to explain what drives concentration.",
            "how_to_read": (
                "For each (model, level), labels are listed with their count and pct_of_usable. "
                "Use this to identify dominant buckets and whether they are meaningful or too generic."
            ),
            "key_columns": "model; level; label; count; pct_of_usable",
            "inferences_to_draw": (
                "If one or two labels dominate L4, investigate whether those are true frequent issues or collapse of taxonomy. "
                "Use these labels as starting points for stratified qualitative review."
            ),
            "pitfalls": "Normalization lowercases/strips; distinct formatting differences are intentionally collapsed.",
        }
    )

    notes_rows.append(
        {
            "section": "Metric 3",
            "sheet": "metric_3_agreement",
            "what_it_is": "Exact-match agreement rate between DEX and WALLE at each level (usable labels on BOTH sides).",
            "how_to_read": (
                "rows_both_usable is the denominator for exact_match_pct. If rows_both_usable is small, agreement is unstable "
                "and likely dominated by missingness rather than true differences."
            ),
            "key_columns": "level; rows_total; rows_both_usable; exact_match_pct",
            "inferences_to_draw": (
                "Low agreement at L1/L2 suggests fundamental taxonomy mismatch; low agreement mainly at L4 suggests different "
                "granularity or divergent issue framing."
            ),
            "pitfalls": (
                "This is string equality, not semantic equivalence. Synonyms or near-matches count as disagreement. "
                "If desired later, we can add a mapping table or a semantic similarity variant."
            ),
        }
    )

    notes_rows.append(
        {
            "section": "Metric 3 (details)",
            "sheet": "metric_3_disagreements",
            "what_it_is": "Top 25 disagreement pairs per level (DEX label vs WALLE label) among mutually usable rows.",
            "how_to_read": (
                "Each row is a (dex_label, walle_label) pair with count and pct_of_both_usable. "
                "Sort by count to find systematic divergences."
            ),
            "key_columns": "level; dex_label; walle_label; count; pct_of_both_usable",
            "inferences_to_draw": (
                "High-frequency pairs often indicate a stable mapping difference (e.g., DEX bucket A aligns to WALLE bucket B). "
                "These are prime candidates for taxonomy reconciliation or mapping tables."
            ),
            "pitfalls": (
                "Pairs reflect disagreement frequency, not which one is correct. Always validate with incident text from the data sheet."
            ),
        }
    )

    # Add a compact, computed “quick inferences” section based on current results.
    notes_rows.append(
        {
            "section": "Quick inferences (computed)",
            "sheet": "",
            "what_it_is": "Lightweight automatically-generated comparisons from this run’s output.",
            "how_to_read": "Use as pointers; validate with the other sheets before concluding.",
            "key_columns": "",
            "inferences_to_draw": "",
            "pitfalls": "These are heuristics; do not treat as final conclusions.",
        }
    )
    if not metric_1.empty:
        for _row in metric_1.itertuples(index=False):
            if _row.dex_coverage_pct is not None and _row.walle_coverage_pct is not None:
                delta = float(_row.walle_coverage_pct) - float(_row.dex_coverage_pct)
                notes_rows.append(
                    {
                        "section": "Quick inferences (computed)",
                        "sheet": "",
                        "what_it_is": f"Coverage delta at {_row.level}",
                        "how_to_read": "Positive means WALLE covers more rows with usable labels than DEX at this level.",
                        "key_columns": "",
                        "inferences_to_draw": f"WALLE-DEX coverage delta: {delta:+.1f} percentage points.",
                        "pitfalls": "Higher coverage can be inflated by broad buckets; check Metric 2.",
                    }
                )
    if not metric_2.empty:
        try:
            d_l4 = metric_2[(metric_2["model"] == "DEX") & (metric_2["level"] == "L4")].head(1)
            w_l4 = metric_2[(metric_2["model"] == "WALLE") & (metric_2["level"] == "L4")].head(1)
            if not d_l4.empty and not w_l4.empty:
                notes_rows.append(
                    {
                        "section": "Quick inferences (computed)",
                        "sheet": "",
                        "what_it_is": "L4 granularity comparison",
                        "how_to_read": "Higher entropy + lower top10_share_pct generally indicates more differentiated labels.",
                        "key_columns": "",
                        "inferences_to_draw": (
                            f"DEX L4: top10_share={float(d_l4['top10_share_pct'].iloc[0]):.1f}%, "
                            f"entropy={float(d_l4['entropy_bits'].iloc[0]):.2f} bits. "
                            f"WALLE L4: top10_share={float(w_l4['top10_share_pct'].iloc[0]):.1f}%, "
                            f"entropy={float(w_l4['entropy_bits'].iloc[0]):.2f} bits."
                        ),
                        "pitfalls": "Granularity is not correctness; validate via disagreements and spot checks.",
                    }
                )
        except Exception:
            pass

    notes_df = pd.DataFrame(notes_rows)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output_path, engine="openpyxl") as xw:
        df_view.to_excel(xw, sheet_name="data", index=False)
        metric_1.to_excel(xw, sheet_name="metric_1_coverage", index=False)
        metric_2.to_excel(xw, sheet_name="metric_2_granularity", index=False)
        metric_2_top.to_excel(xw, sheet_name="metric_2_top_labels", index=False)
        metric_3.to_excel(xw, sheet_name="metric_3_agreement", index=False)
        metric_3_disagree.to_excel(xw, sheet_name="metric_3_disagreements", index=False)
        notes_df.to_excel(xw, sheet_name="notes", index=False)

    print(f"Wrote report: {output_path}", flush=True)
    print(metric_1.to_string(index=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

