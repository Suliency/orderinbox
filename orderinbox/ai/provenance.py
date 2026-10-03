"""Per-field confidence and provenance.

From the integration proposal (Section 10, "Confidence-Based Escalation"):

    Every extracted field should carry provenance.

    {
      "charge": "ocean_freight",
      "amount": 2150,
      "confidence": 0.98,
      "source": {"file": "agent_quote_234.pdf", "page": 2,
                  "text": "O/F USD 2,150 / 40HC"}
    }

The LLM is only as trustworthy as the source it cites. Storing *where* a value
came from (file, page, the exact text span) lets a reviewer verify a value in
one click and lets the escalation policy reason about trust per field, not per
document.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class Source(BaseModel):
    """Where an extracted value came from. Every attribute is optional so a
    deterministic (non-LLM) extraction can record the format instead of a
    document span."""
    file: str = ""            # attachment / document name
    page: Optional[int] = None
    sheet: str = ""           # spreadsheet tab, when applicable
    text: str = ""            # the exact span the value was read from


class FieldProvenance(BaseModel):
    """A single extracted value plus how confident we are and where it came from.

    `confidence` is a float in [0, 1]. It is *not* a probability of the value
    being correct in the strict sense — it is the model's stated certainty
    combined with how unambiguous the source span is. The escalation policy
    (Section 10) acts on this number.
    """
    value: Any = None
    confidence: float = 0.0
    source: Source = Field(default_factory=Source)

    def clamp(self) -> "FieldProvenance":
        self.confidence = max(0.0, min(1.0, float(self.confidence or 0.0)))
        return self


def provenance(value: Any, confidence: float = 0.0, *,
               file: str = "", page: Optional[int] = None,
               sheet: str = "", text: str = "") -> FieldProvenance:
    """Convenience constructor used by extractors."""
    return FieldProvenance(
        value=value,
        confidence=confidence,
        source=Source(file=file, page=page, sheet=sheet, text=text),
    ).clamp()
