"""
TaxonomyValidator: deterministic enforcement of (L1, L2, L3) outputs against
the approved INCIDENT_TAXONOMY.

Behavior contract (Req 1+3):

1. Validate each level against the approved taxonomy.
2. Try a bounded deterministic repair on each level using:
   - normalization (case-fold; underscores/hyphens/spaces equivalent;
     punctuation stripped; whitespace collapsed)
   - an optional explicit alias map loaded from aliases.json
3. After repair, re-check the full hierarchy. If the full tuple is valid,
   keep the canonical labels. Otherwise, set ALL three levels to the
   controlled bucket "Uncategorized" (strict-after-repair policy).
4. Never throw: every error is captured and the validator returns a
   ValidationResult with status set appropriately.

This module is pure-Python and performs no I/O beyond reading the alias
JSON file at construction time (best-effort; falls back to empty map on
any error).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .audit_reasons import L123AuditStatus, UNCATEGORIZED_LABEL


logger = logging.getLogger(__name__)


_PUNCT_RE = re.compile(r"[.,;:!?\"'`(){}\[\]]")
_WHITESPACE_RE = re.compile(r"\s+")
_MISSING_TOKENS = frozenset({"", "none", "null", "nan", "n/a", "na"})


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of validating a single (L1, L2, L3) tuple.

    `final_l1/l2/l3` are the values that must be persisted to the
    checkpoint. They are either canonical taxonomy labels or the literal
    string "Uncategorized" (never None, never invented).

    `original_l1/l2/l3` preserve what the model returned, so audit rows
    can show what was actually emitted before bucketing/repair.
    """

    is_valid: bool
    status: L123AuditStatus
    final_l1: str
    final_l2: str
    final_l3: str
    original_l1: str | None
    original_l2: str | None
    original_l3: str | None
    repair_applied: bool
    details: dict[str, Any] = field(default_factory=dict)


def _normalize_for_match(value: Any) -> str:
    """Reduce a value to its canonical lookup form.

    Returns an empty string for missing/None/whitespace inputs.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    # treat sentinel "missing" tokens as missing
    if text.lower() in _MISSING_TOKENS:
        return ""
    text = text.lower()
    text = _PUNCT_RE.sub(" ", text)
    text = text.replace("_", " ").replace("-", " ").replace("/", " ")
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


def _build_label_index(labels: list[str]) -> dict[str, str]:
    """Build a normalized -> canonical lookup for a flat label list."""
    index: dict[str, str] = {}
    for label in labels:
        key = _normalize_for_match(label)
        if key and key not in index:
            index[key] = label
    return index


class TaxonomyValidator:
    """Pure-Python validator for L1/L2/L3 hierarchy outputs."""

    def __init__(
        self,
        taxonomy: Mapping[str, Mapping[str, list[str]]],
        alias_map: Mapping[str, Mapping[str, str]] | None = None,
    ) -> None:
        self._taxonomy = taxonomy

        # Per-level normalized indexes for canonical labels
        self._l1_index: dict[str, str] = _build_label_index(list(taxonomy.keys()))

        self._l2_index_by_l1: dict[str, dict[str, str]] = {}
        self._l3_index_by_l1_l2: dict[tuple[str, str], dict[str, str]] = {}
        # Global L2/L3 indexes (for swapped-level detection)
        l2_global: dict[str, str] = {}
        l3_global: dict[str, str] = {}

        for l1, l2_map in taxonomy.items():
            l2_labels = list(l2_map.keys())
            self._l2_index_by_l1[l1] = _build_label_index(l2_labels)
            for l2 in l2_labels:
                key = _normalize_for_match(l2)
                if key and key not in l2_global:
                    l2_global[key] = l2
                l3_labels = list(l2_map[l2])
                self._l3_index_by_l1_l2[(l1, l2)] = _build_label_index(l3_labels)
                for l3 in l3_labels:
                    key3 = _normalize_for_match(l3)
                    if key3 and key3 not in l3_global:
                        l3_global[key3] = l3

        self._l2_global = l2_global
        self._l3_global = l3_global

        # Alias map (per level). Keys are normalized; values are canonical
        # labels (must already match a real taxonomy entry).
        self._alias_l1: dict[str, str] = {}
        self._alias_l2: dict[str, str] = {}
        self._alias_l3: dict[str, str] = {}
        if alias_map:
            self._load_alias_map(alias_map)

    @classmethod
    def from_files(
        cls,
        taxonomy: Mapping[str, Mapping[str, list[str]]],
        alias_map_path: str | Path | None,
    ) -> "TaxonomyValidator":
        """Construct a validator, loading the alias map from a JSON file.

        Best-effort: if the file is missing or malformed, the alias map is
        treated as empty and a warning is logged. The validator never
        raises during construction.
        """
        alias_map: dict[str, dict[str, str]] | None = None
        if alias_map_path:
            try:
                p = Path(alias_map_path)
                if p.exists():
                    with p.open("r", encoding="utf-8") as fh:
                        raw = json.load(fh)
                    alias_map = {
                        level: {str(k): str(v) for k, v in (raw.get(level) or {}).items()}
                        for level in ("l1", "l2", "l3")
                    }
                else:
                    logger.warning(
                        "TaxonomyValidator: alias map file %s does not exist; "
                        "proceeding without aliases",
                        alias_map_path,
                    )
            except Exception as exc:
                logger.warning(
                    "TaxonomyValidator: failed to load alias map from %s (%s); "
                    "proceeding without aliases",
                    alias_map_path,
                    exc,
                )
                alias_map = None
        return cls(taxonomy=taxonomy, alias_map=alias_map)

    def _load_alias_map(self, alias_map: Mapping[str, Mapping[str, str]]) -> None:
        for level_name, target in (
            ("l1", self._alias_l1),
            ("l2", self._alias_l2),
            ("l3", self._alias_l3),
        ):
            section = alias_map.get(level_name) or {}
            for raw_key, canonical in section.items():
                norm = _normalize_for_match(raw_key)
                if not norm or not canonical:
                    continue
                target[norm] = str(canonical)

    @staticmethod
    def _str_or_none(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text if text else None

    def _resolve_l1(self, raw: Any) -> tuple[str | None, bool]:
        """Return (canonical_l1_or_None, repair_applied)."""
        # Direct exact match (preserve no-repair status)
        if raw is not None and isinstance(raw, str) and raw in self._taxonomy:
            return raw, False
        norm = _normalize_for_match(raw)
        if not norm:
            return None, False
        if norm in self._l1_index:
            canonical = self._l1_index[norm]
            return canonical, canonical != raw
        if norm in self._alias_l1:
            return self._alias_l1[norm], True
        return None, False

    def _resolve_l2(self, raw: Any, l1: str | None) -> tuple[str | None, bool]:
        if l1 is None:
            return None, False
        l2_index = self._l2_index_by_l1.get(l1, {})
        if raw is not None and isinstance(raw, str) and raw in (self._taxonomy.get(l1) or {}):
            return raw, False
        norm = _normalize_for_match(raw)
        if not norm:
            return None, False
        if norm in l2_index:
            canonical = l2_index[norm]
            return canonical, canonical != raw
        # Alias may target a label belonging to a different L1; only accept
        # if it resolves under the *current* L1
        alias_target = self._alias_l2.get(norm)
        if alias_target and alias_target in (self._taxonomy.get(l1) or {}):
            return alias_target, True
        return None, False

    def _resolve_l3(
        self, raw: Any, l1: str | None, l2: str | None
    ) -> tuple[str | None, bool]:
        if l1 is None or l2 is None:
            return None, False
        valid_l3 = list((self._taxonomy.get(l1) or {}).get(l2) or [])
        if not valid_l3:
            return None, False
        if raw is not None and isinstance(raw, str) and raw in valid_l3:
            return raw, False
        index = self._l3_index_by_l1_l2.get((l1, l2), {})
        norm = _normalize_for_match(raw)
        if not norm:
            return None, False
        if norm in index:
            canonical = index[norm]
            return canonical, canonical != raw
        alias_target = self._alias_l3.get(norm)
        if alias_target and alias_target in valid_l3:
            return alias_target, True
        return None, False

    def _detect_swapped(self, raw_l1: Any, raw_l2: Any, raw_l3: Any) -> bool:
        """Detect a likely L1<->L2 swap (the most common failure mode)."""
        norm_l1 = _normalize_for_match(raw_l1)
        norm_l2 = _normalize_for_match(raw_l2)
        if not norm_l1 or not norm_l2:
            return False
        # If raw_l1 looks like a known L2 AND raw_l2 looks like a known L1,
        # treat as swapped.
        l1_looks_like_l2 = norm_l1 in self._l2_global
        l2_looks_like_l1 = norm_l2 in self._l1_index
        return l1_looks_like_l2 and l2_looks_like_l1

    def _bucketed(
        self,
        status: L123AuditStatus,
        raw_l1: Any,
        raw_l2: Any,
        raw_l3: Any,
        details: dict[str, Any] | None = None,
    ) -> ValidationResult:
        return ValidationResult(
            is_valid=False,
            status=status,
            final_l1=UNCATEGORIZED_LABEL,
            final_l2=UNCATEGORIZED_LABEL,
            final_l3=UNCATEGORIZED_LABEL,
            original_l1=self._str_or_none(raw_l1),
            original_l2=self._str_or_none(raw_l2),
            original_l3=self._str_or_none(raw_l3),
            repair_applied=False,
            details=details or {},
        )

    def validate(
        self, l1: Any, l2: Any, l3: Any
    ) -> ValidationResult:
        """Validate a single (L1, L2, L3) tuple and return the outcome."""
        try:
            return self._validate_unsafe(l1, l2, l3)
        except Exception as exc:
            logger.warning(
                "TaxonomyValidator: unexpected error during validation (%s); "
                "bucketing to Uncategorized",
                exc,
            )
            return self._bucketed(
                "missing", l1, l2, l3, details={"validator_exception": str(exc)}
            )

    def _validate_unsafe(
        self, l1: Any, l2: Any, l3: Any
    ) -> ValidationResult:
        norm_l1 = _normalize_for_match(l1)
        norm_l2 = _normalize_for_match(l2)
        norm_l3 = _normalize_for_match(l3)

        # All three missing => "missing"
        if not norm_l1 and not norm_l2 and not norm_l3:
            return self._bucketed("missing", l1, l2, l3)

        # Detect classic L1<->L2 swap before attempting strict resolution.
        if self._detect_swapped(l1, l2, l3):
            return self._bucketed(
                "swapped_levels_suspected",
                l1,
                l2,
                l3,
                details={
                    "norm_l1": norm_l1,
                    "norm_l2": norm_l2,
                    "swap_hint": "raw_l1 matches a known L2 and raw_l2 matches a known L1",
                },
            )

        canon_l1, repair_l1 = self._resolve_l1(l1)
        if canon_l1 is None:
            # Any missing component fails the whole tuple per strict policy.
            status: L123AuditStatus = "missing" if not norm_l1 else "invalid_label"
            return self._bucketed(
                status, l1, l2, l3, details={"failed_level": "l1"}
            )

        canon_l2, repair_l2 = self._resolve_l2(l2, canon_l1)
        if canon_l2 is None:
            status = "missing" if not norm_l2 else "invalid_hierarchy" if norm_l2 in self._l2_global else "invalid_label"
            return self._bucketed(
                status, l1, l2, l3, details={"failed_level": "l2", "resolved_l1": canon_l1}
            )

        canon_l3, repair_l3 = self._resolve_l3(l3, canon_l1, canon_l2)
        if canon_l3 is None:
            status = "missing" if not norm_l3 else "invalid_hierarchy" if norm_l3 in self._l3_global else "invalid_label"
            return self._bucketed(
                status,
                l1,
                l2,
                l3,
                details={
                    "failed_level": "l3",
                    "resolved_l1": canon_l1,
                    "resolved_l2": canon_l2,
                },
            )

        repair_applied = bool(repair_l1 or repair_l2 or repair_l3)
        status_final: L123AuditStatus = "valid_after_repair" if repair_applied else "valid"

        return ValidationResult(
            is_valid=True,
            status=status_final,
            final_l1=canon_l1,
            final_l2=canon_l2,
            final_l3=canon_l3,
            original_l1=self._str_or_none(l1),
            original_l2=self._str_or_none(l2),
            original_l3=self._str_or_none(l3),
            repair_applied=repair_applied,
            details={
                "repaired_levels": [
                    name
                    for name, repaired in (
                        ("l1", repair_l1),
                        ("l2", repair_l2),
                        ("l3", repair_l3),
                    )
                    if repaired
                ],
            },
        )
