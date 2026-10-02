"""Customer matching: fuzzy match a name as written in a document against
Odoo partner records.

Order of attempts:
  1. exact (case-insensitive) name
  2. fuzzy (rapidfuzz token_set_ratio) — handles "Northline Building Supply"
     vs "N. Building Supply", suffix noise, etc.
  3. email domain match when the document contains an email address
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from rapidfuzz import fuzz, process

from ..odoo import PartnerRecord


@dataclass
class CustomerMatch:
    partner: Optional[PartnerRecord]
    score: float          # 0..100
    method: str          # exact | fuzzy | email | none


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()


def match_customer(query: str, partners: list[PartnerRecord]) -> CustomerMatch:
    q = _norm(query)
    if not q:
        return CustomerMatch(None, 0.0, "none")
    for p in partners:
        if _norm(p.name) == q:
            return CustomerMatch(p, 100.0, "exact")
    choices = {_norm(p.name): p for p in partners}
    choices = {k: v for k, v in choices.items() if k}
    if not choices:
        return CustomerMatch(None, 0.0, "none")
    keys = list(choices.keys())
    hit = process.extractOne(q, keys, scorer=fuzz.token_set_ratio)
    if hit:
        best, score, _ = hit
        if score >= 55:
            return CustomerMatch(choices[best], round(float(score), 1), "fuzzy")
    return CustomerMatch(None, 0.0, "none")


def match_customer_by_email(email: str, partners: list[PartnerRecord]) -> CustomerMatch:
    m = re.search(r"[\w.+-]+@([\w.-]+\.\w+)", email or "")
    if not m:
        return CustomerMatch(None, 0.0, "none")
    domain = m.group(1).lower()
    for p in partners:
        if p.email and domain in p.email.lower():
            return CustomerMatch(p, 95.0, "email")
    return CustomerMatch(None, 0.0, "none")
