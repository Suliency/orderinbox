"""End-to-end pipeline: document → match → validate → draft order.

Uses LLM off so the tests are deterministic and fast; attachment parsing is
fully deterministic. The inline-text case is covered by the text-line parser.
"""
import io
from email.message import EmailMessage

from orderinbox.extract.documents import extract_document, order_like_text
from orderinbox.odoo.mock import MockOdooBackend


def _xlsx_bytes(lines):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["Line", "SKU", "Description", "Qty", "Unit Price", "Total"])
    for i, (sku, qty, price) in enumerate(lines, 1):
        ws.append([i, sku, f"item {sku}", qty, price, round(qty * price, 2)])
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


def _eml_with_xlsx(sender, subject, body, xlsx):
    msg = EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg["Date"] = "Mon, 28 Sep 2026 09:00:00 -0500"
    msg.set_content(body)
    msg.add_attachment(xlsx, maintype="application",
                       subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       filename="order.xlsx")
    return msg.as_bytes()


def test_order_like_text_detects_order():
    assert order_like_text("PO 123. Quantity 5 of item at unit price $4.00, total $20.00.")
    assert not order_like_text("Hi team, the safety meeting is moved to Friday.")


def test_document_extraction_roundtrip(tmp_path):
    xlsx = _xlsx_bytes([("SAF-401", 2, 9.99), ("ELC-220", 4, 3.20)])
    eml = _eml_with_xlsx("ap@prairieridge.build", "Order 999", "Please process.", xlsx)
    p = tmp_path / "msg.eml"
    p.write_bytes(eml)
    doc = extract_document(p, spool_dir=tmp_path)
    assert doc.kind == "eml"
    assert doc.subject == "Order 999"
    assert len(doc.attachments) == 1
    att = extract_document(doc.attachments[0], spool_dir=tmp_path)
    assert att.kind == "xlsx"
    # the attachment table is parsed
    from orderinbox.extract.parser import extract_lines_from_table
    lines, header = extract_lines_from_table(att.tables[0])
    assert header
    assert lines[0].sku_as_written == "SAF-401"
    assert lines[0].quantity == 2


def test_pipeline_end_to_end(env, tmp_path):
    settings, backend, llm, store, pipeline = env
    xlsx = _xlsx_bytes([("SAF-401", 4, 9.99), ("SAF-420", 4, 6.50)])
    eml = _eml_with_xlsx("ap@prairieridge.build", "Order 777", "Please process attached.", xlsx)
    p = tmp_path / "msg.eml"
    p.write_bytes(eml)

    order = pipeline.process_message(p)
    # customer matched from sender domain
    assert order.extraction is not None
    assert order.extraction.customer_matched.startswith("Prairie")
    assert order.extraction.po_number == "777"
    # all lines match + valid pack/min → ready
    assert order.status.value in ("ready", "exception")
    matched = [l for l in order.extraction.lines if l.sku_matched]
    assert len(matched) == 2

    # approve → draft created in (mock) Odoo
    pipeline.approve_and_send(order)
    assert order.status.value == "sent"
    assert order.odoo_reference
    assert any(o.get("partner", "").startswith("Prairie") for o in backend.created_orders)


def test_pipeline_duplicate_detection(env, tmp_path):
    settings, backend, llm, store, pipeline = env
    xlsx = _xlsx_bytes([("SAF-401", 4, 9.99)])
    eml1 = _eml_with_xlsx("ap@prairieridge.build", "Order 888", "First.", xlsx)
    eml2 = _eml_with_xlsx("ap@prairieridge.build", "Order 888 resend", "Resend.", xlsx)
    p1 = tmp_path / "a.eml"; p1.write_bytes(eml1)
    p2 = tmp_path / "b.eml"; p2.write_bytes(eml2)

    first = pipeline.process_message(p1)
    assert "DUPLICATE_PO" not in {i.code for i in first.issues}
    second = pipeline.process_message(p2)
    assert "DUPLICATE_PO" in {i.code for i in second.issues}
    assert second.status.value == "exception"


def test_non_order_rejected(env, tmp_path):
    settings, backend, llm, store, pipeline = env
    msg = EmailMessage()
    msg["From"] = "facilities@example.com"
    msg["Subject"] = "Meeting reminder"
    msg["Date"] = "Fri, 02 Oct 2026 08:00:00 -0500"
    msg.set_content("Reminder: the warehouse safety meeting will be held Friday at 3pm.")
    p = tmp_path / "m.eml"; p.write_bytes(msg.as_bytes())
    order = pipeline.process_message(p)
    assert order.status.value in ("rejected", "failed")
