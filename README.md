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

The first run downloads the local model (~1 GB) before the console starts.
On a CPU-only machine the local model is slow (from tens of seconds to a few
minutes per order), so the demo orders appear in the console one by one as
they finish processing.

Have an NVIDIA GPU (4 GB+ VRAM)? Use it — the demo's model calls drop from
tens of seconds each to under a second:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up
```

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

## The Odoo module (Odoo Apps Store)

`addons/orderinbox` is the in-Odoo face of the product — a review-queue
module you install in the customer's Odoo (it's what gets listed on the
[Odoo Apps Store](https://apps.odoo.com)):

- **Incoming orders kanban** — ready / exception / draft-created / rejected,
  with the validation issues and per-line match scores visible on the card
- **Approve** builds a draft `sale.order` (customer, PO reference, lines,
  quoted prices) — in Odoo directly, or via the appliance
- **Reject** with reason, logged to the chatter
- **Cron sync** — every 5 minutes it pulls the order list from the
  appliance's `/api/orders` endpoint (shared token, `ORDERINBOX_API_TOKEN`)
- **Settings page** — appliance URL, token, thresholds; **standalone mode**
  works with no appliance at all (manual or pushed records)

```
appliance ──/api/orders (token)──▶ orderinbox module (Odoo) ──approve──▶ draft sale.order
```

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
| `ORDERINBOX_API_TOKEN` | *(empty = API off)* | shared token for `/api/*` (Odoo module sync) |
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
pytest            # 36 tests: parsing, matching, rules, end-to-end pipeline, /api bridge
orderinbox serve
```

The Odoo module under `addons/orderinbox` is tested against a real Odoo 18
instance: clone [odoo/odoo](https://github.com/odoo/odoo) (branch `18.0`),
point `--addons-path` at it and this repo's `addons/`, and run
`odoo -d testdb -i orderinbox --test-enable --test-tags orderinbox`.

## Roadmap

- Odoo Apps Store submission (module built — see above; store listing in progress)
- Approval workflows with multi-level sign-off and email notifications
- Price-book / customer-specific pricing rules
- Scanned-PDF OCR tuning per document family
- SAP / Dynamics / NetSuite connectors after Odoo traction

## License

MIT (see [LICENSE](LICENSE)).
