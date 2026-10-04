# OrderInbox AI for Odoo

**Freight quoting and unattended order entry.** OrderInbox watches your inbox
and triages every message into the workflow it belongs to:

- **Freight (RateScout) — the primary workflow.** Customer RFQs and agent
  quotes are classified, normalized against a freight ontology, linked to the
  open RFQ, compared against each other, priced against your margin policy,
  and queued for a human to approve the consequential step (send the RFQ to
  agents, issue the customer quote).
- **Order entry.** Purchase orders in any format — PDF, Excel, CSV, or plain
  email — are matched against your Odoo catalog, validated against your
  business rules, and turned into **draft** sales orders for a human to
  approve.

> The AI prepares, deterministic rules gate, and a person decides.
> Employees stop typing and stop comparing spreadsheets; the system does the
> mechanical 90% and surfaces only what needs judgment.

Every message is triaged first (deterministic classification — an RFQ asks
for rates, a quote states them, a PO carries line items):

```
     incoming message
              │
    ┌─────────┴──────────┐
    ▼                    ▼
 freight lane        order lane
 (RFQ / quote)      (purchase order)
    │                    │
    ▼                    ▼
 normalize · link   parse · customer ·
 compare · margin   SKU match · rules
    │                    │
    ▼                    ▼
 READY / EXCEPTION    READY / EXCEPTION
    │                    │
 approve:             approve:
 RFQ out / quote      draft sale.order
 issued               in Odoo
```

The order lane in detail:

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

## The freight workflow (RateScout)

The freight lane runs the full RFQ-to-quote cycle on the same appliance:

```
 customer RFQ            agent quotes (A, B, C …)
 "2x 40HC SHA→TOR,         "USD 1,925 / 40HQ, POL SHA,
  ready Oct 12,           POD VAN, DTHC CAD 735, 14DEM 7DET,
  budget ~USD 2,400"      valid ETD 12-19, subject to space"
        │                            │
        ▼                            ▼
 match customer           normalize (ontology: ports, equipment,
 (sender domain first)    charges → canonical codes)
        │                            │
        ▼                            ▼
 RFQ ready:           link to the open RFQ (hub-aware: a
 budget, lane,        SHA→VAN quote answers a TOR RFQ),
 free-time needs      compare all-in cost, margin vs. budget
        │                            │
        └──────────────┬─────────────┘
                       ▼
          READY (cheapest clears the margin policy)
          EXCEPTION (thin/negative margin, missing charges,
                     conditional surcharges, unknown ports)
                       │
                 human approves
                       ▼
        RFQ sent to agents · customer quote issued
```

- **Classification is deterministic.** A customer RFQ asks for rates ("need
  2x 40HC …", "cargo ready …", "can you quote"); an agent quote states them
  ("USD 1,925 / 40HQ", "valid ETD …", "subject to space"). A structured rate
  message parses with no model at all; the local model only handles the messy
  remainder, and it is never the final authority.
- **The ontology makes quotes comparable.** Messy terms ("PCS", "DTHC",
  "rail VAN-TOR excluded") normalize to canonical charge concepts, UN/LOCODE
  ports, and equipment codes — so Agent A's and Agent B's rates compare
  like-for-like, and a counterparty's conventions are *learned and persisted*
  (alias store) instead of re-guessed every time.
- **Margin is deterministic math, not judgment.** Buy cost is summed
  like-for-like, the sell is the customer's stated budget when the RFQ has
  one (otherwise the policy margin), and below `FREIGHT_MARGIN_THRESHOLD`
  the quote lands in the exception queue. Comparison text names the cheapest
  counterparty on the lane.
- **Approval is a human action.** Approving an RFQ dispatches the agent RFQs;
  approving a quote issues the customer quote. Nothing external happens
  without a click (Sections 18/19 of the proposal: the agent prepares, the
  human decides).

## Under the hood: local AI, safely

The model is the *reasoning* layer, not the source of truth. The design
follows the [Strata local-LLM integration proposal](RateScout_Strata_Local_AI_Integration.md):
**local AI handles volume, cloud AI handles ambiguity, deterministic software
handles money, and humans handle consequential exceptions.**

```
   email / document
        │
        ▼
 document parser        deterministic tables first; local model for the rest
        │
        ▼
   Model Gateway ──────────────┐   local-first routing by
        │                      │   task / confidentiality / complexity
        ├── local model (Strata / vLLM / Ollama)   ← does the volume
        ├── cloud (OpenAI / Anthropic / Gemini)    ← ambiguous tail only
        │
        ▼
 confidence-tier escalation        >0.95 auto · 0.80–0.95 second local check
                                   0.60–0.80 cloud · <0.60 human
        │
        ├── producer/verifier: a second, independent model re-derives the
        │   fields; agree → accept, disagree → escalate
        ▼
 deterministic rules               pack sizes, currency, margin, duplicates,
                                   quote safety — the model never owns money
        │
        ├── READY → draft in Odoo (human approves)
        └── EXCEPTION → review queue
```

- **Model gateway.** Nothing is hard-wired to one backend. The workflow talks
  to a gateway that routes each task local-first; Strata, vLLM, Ollama,
  OpenAI, Anthropic, and Gemini are all swappable by configuration.
- **Local-first, cloud-escalation.** A strong local model covers the ordinary
  95–99% of work. Only the genuinely ambiguous tail escalates to a cloud
  model (or a human), so you don't pay cloud prices for routine volume.
- **Confidence tiers + producer/verifier.** Every extraction carries a
  confidence and provenance. Borderline values get a second, independent
  model's check before they're trusted — two samplers agreeing is a cheap
  guard against a one-off hallucination.
- **Freight (RateScout) domain.** The same gateway powers the freight
  workflow described above — classification, normalization, comparison,
  margin, and alias learning (see *The freight workflow*).
- **MCP tools.** The capabilities are exposed as Model Context Protocol tools
  (`freight.*`, customer, quote, approval) at `/mcp`, so a local or external
  agent can call the product as controlled tools rather than free-form prompt.

## The two-minute demo

```bash
docker compose up
```

Open **http://localhost:8501** (password: `orderinbox`). On first boot the
appliance seeds a realistic demo — 11 messages: a customer RFQ (40HC
Shanghai→Toronto with a stated budget) answered by three agent quotes
(all-in cheapest wins the comparison; the thin-margin and above-budget
quotes land in the exception queue), plus seven order-side messages — a
clean Excel order, a formal PDF PO, a CSV with a suspicious SKU, an inline
email-body order, a duplicate PO, a pack-size violation, and a non-order
email. Each shows its match scores, validation results, and one-click
approval.

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
| Customer RFQs | "need 2x 40HC Shanghai to Toronto, ready Oct 12, budget USD 2,400" — matched to the customer, budget and free-time needs captured |
| Agent quotes | structured rate emails (Section 8 format) parse deterministically; messy quotes go to the local model, then the ontology |
| PDF purchase orders | formal POs with header blocks and line tables, text PDFs, scanned PDFs (OCR) |
| Excel / CSV spreadsheets | `SKU / Description / Qty / Unit Price / Total` layouts, headers in any order |
| Plain email bodies | "Please order: … SKU qty price …" typed directly in the message |
| Multi-attachment messages | body + PDF + spreadsheet at once |
| What it skips | newsletters, meeting reminders, anything that is neither an order nor freight |

## Business rules enforced

Freight lane:

- **MARGIN_BELOW_THRESHOLD** — margin vs. the customer budget (or policy
  margin) below `FREIGHT_MARGIN_THRESHOLD`
- **MISSING_BASE_FREIGHT / INCOMPLETE_QUOTE** — rate not stated; expected
  charges (ORIGIN_THC, DESTINATION_THC, DOCUMENTATION_FEE, DELIVERY) not
  accounted for
- **EXCLUDED_WITHOUT_CHARGE** — a charge excluded from the base rate with no
  visible amount (destination THC is an error; the rest warnings)
- **ROUTE_SAME_ENDPOINTS / ROUTE_UNKNOWN_PORT / EQUIPMENT_UNKNOWN** — route
  and equipment sanity
- **CURRENCY** — quote currency outside the allowlist
- **NO_FREE_TIME / CONDITIONAL_CHARGES** — free-time and conditional
  surcharge (PSS/GRI/BAF/CAF) review flags
- **DUPLICATE_QUOTE** — the same lane/equipment/counterparty already
  processed

Order lane:

- **PACK_MULTIPLE** — quantity must be a multiple of the product pack size
- **MIN_QTY / MAX_QTY** — per-product order quantity bounds
- **PRICE_DEV** — unit price vs. Odoo catalog price deviation (warning)
- **CURRENCY** — order currency must be in your allowed list
- **DUPLICATE_PO** — same customer + PO number already processed
- **MISSING_SKU / WEAK_SKU** — no or low-confidence product match
- **MISSING_CUSTOMER / WEAK_CUSTOMER** — partner match confidence
- **MISSING_QTY / LOW_CONFIDENCE** — data completeness

Errors send the item to the **exception queue**; warnings are flagged but the
item stays approvable.

## Deployment modes

| Mode | Where inference runs | How |
|---|---|---|
| **A — hosted** | our GPU infrastructure | we run the appliance for you |
| **B — private** | the customer's own GPU/server | `docker compose -f docker-compose.strata.yml up` — [Strata](https://github.com/Niko1221/Strata) as the main local model (`--profile verifier` adds an independent verifier), no cloud |
| **C — hybrid** | small local model, optional cloud fallback | `LLM_MODE=auto` (default): local first, cloud fallback only for the ambiguous tail |

In every mode the model is behind the gateway, so you can start on a small
local model and move to a stronger local model (or a DGX Spark running the
main + verifier models) or a cloud endpoint by changing configuration — not
code.

### Strata, the local model

[Strata](https://github.com/Niko1221/Strata) runs Qwen3.8-Flash-Next (a 125B
mixture-of-experts model) on a single 12 GB+ graphics card with 32-64 GB of
RAM, behind an OpenAI-compatible API. Point `STRATA_URL` at it and it becomes
the main local model: the router gives it the volume ahead of the small
Ollama model, which stays as fallback and as the second model for the
producer/verifier check.

- **Bundled:** `docker compose -f docker-compose.strata.yml up` builds Strata
  `v0.1.38`, downloads the model (~70 GB, once) and wires the app to it.
  On a shared host, `STRATA_GPUS` pins the cards it may see (an nvidia-smi
  index or UUID, default `all`), `STRATA_PORT` moves its UI off 8080, and
  `STRATA_LOW_RAM=mmap` keeps the experts in reclaimable file cache instead
  of locked RAM when other services need the memory.
  Measured on one RTX 3090 (IQ2_XS, mmap mode, warm): ~105-110 tokens/s
  written, ~1,250-1,550 tokens/s read, 3-11 s per extraction call.
- **Already installed on the host** (Strata's `./setup.sh`):
  `STRATA_URL=http://127.0.0.1:8080/v1` (from a container:
  `http://host.docker.internal:8080/v1`).
- The app polls Strata's `/health` every 30 s until the model has loaded, so
  start order doesn't matter. Vision (scanned quotes) is detected from the
  same endpoint.
- Strata thinks at level *high* by default. Extraction calls ask for
  `STRATA_REASONING_EFFORT=low`; use `none` for maximum throughput, since Strata
  answers one request at a time.

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
| `LLM_MODE` | `auto` | `auto` = local first, cloud fallback; `ollama`/`cloud`/`off` |
| `OLLAMA_URL` / `OLLAMA_MODEL` | `localhost:11434` / `qwen2.5:1.5b` | local model (zero-config default) |
| `STRATA_URL` / `STRATA_MODEL` | — / `strata` | [Strata](https://github.com/Niko1221/Strata) local model server — the main local model when set |
| `STRATA_REASONING_EFFORT` / `STRATA_REASONING_BUDGET` | `low` / `0` | Strata thinking level per request, optional hard token cap |
| `VLLM_URL` / `VLLM_MODEL` | — | second local model (independent verifier / vision) |
| `LLM_CLOUD_BASE_URL/API_KEY/MODEL` | — | OpenAI-compatible cloud (escalation target) |
| `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` | — | Anthropic / Gemini cloud providers |
| `ESCALATE_AUTO` / `ESCALATE_VERIFY` / `ESCALATE_CLOUD` | `0.95` / `0.80` / `0.60` | confidence-tier cut-points |
| `USE_PRODUCER_VERIFIER` / `USE_CLOUD_ESCALATION` | `1` / `1` | second-model check / cloud escalation on/off |
| `FREIGHT_MARGIN_THRESHOLD` | `10` | min freight margin (%) before review |
| `MATCH_THRESHOLD` | `80` | match score for auto-ready |
| `PRICE_DEVIATION_TOLERANCE` | `0.15` | price deviation before warning |
| `ALLOWED_CURRENCIES` | `USD,CAD,EUR` | currency allowlist |
| `WEB_PASSWORD` | `orderinbox` | console password |
| `ORDERINBOX_API_TOKEN` | *(empty = API off)* | shared token for `/api/*` (Odoo module sync) |
| `ORDERINBOX_DEMO` | `0` | seed the demo dataset on first boot |

## The console

- **Dashboard** — orders and freight (RFQs + quotes) ready / exceptions at a
  glance, plus the most recent freight cases
- **Freight** — every RFQ and quote, filterable by kind and status; detail
  shows the customer request or the normalized quote (charges, inclusions,
  free time, conditions), buy cost / sell / margin against the policy, the
  lane ranking, and one-click *Approve → send RFQ / issue quote* or *Reject*
- **Orders** — every message that looked like an order, filterable by status
- **Order detail** — extracted lines with match scores, catalog prices,
  validation issues with suggested fixes, pipeline log, one-click
  *Approve → create draft in Odoo* or *Reject*
- **Inbox** — trigger a manual poll; in production it polls automatically
- **Settings** — effective configuration + live Odoo connection test

## Development

```bash
pip install -e ".[dev]"
pytest            # 104 tests: parsing, matching, rules, model gateway,
                 # freight workflow (RFQ/quote intake, comparison, margin,
                 # approval), escalation, producer/verifier, MCP,
                 # end-to-end pipeline, /api bridge
orderinbox serve
```

The Odoo module under `addons/orderinbox` is tested against a real Odoo 18
instance: clone [odoo/odoo](https://github.com/odoo/odoo) (branch `18.0`),
point `--addons-path` at it and this repo's `addons/`, and run
`odoo -d testdb -i orderinbox --test-enable --test-tags orderinbox`.

## Roadmap

- Agent rate book — persistent, learned historical rates per lane/counterparty
  (the MCP `rate.save`/`rate.search` surface is in place; the store backend
  is next)
- TMS booking — a quote the customer accepts becomes a booking draft
  (`freight.tms.create_shipment` tool is in place)
- DG workflow — dangerous-goods RFQs get a dedicated review path (IMDG class,
  packaging, carrier approval)
- Customer price book — negotiated freight sell rates per customer/lane
- Odoo Apps Store submission (module built — see above; store listing in progress)
- Approval workflows with multi-level sign-off and email notifications
- Scanned-PDF OCR tuning per document family
- SAP / Dynamics / NetSuite connectors after Odoo traction

## License

MIT (see [LICENSE](LICENSE)).
