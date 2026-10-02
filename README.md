# OrderInbox AI for Odoo

**Unattended AI order entry.** OrderInbox watches your inbox, reads customer
purchase orders in any format — PDF, Excel, CSV, or plain email — matches
customers and SKUs against your Odoo catalog, validates them against your
business rules, and creates **draft** sales orders in Odoo for a human to
approve.

> Employees stop typing. The AI does the 90% that is mechanical;
> deterministic rules and a human approval handle the rest.

```
        Outlook / Gmail
             │
        incoming order
             │
      ┌──────▼───────┐
      │ OrderInbox AI │   ← Docker appliance, runs locally
      └──────┬───────┘
   ┌─────────┼──────────┐
   ▼         ▼          ▼
 PDF      Excel/CSV   Email body
   └─────────┬──────────┘
             ▼
     document parser        (deterministic tables first,
             │               local LLM for what tables can't give)
             ▼
   structured order
             │
      ┌──────┴──────┐
      ▼             ▼
 customer        SKU matching   ← against your live Odoo catalog
      └──────┬──────┘
             ▼
    business rules
   quantity valid? pack size? price? currency?
   duplicate PO? minimums? maxs?
             │
       ┌─────┴─────┐
       ▼           ▼
     READY      EXCEPTION
     │           │
  draft order  review queue
     │
     ▼
    ODOO
```

## Why this is not another OCR app

- **It watches the inbox.** No download → open Odoo → upload → OCR loop.
  When a PO lands in the mailbox, OrderInbox has already matched it and
  prepared the draft order.
- **The catalog is Odoo's.** There is no second SKU database to maintain —
  matching runs against the products and partners *in your Odoo instance*.
- **Deterministic validation.** Pack sizes, minimums, price deviation vs.
  catalog, currency, duplicate POs — checked by rules, not vibes.
- **Drafts, not decisions.** The AI never confirms a consequential
  transaction. A person clicks *Approve*; a draft order appears in Odoo.
- **Private by default.** Inference runs on a local model (Qwen via Ollama)
  in the same container as the data. Customer documents, pricing, and
  catalogs never need to leave your infrastructure.

## The two-minute demo

```bash
docker compose up
```

Open **http://localhost:8501** (password: `orderinbox`). On first boot the
appliance seeds a realistic demo — 7 messages covering a clean Excel order,
a formal PDF PO, a CSV with a suspicious SKU, an inline email-body order, a
duplicate PO, a pack-size violation, and a non-order email — and shows each
one with its match scores, validation results, and one-click approval.

No Docker?

```bash
pip install -e ".[dev]"
orderinbox demo          # builds + processes the demo, starts the console
```

## What it handles

| Input | Example |
|---|---|
| PDF purchase orders | formal POs with header blocks and line tables, text PDFs, scanned PDFs (OCR) |
| Excel / CSV spreadsheets | `SKU / Description / Qty / Unit Price / Total` layouts, headers in any order |
| Plain email bodies | "Please order: … SKU qty price …" typed directly in the message |
| Multi-attachment messages | body + PDF + spreadsheet at once |
| What it skips | newsletters, meeting reminders, anything that isn't an order |

## Business rules enforced

- **PACK_MULTIPLE** — quantity must be a multiple of the product pack size
- **MIN_QTY / MAX_QTY** — per-product order quantity bounds
- **PRICE_DEV** — unit price vs. Odoo catalog price deviation (warning)
- **CURRENCY** — order currency must be in your allowed list
- **DUPLICATE_PO** — same customer + PO number already processed
- **MISSING_SKU / WEAK_SKU** — no or low-confidence product match
- **MISSING_CUSTOMER / WEAK_CUSTOMER** — partner match confidence
- **MISSING_QTY / LOW_CONFIDENCE** — data completeness

Errors send the order to the **exception queue**; warnings are flagged but the
order stays approvable.

## Deployment modes

| Mode | Where inference runs | How |
|---|---|---|
| **A — hosted** | our GPU infrastructure | we run the appliance for you |
| **B — customer server** | the customer's own GPU/server | this Docker image on their hardware |
| **C — hybrid** | small local model, optional cloud fallback | `LLM_MODE=auto` (default): local first, cloud fallback when configured |

## Connecting a real Odoo

Set `ODOO_MODE=live` and provide credentials (see `.env.example`). The
appliance reads partners (`res.partner`) and products (`product.product`)
over XML-RPC and creates `sale.order` drafts on approval. It needs the
standard `sale` module; a custom `pack_qty`/`min_qty` field is used for
rules when present.

## Configuration

Everything is environment variables — see [`.env.example`](.env.example).

Key ones:

| Variable | Default | Meaning |
|---|---|---|
| `ODOO_MODE` | `mock` | `mock` (demo catalog) or `live` (real Odoo) |
| `ODOO_URL/DB/USERNAME/PASSWORD` | — | Odoo credentials when live |
| `MAIL_PROVIDER` | `imap` | `imap`, `gmail`, or `none` (manual files) |
| `IMAP_HOST/PORT/USERNAME/PASSWORD/FOLDER` | — | inbox to watch |
| `LLM_MODE` | `auto` | `auto` = local Ollama first, cloud fallback if configured |
| `OLLAMA_URL` / `OLLAMA_MODEL` | `localhost:11434` / `qwen2.5:1.5b` | local model |
| `LLM_CLOUD_BASE_URL/API_KEY/MODEL` | — | any OpenAI-compatible endpoint (fallback) |
| `MATCH_THRESHOLD` | `80` | match score for auto-ready |
| `PRICE_DEVIATION_TOLERANCE` | `0.15` | price deviation before warning |
| `ALLOWED_CURRENCIES` | `USD,CAD,EUR` | currency allowlist |
| `WEB_PASSWORD` | `orderinbox` | console password |
| `ORDERINBOX_DEMO` | `0` | seed the demo dataset on first boot |

## The console

- **Dashboard** — ready / exceptions / drafts created at a glance
- **Orders** — every message that looked like an order, filterable by status
- **Order detail** — extracted lines with match scores, catalog prices,
  validation issues with suggested fixes, pipeline log, one-click
  *Approve → create draft in Odoo* or *Reject*
- **Inbox** — trigger a manual poll; in production it polls automatically
- **Settings** — effective configuration + live Odoo connection test

## Development

```bash
pip install -e ".[dev]"
pytest            # 29 tests: parsing, matching, rules, end-to-end pipeline
orderinbox serve
```

## Roadmap

- Odoo Apps Store module (native app listing + in-Odoo review screen)
- Approval workflows with multi-level sign-off and email notifications
- Price-book / customer-specific pricing rules
- Scanned-PDF OCR tuning per document family
- SAP / Dynamics / NetSuite connectors after Odoo traction

## License

MIT (see [LICENSE](LICENSE)).
