"""The LLM prompt is sized to what the parser still needs.

When the deterministic parser already found the order lines, the LLM is
asked for header fields only — generating every line as JSON is the slow part
on CPU and the result was discarded anyway. When no lines were found
(inline email text, scanned PDFs) the full order prompt is used.
"""
from orderinbox.extract.documents import DocumentContent
from orderinbox.extract.llm import HEADER_EXTRACTION_PROMPT, ORDER_EXTRACTION_PROMPT
from orderinbox.extract.parser import extract_order


class FakeLLM:
    def __init__(self, response: dict):
        self.response = response
        self.prompts: list[str] = []

    def available(self) -> bool:
        return True

    def extract_json(self, system: str, user: str) -> dict:
        self.prompts.append(user)
        return self.response


TABLE = [
    ["SKU", "Description", "Qty", "Unit Price"],
    ["SAF-401", "Safety glasses", "4", "10.22"],
    ["FST-321", "Hex bolt M8", "20", "0.35"],
]


def test_header_only_prompt_when_parser_found_lines(env):
    settings = env[0]
    llm = FakeLLM({"is_order": True, "customer_name": "Acme Fabrication",
                   "po_number": "PO-77", "payment_terms": "Net 30"})
    doc = DocumentContent(kind="xlsx", text="Purchase order PO-77 from Acme", tables=[TABLE])

    ex = extract_order([doc], llm, settings, subject="PO-77")

    assert len(llm.prompts) == 1
    assert llm.prompts[0].startswith(HEADER_EXTRACTION_PROMPT)
    assert ex.customer_name_as_written == "Acme Fabrication"
    assert ex.po_number == "PO-77"
    assert ex.payment_terms == "Net 30"
    assert [l.sku_as_written for l in ex.lines] == ["SAF-401", "FST-321"]
    assert ex.extract_method == "hybrid"


def test_full_prompt_when_parser_found_no_lines(env):
    settings = env[0]
    llm = FakeLLM({"is_order": True, "customer_name": "Acme Fabrication", "po_number": "PO-78",
                   "lines": [{"line_no": 1, "sku": "SAF-401", "description": "Safety glasses",
                              "quantity": 4, "unit": None, "unit_price": 10.22}]})
    doc = DocumentContent(kind="eml", text="Purchase order PO-78: please send 4 box of the safety glasses "
                                           "at $10.22 each. Thanks!")

    ex = extract_order([doc], llm, settings, subject="Purchase order PO-78")

    assert len(llm.prompts) == 1
    assert llm.prompts[0].startswith(ORDER_EXTRACTION_PROMPT)
    assert [l.sku_as_written for l in ex.lines] == ["SAF-401"]
    assert ex.extract_method == "llm"
