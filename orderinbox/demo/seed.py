"""Demo dataset builder: realistic reseller orders in the formats buyers actually
send — XLSX, PDF (reportlab), CSV, and plain inline email text — wired to the
built-in seed catalog so matching/validation behave like production.

What the demo deliberately exercises:
  1. clean Excel order, exact SKUs, valid pack multiples        → READY
  2. formal PDF purchase order with header block               → READY (hybrid)
  3. CSV with one unknown SKU + one fuzzy-only match           → EXCEPTION
  4. inline email body order, no attachment                    → READY/EXCEPTION (LLM text path)
  5. duplicate PO (same customer + PO number as #1)            → EXCEPTION (DUPLICATE_PO)
  6. pack-size violation + price deviation                     → EXCEPTION
  7. non-order email (safety meeting reminder)                  → REJECTED
  8. customer RFQ (40HC Shanghai→Toronto, budget stated)        → READY (freight)
  9. agent quote #1 — all-in cheapest, margin above threshold  → READY (freight, recommended)
 10. agent quote #2 — cheap base but thin all-in margin        → EXCEPTION (freight)
 11. agent quote #3 — all-in above the customer budget         → EXCEPTION (freight)
"""
from __future__ import annotations

import email.utils
import hashlib
import io
import json
import random
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

from ..config import Settings

SEED = Path(__file__).resolve().parent / "seed.json"
random.seed(7)


def _load_seed() -> dict:
    return json.loads(SEED.read_text())


def _by_code(seed: dict, code: str):
    return next((p for p in seed["products"] if p["default_code"] == code), None)


def _price(seed: dict, code: str, dev: float = 0.0) -> float:
    p = _by_code(seed, code)
    base = p["list_price"] if p else 5.0
    return round(base * (1 + dev), 2)


def _pack(seed: dict, code: str) -> int:
    p = _by_code(seed, code)
    return (p["pack_qty"] if p else 1) or 1


def _min_qty(seed: dict, code: str) -> float:
    p = _by_code(seed, code)
    return float(p["min_qty"]) if p else 0.0


def _valid_qty(seed: dict, code: str, multiple: int) -> int:
    """A quantity that is a multiple of the pack size AND >= the min order qty."""
    pack = _pack(seed, code)
    minimum = _min_qty(seed, code)
    base = pack * multiple
    if base < minimum:
        # round up to the next pack multiple that satisfies the minimum
        import math
        base = int(math.ceil(minimum / pack)) * pack
    return base


# --------------------------------------------------------------------------
# attachment builders
# --------------------------------------------------------------------------

def _xlsx_bytes(lines: list[tuple[str, float, float]]) -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Order"
    ws.append(["Line", "SKU", "Description", "Qty", "Unit Price", "Total"])
    for i, (sku, qty, price) in enumerate(lines, 1):
        ws.append([i, sku, f"item {sku}", qty, price, round(qty * price, 2)])
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


def _csv_bytes(lines: list[tuple[str, float, float]]) -> bytes:
    out = ["SKU,Description,Qty,UnitPrice\n"]
    for sku, qty, price in lines:
        out.append(f"{sku},item {sku},{qty},{price:.2f}\n")
    return "".join(out).encode()


def _pdf_bytes(company: str, po_number: str, po_date: str, terms: str, ship_to: str,
               lines: list[tuple[str, str, float, float]], currency: str = "USD") -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    w, h = letter
    y = h - 0.7 * inch
    c.setFont("Helvetica-Bold", 16)
    c.drawString(0.75 * inch, y, "PURCHASE ORDER")
    y -= 0.32 * inch
    c.setFont("Helvetica", 10)
    c.drawString(0.75 * inch, y, f"Buyer:  {company}")
    y -= 0.22 * inch
    c.drawString(0.75 * inch, y, f"PO No.: {po_number}      Date: {po_date}")
    y -= 0.22 * inch
    c.drawString(0.75 * inch, y, f"Terms: {terms}      Ship to: {ship_to}")
    y -= 0.45 * inch
    # table header
    c.setFont("Helvetica-Bold", 9)
    c.line(0.75 * inch, y + 0.12 * inch, w - 0.75 * inch, y + 0.12 * inch)
    c.drawString(0.75 * inch, y, "SKU")
    c.drawString(2.6 * inch, y, "Description")
    c.drawRightString(5.6 * inch, y, "Qty")
    c.drawRightString(7.0 * inch, y, "Unit Price")
    c.drawRightString(w - 0.75 * inch, y, "Total")
    y -= 0.25 * inch
    c.setFont("Helvetica", 9)
    total = 0.0
    for sku, desc, qty, price in lines:
        c.drawString(0.75 * inch, y, sku)
        c.drawString(2.6 * inch, y, desc)
        c.drawRightString(5.6 * inch, y, str(int(qty)))
        c.drawRightString(7.0 * inch, y, f"${price:.2f}")
        c.drawRightString(w - 0.75 * inch, y, f"${qty * price:.2f}")
        total += qty * price
        y -= 0.22 * inch
        if y < 0.9 * inch:
            c.showPage()
            y = h - 0.7 * inch
    c.line(0.75 * inch, y + 0.15 * inch, w - 0.75 * inch, y + 0.15 * inch)
    c.setFont("Helvetica-Bold", 9)
    c.drawRightString(w - 0.75 * inch, y - 0.05 * inch, f"TOTAL {currency} {total:.2f}")
    y -= 0.4 * inch
    c.setFont("Helvetica", 8)
    c.drawString(0.75 * inch, y, "Please confirm receipt of this order. Payment per terms above.")
    c.showPage()
    c.save()
    return buf.getvalue()


# --------------------------------------------------------------------------
# eml wrapper
# --------------------------------------------------------------------------

def _eml(sender: str, subject: str, date: str, body: str,
         attachments: list[tuple[str, bytes]] | None = None) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "orders@ourcompany.example"
    msg["Subject"] = subject
    msg["Date"] = date
    msg.set_content(body)
    for name, data in attachments or []:
        suffix = name.rsplit(".", 1)[-1].lower()
        if suffix == "pdf":
            ctype = "application/pdf"
        elif suffix in ("xlsx", "xlsm"):
            ctype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif suffix in ("csv", "txt"):
            ctype = "text/plain"
        else:
            ctype = "application/octet-stream"
        maintype, subtype = ctype.split("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return msg.as_bytes()


# --------------------------------------------------------------------------
# dataset
# --------------------------------------------------------------------------

def build_demo(settings: Settings) -> list[Path]:
    seed = _load_seed()
    outdir = settings.spool_dir / "demo"
    outdir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []

    def save(name: str, data: bytes) -> Path:
        p = outdir / name
        p.write_bytes(data)
        created.append(p)
        return p

    # --- 1. clean Excel order -------------------------------------------
    cust = next(p for p in seed["partners"] if p["name"].startswith("Great Lakes"))
    lines1 = [
        ("PLM-103", 24, _price(seed, "PLM-103")),
        ("PLM-104", 12, _price(seed, "PLM-104")),
        ("ELC-201", 4, _price(seed, "ELC-201")),
        ("ELC-220", 12, _price(seed, "ELC-220")),
        ("FST-301", 10, _price(seed, "FST-301")),
        ("SAF-401", 8, _price(seed, "SAF-401")),
        ("SAF-420", 12, _price(seed, "SAF-420")),
    ]
    save("1_great_lakes.eml", _eml(
        cust["email"],
        f"Order 26-0411 — {cust['name']}",
        "Mon, 28 Sep 2026 09:12:44 -0500",
        f"Hi,\n\nPlease process attached order 26-0411, ship to our main dock.\n\nThanks,\n{cust['name']} purchasing",
        [("order_26-0411.xlsx", _xlsx_bytes(lines1))]))

    # --- 2. formal PDF purchase order -----------------------------------
    cust2 = next(p for p in seed["partners"] if p["name"].startswith("Northline"))
    lines2 = []
    pool = ["PLM-101", "PLM-110", "PLM-120", "PLM-130", "ELC-210", "ELC-221", "ELC-230",
            "ELC-240", "FST-310", "FST-320", "SAF-410", "SAF-411", "SAF-421", "PLM-140"]
    for i, code in enumerate(pool):
        qty = _valid_qty(seed, code, random.choice([1, 2, 3, 4]))
        desc = _by_code(seed, code)["name"].split(" — ")[0]
        lines2.append((code, desc, qty, _price(seed, code)))
    pdf2 = _pdf_bytes(cust2["name"], "PO-26-8814", "2026-09-29", "Net 30",
                      "2400 Dock St, Chicago, IL 60622", lines2)
    save("2_northline.eml", _eml(
        cust2["email"],
        "PO-26-8814 — Northline Building Supply",
        "Tue, 29 Sep 2026 14:03:10 -0400",
        "Please find attached our purchase order PO-26-8814.\n\nRegards,\nNorthline Building Supply",
        [("PO-26-8814.pdf", pdf2)]))

    # --- 3. CSV with a bad SKU + a fuzzy-only match ----------------------
    cust3 = next(p for p in seed["partners"] if p["name"].startswith("Metro"))
    lines3 = [
        ("ELC-202", 3, _price(seed, "ELC-202")),
        ("BRK-20X", 6, 3.4),          # unknown SKU → exception
        ("ROM-142", 2, _price(seed, "ELC-201")),  # alias of ELC-201
        ("PANEL-100", 1, _price(seed, "ELC-240")),
    ]
    save("3_metro.eml", _eml(
        cust3["email"],
        "Reorder — Metro Electrical Wholesale",
        "Wed, 30 Sep 2026 08:45:02 -0400",
        "Attached is our weekly reorder, PO 26-1102. Thanks!",
        [("reorder_26-1102.csv", _csv_bytes(lines3))]))

    # --- 4. inline email-body order (no attachment) ----------------------
    cust4 = next(p for p in seed["partners"] if p["name"].startswith("Summit"))
    body4 = (
        "Hi,\n\nPlease process the following order, PO 26-1187, "
        f"ship to our Toronto yard, terms net 30:\n\n"
        "SKU        Qty   Unit Price\n"
        f"PLM-103     12   ${_price(seed, 'PLM-103'):.2f}\n"
        f"PLM-121      6   ${_price(seed, 'PLM-121'):.2f}\n"
        f"ELC-220     10   ${_price(seed, 'ELC-220'):.2f}\n\n"
        "Thanks!\nDana Okafor\nSummit Plumbing & Supply\n"
    )
    save("4_summit.eml", _eml(
        "dana@summitplumbing.com", "Order 26-1187 — Summit Plumbing",
        "Thu, 01 Oct 2026 10:22:31 -0400", body4))

    # --- 5. duplicate PO (same customer + PO number as #1) ---------------
    save("5_duplicate.eml", _eml(
        cust["email"],
        "RE: Order 26-0411 — Great Lakes (resend)",
        "Thu, 01 Oct 2026 16:40:00 -0500",
        "Apologies, resending order 26-0411.",
        [("order_26-0411_resend.xlsx", _xlsx_bytes(lines1))]))

    # --- 6. pack violation + price deviation -----------------------------
    cust6 = next(p for p in seed["partners"] if p["name"].startswith("Prairie"))
    lines6 = [
        ("PLM-110", 5, _price(seed, "PLM-110")),          # pack of 4 → violation
        ("ELC-231", 3, _price(seed, "ELC-231", 0.35)),    # 35% above list → price deviation
        ("FST-321", 2, _price(seed, "FST-321")),
    ]
    save("6_prairie.eml", _eml(
        cust6["email"],
        "PO 26-1201 — Prairie Ridge Construction",
        "Fri, 02 Oct 2026 07:55:44 -0500",
        "Attached PO 26-1201. Need it by end of week, please.",
        [("po_26-1201.xlsx", _xlsx_bytes(lines6))]))

    # --- 7. non-order email ----------------------------------------------
    save("7_reminder.eml", _eml(
        "facilities@ourcompany.example", "Warehouse safety meeting — Friday 3pm",
        "Fri, 02 Oct 2026 08:00:00 -0500",
        "Reminder: the monthly warehouse safety meeting will be held Friday at 3pm "
        "in the break room. Please bring your PPE. Pizza provided."))

    # ======================================================================
    # freight (RateScout) demo — a customer RFQ answered by three agent
    # quotes on the same lane. Quote #1 is the all-in cheapest and clears
    # the margin policy; #2 has a cheap base but a thin all-in margin;
    # #3 prices above the customer's budget.
    # ======================================================================
    cust6 = next(p for p in seed["partners"] if p["name"].startswith("Prairie"))
    # --- 8. customer RFQ --------------------------------------------------
    save("8_rfq_toronto.eml", _eml(
        cust6["email"],
        "RFQ — 2x 40HC Shanghai to Toronto, ready Oct 12",
        "Fri, 02 Oct 2026 09:15:00 -0400",
        "Hi,\n\n"
        "We need 2x 40HC from Shanghai to Toronto, non-DG electronics.\n"
        "Cargo is ready Oct 12.\n"
        "We need 14 free days demurrage + 7 detention.\n"
        "Our budget is around USD 2,400 per container.\n\n"
        "Thanks,\nLogistics — Prairie Ridge Construction"))

    # --- 9. agent quote #1 (all-in cheapest) ------------------------------
    save("9_quote_shanghai_abc.eml", _eml(
        "quotes@shanghaiabc.com",
        "Quote 234 — Shanghai ABC Logistics (40HC SHA→VAN)",
        "Fri, 02 Oct 2026 13:40:00 +0800",
        "Quote 234 — Shanghai ABC Logistics\n"
        "USD 1,925 / 40HQ\n"
        "POL: SHA\nPOD: VAN\n"
        "incl. BAF / CAF\nexcl. THC both ends\n"
        "DTHC CAD 735\nDOC USD 50\n14 DEM + 7 DET\n"
        "PSS subject to vessel\n"
        "valid ETD 12-19 OCT\n"
        "subject to space/equipment\n"
        "rail VAN-TOR excluded"))

    # --- 10. agent quote #2 (cheap base, thin all-in margin) --------------
    save("10_quote_north_bridge.eml", _eml(
        "sales@northbridge-freight.com",
        "Rate for your RFQ — North Bridge Freight",
        "Fri, 02 Oct 2026 15:05:00 -0400",
        "Our rate for your RFQ, North Bridge Freight:\n"
        "O/F USD 1,850 / 40HC\nPOL SHA\nPOD VAN\n"
        "incl BAF, CAF\nexcl THC\n"
        "THC USD 450\nDOC USD 45\n14 DEM + 7 DET\n"
        "valid ETD Oct 12-19\nsubject to space"))

    # --- 11. agent quote #3 (above budget) --------------------------------
    save("11_quote_global_ocean.eml", _eml(
        "rates@globalocean.com",
        "Ocean rate — Global Ocean Lines (40HC SHA→VAN)",
        "Sat, 03 Oct 2026 02:20:00 -0700",
        "Rate from Global Ocean Lines:\n"
        "USD 2,380 / 40HC, POL Shanghai, POD Vancouver\n"
        "excl. everything else\nDTHC USD 480\nDOC USD 60\n"
        "valid ETD 12-19 OCT\nsubject to space/equipment"))

    return created
