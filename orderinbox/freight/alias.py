"""Persistent alias learning (Section 13).

Counterparties use their own shorthand. Two forwarders both calling a port
congestion fee "PCS" is fine, but one agent's "PCS" can mean something an
other agent's "PCS" does not. The system learns these conventions and, before
asking the model to interpret a future quote from that counterparty, retrieves
the stored mapping.

    Agent:  Shanghai ABC Logistics
    Alias:  PCS
    Canonical: PORT_CONGESTION_SURCHARGE
    Evidence: confirmed by operator

The store is small and file-backed (one JSON document) so a private
deployment keeps counterparty knowledge on-prem. It is a *learning* layer:
entries are added with an evidence note and a confidence, and the interpreter
can weight them when resolving ambiguous terms.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

log = logging.getLogger("orderinbox.freight.alias")


class AliasRecord(BaseModel):
    counterparty: str            # normalized agent / carrier name
    alias: str                   # the term as the counterparty writes it
    canonical: str               # ontology code it maps to
    evidence: str = ""           # why we believe this (e.g. "confirmed by operator")
    confidence: float = 0.9
    created_at: str = ""
    times_confirmed: int = 1

    @property
    def key(self) -> tuple[str, str]:
        return (self.counterparty.lower(), self.alias.upper())


class AliasStore:
    """File-backed store of counterparty-specific terminology.

    Persistence is a single JSON array so it survives restarts and is trivial
    to back up. All lookups are case-insensitive on the counterparty and
    case-normalized on the alias.
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self._records: list[AliasRecord] = []
        if self.path and self.path.exists():
            self._load()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
            self._records = [AliasRecord.model_validate(r) for r in raw]
        except Exception as exc:
            log.warning("alias store load failed (%s); starting empty", exc)
            self._records = []

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(
            [r.model_dump() for r in self._records], indent=2, ensure_ascii=False))

    # ------------------------------------------------------------------
    def learn(self, counterparty: str, alias: str, canonical: str,
              evidence: str = "", confidence: float = 0.9) -> AliasRecord:
        """Record (or reinforce) a counterparty-specific mapping."""
        cp = (counterparty or "").strip()
        al = (alias or "").strip()
        if not cp or not al or not canonical:
            raise ValueError("counterparty, alias and canonical are required")
        for r in self._records:
            if r.key == (cp.lower(), al.upper()):
                r.times_confirmed += 1
                r.confidence = min(1.0, max(r.confidence, confidence))
                if evidence:
                    r.evidence = evidence
                return r
        rec = AliasRecord(
            counterparty=cp, alias=al, canonical=canonical,
            evidence=evidence, confidence=confidence,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self._records.append(rec)
        return rec

    def lookup(self, counterparty: str, alias: str) -> Optional[AliasRecord]:
        cp, al = (counterparty or "").lower(), (alias or "").upper()
        for r in self._records:
            if r.key == (cp, al):
                return r
        return None

    def all_for(self, counterparty: str) -> list[AliasRecord]:
        cp = (counterparty or "").lower()
        return [r for r in self._records if r.counterparty.lower() == cp]

    # ------------------------------------------------------------------
    def resolve(self, counterparty: str, term: str) -> Optional[str]:
        """Resolve one counterparty-specific term to a canonical code, if we
        have learned it. Returns None to fall through to generic ontology."""
        r = self.lookup(counterparty, term)
        return r.canonical if r else None

    def conventions_for(self, counterparty: str) -> dict[str, str]:
        """All learned mappings for a counterparty, as {alias: canonical}.

        Intended to be injected into the model's context before interpretation
        ("retrieve those conventions before asking Strata to interpret future
        quotes" — Section 13).
        """
        return {r.alias.upper(): r.canonical for r in self.all_for(counterparty)}
