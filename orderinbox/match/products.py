"""SKU matching: resolve a document line to a catalog product.

Order of attempts:
  1. exact default_code (case-insensitive)
  2. exact alias (customer/legacy codes)
  3. fuzzy against codes + names + aliases (rapidfuzz WRatio)
  4. LLM-assisted: if still unresolved, ask the LLM to pick from candidates
     (only used when a local LLM is available; the caller may pass the client)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from rapidfuzz import fuzz, process

from ..odoo import ProductRecord


@dataclass
class ProductMatch:
    product: Optional[ProductRecord]
    score: float
    method: str          # exact | alias | fuzzy | llm | none


def _product_strings(p: ProductRecord) -> list[str]:
    out = []
    if p.default_code:
        out.append(p.default_code)
    out.extend(p.aliases)
    out.append(p.name)
    return [s for s in (x.strip() for x in out) if s]


def match_product(query: str, products: list[ProductRecord]) -> ProductMatch:
    q = (query or "").strip()
    if not q:
        return ProductMatch(None, 0.0, "none")
    qn = q.upper()
    for p in products:
        if p.default_code and p.default_code.upper() == qn:
            return ProductMatch(p, 100.0, "exact")
        for a in p.aliases:
            if a.upper() == qn:
                return ProductMatch(p, 98.0, "alias")
    # fuzzy pass
    candidates: dict[str, ProductRecord] = {}
    for p in products:
        for s in _product_strings(p):
            candidates[s.upper()] = p
    if not candidates:
        return ProductMatch(None, 0.0, "none")
    keys = list(candidates.keys())
    hit = process.extractOne(qn, keys, scorer=fuzz.WRatio)
    if hit:
        best, score, _ = hit
        if score >= 70:
            return ProductMatch(candidates[best], round(float(score), 1), "fuzzy")
    return ProductMatch(None, 0.0, "none")


def llm_pick(query: str, description: str, products: list[ProductRecord], llm) -> ProductMatch:
    """Ask the LLM to choose the best product from the catalog for a line."""
    if llm is None or not llm.available():
        return ProductMatch(None, 0.0, "none")
    lines = []
    for p in products:
        codes = ", ".join([p.default_code] + p.aliases)
        lines.append(f"- {p.default_code} ({codes}): {p.name}")
    prompt = (
        f"A purchase-order line says: SKU={query!r} description={description!r}.\n"
        f"Choose the single best-matching product code from this catalog (or NONE):\n"
        f"{chr(10).join(lines[:120])}\n"
        'Respond with ONLY JSON: {"code": "CODE" or "NONE", "confidence": 0.0-1.0}'
    )
    try:
        data = llm.extract_json("You are an exact-match selector. Answer with JSON only.", prompt)
    except Exception:
        return ProductMatch(None, 0.0, "none")
    code = str(data.get("code") or "").strip().upper()
    conf = float(data.get("confidence") or 0)
    if code == "NONE" or conf < 0.6:
        return ProductMatch(None, 0.0, "none")
    for p in products:
        if p.default_code.upper() == code or code in [a.upper() for a in p.aliases]:
            return ProductMatch(p, round(conf * 100, 1), "llm")
    return ProductMatch(None, 0.0, "none")
