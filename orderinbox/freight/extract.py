"""Freight extraction: classification, deterministic parsing, LLM fallback.

Same philosophy as the order parser: deterministic first, model for the rest.
A *structured* rate message (the Section 8 format — "USD 1,925 / 40HQ, POL
SHA, POD VAN, excl. THC ...") is fully parseable with regexes and needs no
model. A *messy* quote or a free-form RFQ falls back to the local model via
the gateway, then to deterministic sanity checks.

Two entry points:
    classify_freight_text(text) -> "rfq" | "quote" | None
    extract_freight(text, kind, llm, settings) -> (raw_dict, method)
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
from typing import Optional

from ..config import Settings
from ..extract.llm import LLMClient
from . import ontology

log = logging.getLogger("orderinbox.freight.extract")


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------

_RFQ_STRONG = (
    "rfq", "request for quote", "request for rate", "requesting rate",
    "requesting quote", "requesting your rate", "rate request", "quote request",
    "looking for rate", "looking for a rate", "looking for space",
    "your best rate", "best rate for", "how much for", "can you quote",
    "can you offer", "can you quote us", "quote us",
)
_RFQ_WEAK = (
    "space needed", "need space", "cargo ready", "cargo is ready",
    "cargo will be ready", "will be ready", "ready date",
)
_QUOTE_STRONG = (
    "ocean freight", "ocean rate", "rate sheet", "our quote", "our rate is",
    "rates as follows", "quotation", "valid etd", "subject to space",
    "subject to equipment", "subject to vessel", "per 40hc", "per 40hq",
    "per 20gp", "per container",
)
_QUOTE_WEAK = ("o/f", "demurrage", "detention", "free time", "thc")
_MONEY_PER_EQUIP = re.compile(
    r"\$\s?\d[\d,]*\s*/\s*\d{2}[a-z]{0,3}|\b(usd|eur|cad|cny|gbp|jpy)\s?\d[\d,]*\s*/\s*\d{2}[a-z]{0,3}", re.I)


def classify_freight_text(text: str) -> Optional[str]:
    """Cheap heuristic: does this text look like an RFQ, a quote, or neither?

    Returns "rfq", "quote", or None. Mirrors `order_like_text` for the order
    domain. Rate statements (money/container, "valid ETD", "subject to ...")
    outweigh request phrasing; request phrasing ("need 2x 40HC", "can you
    quote") outweighs weak quote vocabulary (an RFQ can ask for demurrage
    free time without being a quote).
    """
    t = (text or "").lower()
    if len(t.strip()) < 24:
        return None
    rfq_strong = sum(1 for s in _RFQ_STRONG if s in t)
    rfq_strong += 1 if re.search(r"\bneed\s+\d+\s*[x×]", t) else 0
    rfq_strong += 1 if re.search(r"\bready\s+[A-Za-z]{3,4}\.?\s+\d{1,2}\b", t) else 0
    rfq_weak = sum(1 for s in _RFQ_WEAK if s in t)
    quote_strong = sum(1 for s in _QUOTE_STRONG if s in t)
    quote_weak = sum(1 for s in _QUOTE_WEAK if s in t)
    money = 2 if _MONEY_PER_EQUIP.search(t) else 0

    score_rfq = rfq_strong * 2 + rfq_weak
    score_quote = (quote_strong + 1) * 2 + money + quote_weak
    # a stated rate is decisive: quotes say what it costs, RFQs ask
    if money or quote_strong >= 2:
        if score_quote >= 4 and score_quote > score_rfq:
            return "quote"
    if score_rfq >= 2 and score_rfq > score_quote:
        return "rfq"
    # tie / no dominant side: an RFQ is the safer guess — a quote needs a
    # stated rate to be one, and a mis-routed RFQ fails loudly (no lane),
    # which is the review queue doing its job
    if score_rfq >= 4:
        return "rfq"
    if score_quote >= 4:
        return "quote"
    return None


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------

_MONTHS = {m.lower(): i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}

_EQUIP_RE = re.compile(r"\b(20gp|40gp|40hc|40hq|45hc|20rf|40rf|lcl)\b", re.I)


def _now_year() -> int:
    return _dt.date.today().year


def _parse_ready_date(text: str) -> str:
    """Find a cargo-ready / ETD date. Handles '2026-10-12', 'Oct 12', '12 Oct',
    '12 OCT' (year defaults to current)."""
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if m:
        return m.group(0)
    # "Oct 12" or "12 Oct"
    for m in re.finditer(r"\b([A-Za-z]{3,4})\.?\s+(\d{1,2})\b", text):
        mon = _MONTHS.get(m.group(1).lower())
        if mon and 1 <= int(m.group(2)) <= 31:
            return f"{_now_year()}-{mon:02d}-{int(m.group(2)):02d}"
    for m in re.finditer(r"\b(\d{1,2})\s+([A-Za-z]{3,4})\.?\b", text):
        mon = _MONTHS.get(m.group(2).lower())
        if mon and 1 <= int(m.group(1)) <= 31:
            return f"{_now_year()}-{mon:02d}-{int(m.group(1)):02d}"
    return ""


def _parse_count_equipment(text: str) -> tuple[int, str]:
    """Find '2x 40HC' / '2 x 40hc' / '40HC' -> (count, equipment)."""
    equip = ""
    m = _EQUIP_RE.search(text)
    if m:
        equip = ontology.normalize_equipment(m.group(1)) or m.group(1).upper()
    count = 1
    m = re.search(r"\b(\d+)\s*[x×]\s*(\d{2}[a-z]{0,3})", text, re.I)
    if m:
        count = int(m.group(1))
    return count, equip


def _parse_free_time(text: str) -> tuple[int, int]:
    """Find demurrage/detention free days. Handles the quote format
    '14 DEM + 7 DET' and RFQ phrasing '14 free days demurrage + 7 detention',
    '14 days demurrage, 7 days detention', or a bare '14 free days'
    (applied to both)."""
    t = text.lower()
    dem = det = 0
    # explicit pair: "14 dem + 7 det" / "14 demurrage + 7 detention"
    m = re.search(r"(\d+)\s*(?:days?\s+)?(?:free\s+days?\s+)?dem(urrage)?\s*\+?\s*(\d+)\s*(?:days?\s+)?(?:free\s+days?\s+)?det(ention)?", t)
    if m:
        return int(m.group(1)), int(m.group(3))
    m = re.search(r"(\d+)\s*(?:days?\s+)?(?:free\s+days?\s+)?dem(urrage)?\b", t)
    if m:
        dem = int(m.group(1))
    m = re.search(r"(\d+)\s*(?:days?\s+)?(?:free\s+days?\s+)?det(ention)?\b", t)
    if m:
        det = int(m.group(1))
    if not dem and not det:
        m = re.search(r"(\d+)\s*(?:free\s+)?days?\b", t)
        if m:
            dem = det = int(m.group(1))
    return dem, det


# --------------------------------------------------------------------------
# deterministic QUOTE extraction (Section 8 format)
# --------------------------------------------------------------------------

def _parse_money_port(text: str) -> tuple[str, str]:
    """Resolve POL/POD given as 'POL: SHA' 'POD: VAN' or full names."""
    origin = dest = ""
    m = re.search(r"\bPOL\s*:?\s*([A-Za-z ]{1,20})", text)
    if m:
        origin = m.group(1).strip()
    m = re.search(r"\bPOD\s*:?\s*([A-Za-z ]{1,20})", text)
    if m:
        dest = m.group(1).strip()
    return origin, dest


def extract_quote_from_text(text: str) -> Optional[dict]:
    """Parse a structured rate message (Section 8 format) into the raw dict
    shape that `normalize_quote` accepts. Returns None when it is not a
    recognizable rate (caller falls back to the model)."""
    t = text or ""
    tl = t.lower()
    if not re.search(r"\b(usd|eur|cad|cny|gbp|jpy)\s?\$?\s?\d[\d,]*\s*/\s*\d{2}[a-z]{0,3}", tl):
        # no "money / container" pattern — not a structured rate
        return None
    out: dict = {}

    # base freight: "USD 1,925 / 40HQ" (optionally prefixed O/F, ocean)
    m = re.search(r"\b(usd|eur|cad|cny|gbp|jpy)\s?\$?\s?(\d[\d,]*)\s*/\s*(\d{2}[a-z]{0,3})", tl)
    if m:
        out["base_freight"] = {
            "amount": float(m.group(2).replace(",", "")),
            "currency": m.group(1).upper(),
        }
        out["equipment"] = ontology.normalize_equipment(m.group(3)) or m.group(3).upper()

    origin, dest = _parse_money_port(t)
    # if POL/POD absent, try port names in the lane
    if not origin:
        m = re.search(r"\bfrom\s+([A-Za-z ]{3,20})\b", tl)
        if m:
            origin = m.group(1)
    if not dest:
        m = re.search(r"\bto\s+([A-Za-z ]{3,20})\b", tl)
        if m:
            dest = m.group(1)
    if origin:
        out["origin_port"] = origin
    if dest:
        out["destination_port"] = dest

    # included / excluded
    m = re.search(r"\bincl\.?\s+([A-Za-z /,]+?)(?:\n|$|\s+excl)", tl)
    if m:
        out["included"] = [s.strip() for s in re.split(r"[/,]+", m.group(1)) if s.strip()]
    m = re.search(r"\bexcl\.?\s+([A-Za-z /,]+?)(?:\n|$)", tl)
    if m:
        out["excluded"] = [s.strip() for s in re.split(r"[/,]+", m.group(1)) if s.strip()]
    # "rail VAN-TOR excluded" style
    for m in re.finditer(r"\b([a-z][a-z\- ]{2,20})\s+excluded\b", tl):
        term = m.group(1).strip()
        out.setdefault("excluded", []).append(term)

    # explicit charges: "DTHC CAD 735", "DOC USD 50", "THC CNY 1200"
    charges = []
    for m in re.finditer(r"\b([A-Za-z]{2,8})\s+(usd|eur|cad|cny|gbp|jpy)\s+(\d[\d,]*(?:\.\d+)?)\b", tl):
        charges.append({"type": m.group(1), "currency": m.group(2).upper(),
                        "amount": float(m.group(3).replace(",", ""))})
    if charges:
        out["charges"] = charges

    # free time
    dem, det = _parse_free_time(t)
    if dem or det:
        out["free_time"] = {"demurrage_days": dem, "detention_days": det}

    # conditional surcharges: "PSS subject to vessel"
    cond = []
    for m in re.finditer(r"\b(pss|gri|baf|caf)\b\s+subject to\b", tl):
        if m.group(1).upper() not in cond:
            cond.append(m.group(1).upper())
    if cond:
        out["conditional_charges"] = cond

    # conditions — skip "subject to vessel" when it belongs to a conditional
    # surcharge line (e.g. "PSS subject to vessel")
    conds = []
    for m in re.finditer(r"\bsubject to\s+([a-z /,]+?)(?:\n|$|\.|;)", tl):
        prefix = tl[max(0, m.start() - 24):m.start()].rstrip()
        if re.search(r"\b(pss|gri|baf|caf)\s*$", prefix):
            continue
        conds.append(m.group(1).strip())
    if conds:
        out["conditions"] = conds

    # validity window
    m = re.search(r"\bvalid\s+(?:etd\s+)?(\d{1,2})\s*[–-]\s*(\d{1,2})\s+([A-Za-z]{3,4})", tl)
    if m:
        out["valid_from"] = f"{_now_year()}-{_MONTHS.get(m.group(3).lower(), 1):02d}-{int(m.group(1)):02d}"
        out["valid_to"] = f"{_now_year()}-{_MONTHS.get(m.group(3).lower(), 1):02d}-{int(m.group(2)):02d}"

    return out or None


# --------------------------------------------------------------------------
# deterministic RFQ extraction
# --------------------------------------------------------------------------

_DG_RE = re.compile(r"(?<!non-)(?<!non )\b(dg|imdg|dangerous goods|class \d)\b", re.I)


def _parse_budget(text: str) -> tuple[float, str]:
    """Find a customer budget / target price: 'budget of $2,400',
    'our target is USD 2,400 per container', 'expecting around 2,400 USD'."""
    t = (text or "").lower()
    m = re.search(
        r"\b(budget|target (?:price|cost|rate)|expect(?:ing)?(?:\s+\w+){0,2}|"
        r"approximately|around|up to)\b[^$\d]{0,30}?\$?\s?(\d[\d,]*(?:\.\d+)?)\s*"
        r"(usd|eur|cad|cny|gbp)?", t)
    if m:
        amount = float(m.group(2).replace(",", ""))
        if amount >= 100:   # filter out dates/quantities that sneak in
            return amount, (m.group(3) or "USD").upper()
    return 0.0, ""


def _clean_port_ref(value: str) -> str:
    """Strip direction keywords a lane capture may have swallowed.

    "from Shanghai" -> "Shanghai"; "HC from Shanghai" -> "Shanghai" (the port
    is the segment after the last 'from'/'via')."""
    s = (value or "").strip()
    s = re.split(r"\s+(?:from|via)\s+", s)[-1]
    s = re.sub(r"^(?:from|to|via)\s+", "", s, flags=re.I)
    return s.strip()


def extract_rfq_from_text(text: str) -> Optional[dict]:
    """Parse a customer RFQ request into the raw RFQRequest dict. Returns
    None when it does not look like an RFQ at all."""
    t = text or ""
    tl = t.lower()
    # must reference a container or a lane to count as an RFQ
    if not (_EQUIP_RE.search(t) or re.search(r"\b(from|to)\s+[a-z]{3,}", tl)):
        return None
    out: dict = {}
    count, equip = _parse_count_equipment(t)
    if equip:
        out["equipment"] = equip
        out["container_count"] = count

    # lane: "Shanghai -> Toronto", "Shanghai to Vancouver", "SHA to VAN"
    m = re.search(r"\b([A-Za-z][A-Za-z ]{2,20})\s*(?:->|to)\s+([A-Za-z][A-Za-z ]{2,20})", t)
    if m:
        o_raw = _clean_port_ref(m.group(1))
        d_raw = _clean_port_ref(m.group(2))
        o = ontology.lookup_port(o_raw)
        d = ontology.lookup_port(d_raw)
        if o or d:
            out["origin_port"] = o.un_locode if o else o_raw.upper()
            out["destination_port"] = d.un_locode if d else d_raw.upper()
    else:
        # separate "from X" / "to Y"
        mo = re.search(r"\bfrom\s+([A-Za-z][A-Za-z ]{2,20})\b", t)
        md = re.search(r"\bto\s+([A-Za-z][A-Za-z ]{2,20})\b", t)
        if mo:
            p = ontology.lookup_port(mo.group(1))
            out["origin_port"] = p.un_locode if p else mo.group(1)
        if md:
            p = ontology.lookup_port(md.group(1))
            out["destination_port"] = p.un_locode if p else md.group(1)

    rd = _parse_ready_date(t)
    if rd:
        out["ready_date"] = rd
    dem, det = _parse_free_time(t)
    if dem:
        out["free_demurrage_days"] = dem
    if det:
        out["free_detention_days"] = det
    out["dangerous_goods"] = bool(_DG_RE.search(t))
    # cargo description, e.g. "Cargo: non-DG electronics" — reject readiness
    # statements ("cargo ready Oct 12") and dates
    m = re.search(r"\bcargo\s*(?:is|:)?\s+([A-Za-z][A-Za-z0-9 ,\-]{2,60})", tl)
    if m:
        cand = m.group(1).strip().rstrip(".,")
        first = cand.split()[0].lower()
        if first not in ("ready", "etd", "will", "is", "expected", "scheduled",
                         "arrives", "leaving") and first not in _MONTHS:
            out["cargo"] = cand
    budget, cur = _parse_budget(t)
    if budget:
        out["budget_total"] = budget
        out["budget_currency"] = cur
    return out or None


# --------------------------------------------------------------------------
# LLM fallback (via the gateway, local-first)
# --------------------------------------------------------------------------

RFQ_PROMPT = (
    "Extract the customer's freight request for quote (RFQ) from the text below.\n"
    "Return ONLY JSON: {\"origin_port\": str|null, \"destination_port\": str|null, "
    "\"equipment\": \"40HC\"-style|null, \"container_count\": int, "
    "\"ready_date\": \"YYYY-MM-DD\"|null, \"free_demurrage_days\": int, "
    "\"free_detention_days\": int, \"cargo\": str|null, \"dangerous_goods\": bool, "
    "\"budget_total\": number|null, \"budget_currency\": \"USD\"|null, "
    "\"notes\": str|null, \"confidence\": 0.0-1.0}\n"
    "Use port city names or UN/LOCODE. budget_total is the customer's stated "
    "budget or target price if any. Do not invent data; use null when absent.\n\n"
    "TEXT:\n"
)

QUOTE_PROMPT = (
    "Interpret the freight rate/quote below and normalize it. Return ONLY JSON: "
    "{\"origin_port\": str|null, \"destination_port\": str|null, \"equipment\": "
    "\"40HC\"-style|null, \"base_freight\": {\"amount\": 0, \"currency\": \"USD\"}, "
    "\"included\": [charge codes], \"excluded\": [charge codes], "
    "\"charges\": [{\"type\": \"DTHC\", \"amount\": 0, \"currency\": \"CAD\"}], "
    "\"free_time\": {\"demurrage_days\": 0, \"detention_days\": 0}, "
    "\"conditional_charges\": [\"PSS\"], \"conditions\": [str], "
    "\"valid_from\": \"YYYY-MM-DD\"|null, \"valid_to\": \"YYYY-MM-DD\"|null, "
    "\"counterparty\": str|null, \"confidence\": 0.0-1.0}\n"
    "Map charge terms to canonical freight concepts where you can "
    "(OCEAN_FREIGHT, ORIGIN_THC, DESTINATION_THC, DOCUMENTATION_FEE, DELIVERY, PSS, ...).\n\n"
    "QUOTE:\n"
)


def _llm_extract(text: str, kind: str, llm: LLMClient, settings: Settings) -> Optional[dict]:
    prompt = (RFQ_PROMPT if kind == "rfq" else QUOTE_PROMPT) + text[:60000]
    try:
        return llm.extract_json(
            "You are a freight data extractor. Answer with JSON only.",
            prompt, task="freight_" + kind, confidentiality="high")
    except Exception as exc:
        log.warning("freight %s LLM extraction failed: %s", kind, exc)
        return None


def extract_freight(text: str, kind: str, llm: Optional[LLMClient],
                    settings: Settings) -> tuple[dict, str]:
    """Extract a freight object (rfq|quote) from text.

    Deterministic first; the model fills in when the structured parse found
    nothing (or, for quotes, when a structured parse is uncertain). Returns
    (raw_dict, method) where method is 'deterministic' | 'llm' | 'hybrid' |
    'none'.
    """
    if kind == "quote":
        det = extract_quote_from_text(text)
        if det and det.get("base_freight"):
            # deterministic found the core of the rate; let the model enrich
            # free-text fields only if available (cheap header-style call).
            if llm is not None and llm.available():
                llmres = _llm_extract(text, "quote", llm, settings)
                if llmres:
                    merged = {**llmres, **det}   # deterministic wins on overlap
                    return merged, "hybrid"
            return det, "deterministic"
        if llm is not None and llm.available():
            llmres = _llm_extract(text, "quote", llm, settings)
            if llmres:
                return llmres, "llm"
        if det:
            return det, "deterministic"
        return {}, "none"

    # rfq
    det = extract_rfq_from_text(text)
    if det and (det.get("equipment") or det.get("origin_port")):
        if llm is not None and llm.available():
            llmres = _llm_extract(text, "rfq", llm, settings)
            if llmres:
                merged = {**llmres, **{k: v for k, v in det.items() if v}}
                return merged, "hybrid"
        return det, "deterministic"
    if llm is not None and llm.available():
        llmres = _llm_extract(text, "rfq", llm, settings)
        if llmres:
            return llmres, "llm"
    if det:
        return det, "deterministic"
    return {}, "none"
