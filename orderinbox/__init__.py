"""OrderInbox / RateScout AI — freight quoting and unattended order entry
for Odoo.

Triage first: customer RFQs and agent quotes run the freight workflow
(normalize → link to the open RFQ → compare → margin policy → human
approval); purchase orders in any format run the order pipeline (extract →
match against the Odoo catalog → deterministic rules → draft sale order).
Model work is routed through a local-first gateway (Strata / vLLM / Ollama,
with confidence-tier escalation to cloud for the ambiguous tail).
"""

__version__ = "0.4.0"
PRODUCT_NAME = "OrderInbox AI"
