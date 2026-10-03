"""Producer / Verifier architecture (Section 11).

    quote
      │
   ┌──▼───┐
   │ Model A  │   Extractor (fast, e.g. 14B) — produces the JSON
   └──┬───┘
      │ JSON
   ┌──▼─────┐
   │ Model B  │   Validator (stronger, e.g. 32B) — re-derives the same fields
   └──┬─────┘
      │
   agree?  ── yes ──> accept
      │
   no ──────────────> escalate (cloud / human)

Two independent models are asked for the *same* structured fields. Agreement
between two independent samplers is a cheap, high-signal check that the
extraction is stable and not a one-off hallucination. Disagreement routes the
record up the escalation ladder (Section 10) instead of being trusted.

The comparison is field-scoped and tolerant of floating-point noise on money
amounts, so a $1,925.00 vs $1925 difference does not count as a conflict.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FieldDiff:
    field: str
    a: Any
    b: Any
    kind: str        # "mismatch" | "missing_a" | "missing_b"


@dataclass
class Verdict:
    agree: bool
    diffs: list[FieldDiff] = field(default_factory=list)

    @property
    def diff_fields(self) -> list[str]:
        return [d.field for d in self.diffs]


def _norm(value: Any) -> Any:
    """Normalize a value for comparison: trim strings, None/''/[]/{} collapse."""
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        return s if s else None
    if isinstance(value, (list, tuple, dict)) and not value:
        return None
    return value


def _close_money(a: Any, b: Any, tol: float = 0.01) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return _norm(a) == _norm(b)


def compare(a: dict[str, Any], b: dict[str, Any],
            fields: list[str], money_fields: set[str] | None = None) -> Verdict:
    """Compare two extracted JSON objects over the given fields.

    Returns a Verdict that is `agree` when every field is present on both
    sides and equal (money fields within tolerance).
    """
    money_fields = money_fields or set()
    diffs: list[FieldDiff] = []
    for f in fields:
        va, vb = _norm(a.get(f)), _norm(b.get(f))
        if va is None and vb is None:
            continue
        if va is None:
            diffs.append(FieldDiff(f, a.get(f), b.get(f), "missing_a"))
        elif vb is None:
            diffs.append(FieldDiff(f, a.get(f), b.get(f), "missing_b"))
        elif f in money_fields:
            if not _close_money(va, vb):
                diffs.append(FieldDiff(f, a.get(f), b.get(f), "mismatch"))
        else:
            if va != vb:
                diffs.append(FieldDiff(f, a.get(f), b.get(f), "mismatch"))
    return Verdict(agree=not diffs, diffs=diffs)
