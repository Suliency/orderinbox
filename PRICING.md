# Pricing & Business Model

OrderInbox AI is a B2B appliance for manufacturers, wholesalers, and
distributors running Odoo. Pricing targets the cost of the problem, not the
cost of the software.

## Anchor math (used in every sales conversation)

A reseller with **300 emailed orders/month, ~40 lines average**:

- Manual entry ≈ 20 min/order (keying + checking pack sizes + fixing errors)
- 300 × 20 min = **100 hours/month** ≈ 2.5 FTE
- At $25–30/hr loaded cost: **$2,500–3,000/month** of labor
- Plus the hidden cost: 1–2% of orders with a keying error → invoice disputes,
  credit notes, late quotes.

A $199–799/month subscription replaces 2.5 FTE. The ROI conversation is a
non-event — the buyer is already paying for the work, in wages.

## Plans

| Plan | Orders/mo | Price | Positioning |
|---|---:|---:|---|
| Starter | 100 | **$199/mo** | Single warehouse, one inbox |
| Business | 500 | **$399/mo** | The target buyer from the scan (300 POs/mo, 275 SKUs) |
| Pro | 2,000 | **$799/mo** | Multi-branch distributors |
| Enterprise | custom | **$1,500+/mo** | On-prem appliance, customer GPU, private model |

Onboarding: **$1,000–$3,000** one-time — customer/SKU mapping, business-rule
setup, first-week shadow mode. This is where the buyer's specific pack sizes,
price books, and PO formats get encoded.

## Why these numbers

1. **Not consumer-SaaS pricing.** The buyer is a CFO/controller comparing
   against FTE cost and ERP-consulting day rates ($250–400/day), not against
   $15 apps.
2. **Volume tiers, not seat tiers.** Order entry volume scales with revenue;
   seats do not.
3. **Onboarding fee funds the real work.** The first deployment is where
   formats get mapped; charging for it filters out tire-kickers and pays for
   the time.
4. **Low marginal cost.** Local inference on one appliance serves a
   2,000-PO/month customer at near-zero variable cost — the plan's "private
   AI" story is also the margin story.

## Partner economics (see PARTNERS.md)

20–30% recurring to Odoo partners. At a $399 Business plan the partner earns
~$88–119/month per client; 20 clients ≈ $1,800–2,400/month for doing demos
they would have done anyway.

## Deployment pricing

| Mode | What the customer pays | Notes |
|---|---|---|
| Hosted (our GPU) | subscription only | we own the hardware |
| Customer GPU/server | subscription + their hardware | strongest "private AI" story |
| Hybrid (local + cloud fallback) | subscription | for shops wanting cloud fallback without a GPU |

## RateScout (freight) tier

The freight-forwarding product (see
[RateScout_Strata_Local_AI_Integration.md](RateScout_Strata_Local_AI_Integration.md))
is a different buyer — a forwarder's ops/desk team — and prices on a different
anchor: the cost of rate work, not order entry. The appliance ships the full
workflow: customer RFQ intake, agent-quote normalization against the freight
ontology, lane-aware comparison, deterministic margin policy, and the
approve-to-send approval step. The private tier bundles what the proposal
calls the defensible product on top: local inference, counterparty alias
learning, and private document processing.

| Plan | Positioning | Price |
|---|---|---:|
| RateScout Cloud | hosted, shared local inference, cloud fallback | **$399–999/mo** |
| RateScout Private AI | customer-hosted, two local models (main + independent verifier), no cloud | **$2,000–5,000+/mo** + implementation |

Private-tier features (from the proposal): customer-hosted inference, local
model behind the gateway, private document processing, local TMS + Microsoft 365
connectivity, local audit log, optional cloud fallback disabled. The premium
reflects the dual-model deployment (a main model + an independent verifier) and
the integration work — not just the software license.

## Launch pricing strategy

- **First 3 paid implementations: 50% off year 1** (in exchange for a
  testimonial + a reference call). The plan's initial goal is 3 paid
  implementations before expanding to other ERPs — discounts buy the
  references that sell the next 10.
- **Founding-partner rate:** first 5 Odoo partners get 30% (not 20%) for
  their first 12 months.
- Annual prepay: 2 months free (16/12 = ~17% discount).
