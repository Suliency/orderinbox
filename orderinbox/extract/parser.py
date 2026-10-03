"""Order extraction orchestrator.

Hybrid strategy (deterministic first, LLM for the rest):
  1. Tables from spreadsheet/PDF formats are parsed deterministically.
  2. The LLM extracts header entities (customer, PO number, date, currency,
     terms, ship-to) and sanity-checks the result.
  3. If no deterministic lines were found (e.g. a scanned/text PDF), the LLM
     extracts the line items from raw text.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from ..config import Settings
from ..models import Extraction, MatchMethod, OrderLine
from .documents import DocumentContent, order_like_text
from .llm import HEADER_EXTRACTION_PROMPT, LLMClient, ORDER_EXTRACTION_PROMPT

log = logging.getLogger("orderinbox.parser")

# Deterministic PO-number fallback (duplicates detection must not rely on the LLM).
# (pattern, require_digit)
_PO_PATTERNS = [
    (re.compile(r"\bPO\s*(?:No\.?|Number|P/O|#|:)?\s*[:.]?\s*([A-Za-z0-9][A-Za-z0-9\-/]{1,15})\b"), True),
    (re.compile(r"\bP/O\s*:?\s*([A-Za-z0-9][\w\-/]{1,15})\b"), True),
    (re.compile(r"\bOrder\s*(?:No|number|#)?\s*:?\s*(\d{2,4}-\d{2,})\b", re.I), True),
    (re.compile(r"\bOrder\s*(?:No|number|#)?\s*:?\s*([A-Za-z0-9]{2,}[-/][A-Za-z0-9\-/]{1,15})\b", re.I), False),
    (re.compile(r"\bOrder\s*(?:No|number|#)?\s*:?\s*(\d{2,6})\b", re.I), True),
]


def find_po_number(text: str) -> str:
    for pat, require_digit in _PO_PATTERNS:
        for m in pat.finditer(text or ""):
            cand = m.group(1)
            if require_digit and not any(ch.isdigit() for ch in cand):
                continue
            if any(ch.isdigit() for ch in cand) or len(cand) >= 4:
                return cand
    return ""

# --------------------------------------------------------------------------
# deterministic table parsing
# --------------------------------------------------------------------------

SKU_MARKERS = ("sku", "part no", "part number", "part", "item no", "item number",
               "item", "code", "product code", "product no", "mpn", "item code", "p/n", "pn",
               "itemno", "itemnumber", "partnumber", "productcode", "productno", "skuno")
DESC_MARKERS = ("description", "desc", "item description", "product description",
                "name", "product name", "item name", "product",
                "itemdescription", "productdescription", "itemname")
QTY_MARKERS = ("quantity", "qty", "q'ty", "qnty", "units", "unit", "amt", "amount", "no.", "no", "qtyonly")
PRICE_MARKERS = ("unit price", "price each", "price/each", "unit cost", "price", "rate",
                 "each", "cost", "unit price (", "unitprice", "priceeach", "unitcost")
TOTAL_MARKERS = ("total", "extended", "line total", "amount", "amount due", "linetotal")


def _norm(cell: str) -> str:
    return re.sub(r"\s+", " ", cell or "").strip().lower()


def _header_map(header_row: list[str]) -> dict[str, list[int]]:
    """Map semantic columns to indices.

    Three passes so exact matches always win: pass 1 matches cells exactly
    equal to a marker, pass 2 prefix matches, pass 3 contains. This is what
    keeps "Qty" from being stolen by the "unit" marker inside "Unit Price".
    """
    order = [("sku", SKU_MARKERS), ("desc", DESC_MARKERS),
             ("qty", QTY_MARKERS), ("price", PRICE_MARKERS), ("total", TOTAL_MARKERS)]
    mapping: dict[str, list[int]] = {}
    taken: set[int] = set()

    for pass_kind in ("exact", "prefix", "contains"):
        for key, markers in order:
            for m in sorted(markers, key=len, reverse=True):
                for i, cell in enumerate(header_row):
                    if i in taken:
                        continue
                    n = _norm(cell)
                    if not n:
                        continue
                    if pass_kind == "exact" and n == m:
                        mapping.setdefault(key, []).append(i)
                        taken.add(i)
                        break
                    if pass_kind == "prefix" and len(m) >= 3 and n.startswith(m):
                        mapping.setdefault(key, []).append(i)
                        taken.add(i)
                        break
                    if pass_kind == "contains" and len(m) >= 4 and m in n and len(n) <= len(m) + 4:
                        mapping.setdefault(key, []).append(i)
                        taken.add(i)
                        break
    return mapping


_NUM_RE = re.compile(r"^\s*\(?-?\d[\d,]*\.?\d*\)?\s*$")
_QTY_UNIT_RE = re.compile(r"^\s*(\d[\d,]*\.?\d*)\s*([a-zA-Z/][a-zA-Z/\- ]{0,12})?\s*$")


def _parse_number(raw: str) -> Optional[float]:
    s = (raw or "").replace("$", "").replace(",", "").replace(" ", "").strip()
    if not s or s in ("-", "--", "—"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    if re.match(r"^-?\d+(\.\d+)?$", s):
        v = float(s)
        return -v if neg else v
    return None


def _parse_qty(raw: str) -> tuple[Optional[float], str]:
    m = _QTY_UNIT_RE.match(raw or "")
    if not m:
        n = _parse_number(raw)
        return (n, "") if n is not None else (None, (raw or "").strip())
    num = _parse_number(m.group(1))
    unit = (m.group(2) or "").strip()
    unit = unit.upper() if unit in ("ea", "each", "cs", "case", "box", "ct", "pcs", "pc", "roll", "pack") else (m.group(2) or "").strip()
    return num, unit


def extract_lines_from_table(rows: list[list[str]]) -> tuple[list[OrderLine], bool]:
    """Return (lines, header_found)."""
    if not rows:
        return [], False
    # find header row: the row maximizing semantic-column hits
    best_i, best_map = -1, {}
    for i, row in enumerate(rows[:40]):
        m = _header_map(row)
        hits = len(m)
        if hits >= 2 and (hits > len(best_map) or (hits == len(best_map) and "qty" in m and "qty" not in best_map)):
            best_i, best_map = i, m
    if best_i < 0 or not best_map.get("qty"):
        return [], False

    sku_cols = best_map.get("sku")
    desc_cols = best_map.get("desc")
    qty_cols = best_map["qty"]
    price_cols = best_map.get("price")
    total_cols = best_map.get("total")

    lines: list[OrderLine] = []
    for row in rows[best_i + 1:]:
        def pick(cols: Optional[list[int]]) -> str:
            if not cols:
                return ""
            vals = [(row[c] or "").strip() for c in cols if c < len(row)]
            return next((v for v in vals if v), "")

        qty_raw = pick(qty_cols)
        if not qty_raw:
            # maybe a separator row; keep scanning
            continue
        q, unit = _parse_qty(qty_raw)
        price_raw = pick(price_cols)
        price = _parse_number(price_raw)
        if q is None and price is None:
            # non-data row (totals etc.)
            if _norm(pick(desc_cols) or pick(sku_cols)).startswith("total"):
                break
            continue
        sku = pick(sku_cols)
        desc = pick(desc_cols)
        if not sku and not desc:
            continue
        line_total = None
        if total_cols:
            line_total = _parse_number(pick(total_cols))
        lines.append(OrderLine(
            line_no=len(lines) + 1,
            sku_as_written=sku,
            description=desc,
            quantity=q,
            quantity_as_written=qty_raw,
            unit=unit,
            unit_price=price,
            line_total=line_total,
        ))
    return lines, True


# --------------------------------------------------------------------------
# text-line parsing (for PDFs/text without table structure)
# --------------------------------------------------------------------------

_ROW_PRICE_RE = re.compile(r"^\$?\(?\d[\d,]*\.?\d*\)?$")
_ROW_QTY_RE = re.compile(r"^\d+(?:\.\d+)?(?:\s?[a-zA-Z/]{1,4})?$")
_HEADER_RE = re.compile(r"\b(sku|part|item)\b.*\b(qty|quantity)\b.*\b(price|cost)\b", re.I)


def _looks_like_money(s: str) -> bool:
    """A money amount carries a $ sign or a decimal part (prices in real
    documents virtually always do; bare integers are more likely quantities)."""
    return "$" in s or re.search(r"\.\d", s) is not None


def _parse_row(tokens: list[str]):
    """Parse one whitespace-column order line. Returns (rest, qty, price, total)
    or None. Tries 3-column (qty, price, total) first, then 2-column (qty, price).
    The price cell must look like money to avoid confusing a second quantity
    with a price (e.g. 'Hex Bolt Box 50 10 $4.10').
    """
    if len(tokens) >= 4:
        q, p, t = tokens[-3], tokens[-2], tokens[-1]
        if (_ROW_QTY_RE.match(q) and not q.startswith("$")
                and _ROW_PRICE_RE.match(p) and _looks_like_money(p)
                and _ROW_PRICE_RE.match(t)):
            return tokens[:-3], q, p, t
    if len(tokens) >= 3:
        q, p = tokens[-2], tokens[-1]
        if (_ROW_QTY_RE.match(q) and not q.startswith("$")
                and _ROW_PRICE_RE.match(p) and _looks_like_money(p)):
            return tokens[:-2], q, p, None
    return None


def extract_lines_from_text(text: str) -> tuple[list[OrderLine], bool]:
    """Parse whitespace-column lines like:
        SKU Description Qty Unit Price Total
        PLM-101 Copper Pipe 1/2 in x 10 ft 12 $29.32 $351.84
    """
    lines_out: list[OrderLine] = []
    header_found = False
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if not header_found:
            if _HEADER_RE.search(line):
                header_found = True
            continue
        if re.match(r"^\s*total\b", line, re.I):
            break
        parsed = _parse_row(line.split())
        if parsed is None:
            continue
        rest, q_raw, p_raw, t_raw = parsed
        try:
            qty = float(q_raw)
            price = float(p_raw.replace("$", "").replace(",", ""))
        except ValueError:
            continue
        total = None
        if t_raw:
            try:
                total = float(t_raw.replace("$", "").replace(",", ""))
            except ValueError:
                total = None
        sku = rest[0] if rest and _looks_like_code(rest[0]) else ""
        desc = " ".join(rest[1:]) if sku else " ".join(rest)
        lines_out.append(OrderLine(
            line_no=len(lines_out) + 1,
            sku_as_written=sku,
            description=desc,
            quantity=qty,
            quantity_as_written=q_raw,
            unit_price=price,
            line_total=total if total is not None else round(qty * price, 2),
        ))
    return lines_out, header_found


def _looks_like_code(tok: str) -> bool:
    """Heuristic: does a token look like a product code (has digits or is
    an all-caps multi-char token)?"""
    if not tok:
        return False
    if any(c.isdigit() for c in tok):
        return True
    return len(tok) >= 2 and tok.isupper()


# --------------------------------------------------------------------------
# LLM-based fallback extraction
# --------------------------------------------------------------------------

def _llm_to_extraction(data: dict) -> Extraction:
    lines: list[OrderLine] = []
    for i, raw in enumerate(data.get("lines") or []):
        if not isinstance(raw, dict):
            continue
        qty = raw.get("quantity")
        try:
            qty = float(qty) if qty is not None else None
        except (TypeError, ValueError):
            qty = None
        price = raw.get("unit_price")
        try:
            price = float(price) if price is not None else None
        except (TypeError, ValueError):
            price = None
        lines.append(OrderLine(
            line_no=i + 1,
            sku_as_written=str(raw.get("sku") or ""),
            description=str(raw.get("description") or ""),
            quantity=qty,
            quantity_as_written=str(raw.get("quantity") or ""),
            unit=str(raw.get("unit") or ""),
            unit_price=price,
            remarks=str(raw.get("remarks") or ""),
            line_total=qty * price if qty and price else None,
        ))
    return Extraction(
        customer_name_as_written=str(data.get("customer_name") or ""),
        po_number=str(data.get("po_number") or ""),
        po_date=str(data.get("po_date") or ""),
        currency=str(data.get("currency") or ""),
        payment_terms=str(data.get("payment_terms") or ""),
        ship_to=str(data.get("ship_to") or ""),
        notes=str(data.get("notes") or ""),
        lines=lines,
        confidence=0.0,
        extract_method="llm",
    )


# --------------------------------------------------------------------------
# orchestrator
# --------------------------------------------------------------------------

def extract_order(docs: list[DocumentContent], llm: Optional[LLMClient],
                  settings: Settings, subject: str = "") -> Optional[Extraction]:
    """Combine all documents of one message into a single Extraction.

    Priority: spreadsheet/PDF tables > LLM text extraction > email body.
    Returns None when the message doesn't look like an order.
    """
    # 1) deterministic lines: structured tables first, then whitespace-column
    #    text lines (common in simple/scanned PDFs)
    det_lines: list[OrderLine] = []
    header_found = False
    for d in docs:
        for tbl in d.tables:
            lines, found = extract_lines_from_table(tbl)
            if found and len(lines) > len(det_lines):
                det_lines, header_found = lines, True
    if not det_lines:
        for d in docs:
            lines, found = extract_lines_from_text(d.text)
            if found and len(lines) > len(det_lines):
                det_lines, header_found = lines, True
    if not det_lines and header_found:
        # table with a recognizable header but no parseable rows — let the LLM
        # decide whether it's an order at all
        det_lines = []

    # 2) is this even an order? The subject is included too — POs are usually
    #    referenced in the subject line.
    primary = next((d for d in docs if d.tables), docs[0]) if docs else None
    combined_text = "\n\n".join([subject] + [d.text for d in docs if d.text])
    if not combined_text.strip():
        return None
    looks_like_order = order_like_text(combined_text) or bool(det_lines)
    if not looks_like_order:
        return None

    if det_lines:
        extraction = Extraction(
            lines=det_lines,
            extract_method="deterministic",
            raw_text_chars=len(combined_text),
            source=primary.subject or primary.path if primary else "",
        )
    else:
        extraction = None

    # 3) LLM pass: header entities only when the parser found the lines,
    #    header + lines otherwise
    if llm is not None and llm.available():
        try:
            prompt = HEADER_EXTRACTION_PROMPT if det_lines else ORDER_EXTRACTION_PROMPT
            data = llm.extract_json("You are an order-entry data extractor.",
                                    prompt + combined_text[:60000])
            if data.get("is_order") is False and not det_lines:
                return None
            llm_ex = _llm_to_extraction(data)
            if extraction is None:
                extraction = llm_ex
            else:
                # keep deterministic lines; take header fields from LLM
                extraction.customer_name_as_written = llm_ex.customer_name_as_written or extraction.customer_name_as_written
                extraction.po_number = llm_ex.po_number or extraction.po_number
                extraction.po_date = llm_ex.po_date or extraction.po_date
                extraction.currency = llm_ex.currency or extraction.currency
                extraction.payment_terms = llm_ex.payment_terms or extraction.payment_terms
                extraction.ship_to = llm_ex.ship_to or extraction.ship_to
                extraction.notes = llm_ex.notes or extraction.notes
                extraction.extract_method = "hybrid"
        except Exception as exc:
            log.warning("LLM extraction failed (%s) — using deterministic result", exc)

    if extraction is None or not extraction.lines:
        return None

    # deterministic PO-number fallback (needed for duplicate detection)
    if not extraction.po_number:
        extraction.po_number = find_po_number(combined_text)

    # 4) confidence
    confidence = 0.5
    if header_found:
        confidence += 0.25
    if extraction.extract_method == "hybrid":
        confidence += 0.2
    matched_sku_ratio = sum(1 for l in extraction.lines if l.sku_as_written) / max(1, len(extraction.lines))
    confidence += 0.15 * matched_sku_ratio
    extraction.confidence = round(min(1.0, confidence), 3)
    for i, line in enumerate(extraction.lines):
        line.line_no = i + 1
        if line.line_total is None and line.quantity is not None and line.unit_price is not None:
            line.line_total = round(line.quantity * line.unit_price, 2)
    return extraction
