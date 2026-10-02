"""Deterministic document parsing: tables + text lines + PO numbers."""
from orderinbox.extract.parser import (
    extract_lines_from_table,
    extract_lines_from_text,
    find_po_number,
)


def test_xlsx_style_table():
    rows = [
        ["Line", "SKU", "Description", "Qty", "Unit Price", "Total"],
        ["1", "PLM-103", "PVC Pipe 2 in", "24", "$8.40", "$201.60"],
        ["2", "ELC-220", "Breaker 20A", "10", "$3.20", "$32.00"],
    ]
    lines, header = extract_lines_from_table(rows)
    assert header
    assert len(lines) == 2
    assert lines[0].sku_as_written == "PLM-103"
    assert lines[0].quantity == 24
    assert lines[0].unit_price == 8.40
    assert lines[1].sku_as_written == "ELC-220"


def test_unit_price_column_not_claimed_as_qty():
    rows = [
        ["SKU", "Description", "Qty", "Unit Price", "Total"],
        ["X-1", "Widget", "5", "$2.00", "$10.00"],
    ]
    lines, header = extract_lines_from_table(rows)
    assert header
    assert lines[0].quantity == 5
    assert lines[0].unit_price == 2.00
    assert lines[0].line_total == 10.00


def test_csv_style_header_nospace():
    rows = [
        ["SKU", "Description", "Qty", "UnitPrice"],
        ["ROM-142", "Romex 14/2", "2", "312.00"],
    ]
    lines, header = extract_lines_from_table(rows)
    assert header
    assert lines[0].quantity == 2
    assert lines[0].unit_price == 312.00


def test_text_lines_with_total():
    text = (
        "PURCHASE ORDER\n"
        "PO No.: 55-901 Date: 2026-01-01\n"
        "SKU Description Qty Unit Price Total\n"
        "PLM-101 Copper Pipe 1/2 in x 10 ft 12 $29.32 $351.84\n"
        "ELC-220 Breaker 20A Single 10 $70.55 $705.50\n"
        "TOTAL USD 1057.34\n"
    )
    lines, header = extract_lines_from_text(text)
    assert header
    assert len(lines) == 2
    assert lines[0].sku_as_written == "PLM-101"
    assert lines[0].quantity == 12
    assert lines[0].unit_price == 29.32
    assert lines[0].line_total == 351.84
    assert lines[1].sku_as_written == "ELC-220"


def test_text_lines_two_column():
    text = (
        "Item Qty Price\n"
        "FST-301 Hex Bolt Box 50 10 $4.10\n"
    )
    lines, header = extract_lines_from_text(text)
    assert header
    assert len(lines) == 1
    assert lines[0].quantity == 10
    assert lines[0].unit_price == 4.10


def test_text_lines_stops_at_total():
    text = (
        "SKU Qty Unit Price Total\n"
        "A-1 thing 3 $5.00 $15.00\n"
        "TOTAL $15.00\n"
        "B-2 after 4 $6.00 $24.00\n"
    )
    lines, _ = extract_lines_from_text(text)
    assert len(lines) == 1


def test_po_number_variants():
    assert find_po_number("PO No.: PO-26-8814") == "PO-26-8814"
    assert find_po_number("please process order 26-0411") == "26-0411"
    assert find_po_number("PO 26-1187 ship to Toronto") == "26-1187"
    assert find_po_number("PO No.: 12345") == "12345"
    assert find_po_number("PO Number is pending") == ""
    assert find_po_number("No PO this week") == ""
