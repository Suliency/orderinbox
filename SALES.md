# Sales Playbook

Everything below is copy-paste-ready. The motion is always the same:
**qualify on the pain, prove it with the two-minute demo, close on ROI,
and route the relationship (partner or direct).**

---

## 1. Outreach to Odoo partners (primary channel)

**Subject:** a question about your manufacturing clients

> Hi {first name},
>
> Quick question — do any of your manufacturing or distribution clients
> still have staff manually keying emailed customer purchase orders into
> Odoo? (PDFs, spreadsheets, orders typed in the email body — the
> 60–70 line items kind.)
>
> If so, we built a tool that watches their inbox and turns those into
> **draft** sales orders in Odoo — customer matched, SKUs matched against
> your catalog, pack sizes and prices validated — for their team to
> approve in one click. It runs on a local AI model on their own
> infrastructure, so documents never leave the building.
>
> I can show you a two-minute demo on a call — or you can run it yourself
> in two minutes:
>
> ```
> git clone https://github.com/{repo}/orderinbox.git && cd orderinbox
> docker compose up
> # → http://localhost:8501  (password: orderinbox)
> ```
>
> You'd earn **20–30% recurring** on every client you introduce. Want 20
> minutes this week?
>
> {name} — OrderInbox AI

**Follow-up (day 5):**

> {first name} — bumping this. One line if it's not relevant: do any of
> your clients still key emailed POs into Odoo by hand? If yes, I'll send
> the two-minute demo link. If no, no hard feelings.

**Why this works:** it's a question, not a pitch; the demo is self-serving
(the partner can try it without us on the call); and the commission is
mentioned once, in one sentence.

---

## 2. Direct outreach to the buyer (the manufacturer/reseller)

Use when the buyer came to us (demand scan, Upwork request, website form)
or when a partner says "I'll let the client talk to you directly."

**Subject:** your PO inbox → draft Odoo orders, unattended

> Hi {name},
>
> You mentioned your team keys about {N} emailed purchase orders a month
> into Odoo by hand. That's roughly {N × 0.3} hours a month of typing —
> before the wrong-pack and wrong-price catches.
>
> We built OrderInbox AI: it watches your orders inbox, reads the PDF /
> Excel / email body, matches the customer and every SKU against your Odoo
> catalog, checks pack sizes, minimums, prices and duplicates, and puts a
> **draft** sales order in front of your team — approve or send to
> exception review. Your documents stay on your own server (local AI).
>
> Two things I'd like to do this week:
> 1. Run the two-minute demo (no setup — I'll walk you through it live)
> 2. Take **three real past POs** from your team (anonymized) and show you
>    exactly what the system would produce for each — matches, flags, and
>    the draft order.
>
> The second one is the honest test: if it handles your actual PO formats,
> it's worth a conversation. If it doesn't, you've lost 30 minutes.
>
> Does {day} at {time} work?
>
> {name} — OrderInbox AI

**The "three real POs" move is the whole sales motion.** It converts a
vendor pitch into a working proof on the buyer's own data, and it's free
for us (the pipeline runs locally in minutes).

---

## 3. Objection handling

| Objection | Response |
|---|---|
| "Odoo already has OCR apps" | "They do — upload a PDF, get a draft. But somebody has to download it, open Odoo, and upload it, every single time. We make that loop disappear: the inbox is the input. And our matching runs against your live catalog with pack/price/duplicate rules — the OCR apps stop at text." |
| "What if the AI misreads an order?" | "It doesn't decide anything. It produces a draft with a confidence score on every customer and SKU match; below your threshold it lands in an exception queue with the exact issue and a suggested fix. You approve the good ones, review the rest. Nothing posts to Odoo without a human click." |
| "Our POs are scanned images" | "The appliance does OCR (Tesseract) on scanned PDFs, then the same matching and rules as everything else. We tune it against your actual scanned formats during onboarding." |
| "Does our data leave the building?" | "No. The model runs locally in the same container as your data — a Qwen-class model on your server (or ours, if you prefer hosted). Cloud is an optional fallback you configure, never a requirement." |
| "How long is onboarding?" | "One to two weeks. Week one: we connect the inbox and your Odoo, shadow mode — you watch it process your real orders without sending anything. Week two: we tune your pack sizes, price tolerance and PO formats, and flip on draft creation." |
| "What about our negotiated prices — the AI will flag everything" | "Price checks are configurable: per-customer price books, tolerance bands, or off. By default we flag deviations as warnings, not blockers — your team sees both the quoted and catalog price side by side." |
| "It's just $X/month for what a junior does for $Y" | "Yes — that's the point. The junior still exists; they stop typing and start handling exceptions, which is what they're actually paid to think about. And the volume tier means the tool costs less per order the bigger you get." |
| "We already tried an n8n/Make workflow for this" | "Those break on the 3% of POs that are formatted differently, and nobody notices until an order is missed. We built the failure path as a first-class queue: everything uncertain is *shown*, never silently dropped." |

---

## 4. The closing sequence

1. **Qualify** — orders/month, lines/order, SKUs, inbox provider, Odoo
   version, who approves orders. (Budget emerges: >300 orders/mo → Business
   or Pro.)
2. **Three-PO proof** — run their real past POs through the pipeline, walk
   through the results together. Count what it would have caught.
3. **Offer** — plan + onboarding, shadow week included. Anchor on the FTE
   math from PRICING.md.
4. **Risk reversal** — "Two weeks of shadow mode: it processes your real
   inbox but creates nothing in Odoo. If it misses things you'd have
   caught, we tune it — free."
5. **Close the loop** — onboarding booked, first week scheduled, success
   metric agreed: *% of incoming POs producing an approvable draft by
   day 14.*

---

## 5. What we never say

- "Our AI will automate your order entry" — we say *drafts*, always.
- "It reads any document" — we list the formats we've proven.
- "Zero errors" — we talk about the exception queue and scores.
- Anything about other ERPs until the Odoo goal (3 paid implementations)
  is hit.
