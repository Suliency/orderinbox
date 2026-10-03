# RateScout AI + Strata Local LLM Integration Proposal

## Executive Summary

For **RateScout AI**, integrating a strong local model behind the **Strata framework** can materially strengthen the product—especially if the goal is to sell **private/on-prem freight AI** rather than another cloud-LLM wrapper.

Recommended design:

- Use **Strata as the local reasoning/model-serving layer**
- Keep RateScout responsible for workflow state, freight ontology, pricing logic, deterministic validation, permissions, approval policy, audit logs, and integrations
- Use a **model gateway** so Strata can later be swapped for vLLM, OpenAI, Anthropic, Gemini, or another inference layer
- Let local AI handle the majority of workload
- Escalate only highly ambiguous cases to a cloud model or human review
- Never let an LLM directly own final commercial calculations or uncontrolled external actions

> **The LLM interprets freight information. Deterministic software controls money and workflow authority.**

## 1. Strata's Role in RateScout

Strata should be the **reasoning engine**, not the whole application.

```text
                   RateScout AI
                        │
        ┌───────────────┼────────────────┐
        │               │                │
        ▼               ▼                ▼
 Outlook/Gmail       TMS/API       Web dashboard
        │
        ▼
┌──────────────────────────────┐
│       Workflow Engine        │
│ FastAPI + state machine      │
└───────────────┬──────────────┘
                │
                ▼
┌──────────────────────────────┐
│       Freight Agent Layer    │
│ RFQ understanding            │
│ quote extraction             │
│ rate comparison              │
│ supplier follow-up           │
│ exception reasoning          │
└───────────────┬──────────────┘
                │
                ▼
        ┌───────────────┐
        │    Strata     │
        │ local AI API  │
        └───────┬───────┘
                │
                ▼
       Strong local Qwen model
                │
        local GPU infrastructure
```

RateScout owns:

```text
PostgreSQL
freight ontology
rate database
currency conversion
margin calculation
customer rules
permissions
approval policy
audit trail
email state
TMS synchronization
```

Strata owns:

```text
language understanding
document interpretation
reasoning
tool selection
drafting
ambiguity resolution
```

## 2. Model Gateway

Do not hard-code RateScout around Strata.

```text
RateScout
    │
    ▼
Model Gateway
    │
    ├── Strata local
    ├── vLLM local
    ├── OpenAI
    ├── Anthropic
    └── Gemini
```

Example interface:

```python
result = model_router.run(
    task="quote_normalization",
    data=quote,
    confidentiality="high",
    complexity="medium"
)
```

## 3. Local-First, Cloud-Escalation Strategy

A strong local model can potentially handle **95–99%** of ordinary freight operations.

```text
                   ALL REQUESTS
                        │
                        ▼
                 Local Strata
                        │
            ┌───────────┴───────────┐
            │                       │
         confident               uncertain
            │                       │
            ▼                       ▼
 deterministic validation    second local pass
            │                       │
            │                  still uncertain?
            │                    /       \
            │                  no         yes
            │                   │          │
            └───────────────────┘       cloud
                       │
                       ▼
                  final result
```

Cloud inference should be reserved for:

- conflicting documents
- unusual surcharge terminology
- ambiguous freight conditions
- complex multi-document reasoning
- unusual legal or commercial language
- edge cases not covered by the freight ontology

## 4. Local AI Tasks

Local AI is well suited for:

- email classification
- quote extraction
- document interpretation
- entity matching
- charge normalization
- route understanding
- customer requirement extraction
- follow-up drafting
- supplier-response comparison
- exception explanation
- structured JSON generation

A strong 14B–32B model can cover a large share of this workload, with a larger local or cloud model reserved for difficult cases.

## 5. Agent + Tool Architecture

The local agent should use controlled tools rather than an enormous prompt.

```text
Local Strata Agent
        │
        ├── search_customer()
        ├── search_carrier()
        ├── lookup_port()
        ├── lookup_rate_history()
        ├── get_exchange_rate()
        ├── lookup_charge_alias()
        ├── request_quote()
        ├── parse_attachment()
        ├── calculate_sell_rate()
        ├── create_quote()
        └── send_followup()
```

Example workflow:

```text
Customer:
"Need 2x40HC Shanghai → Toronto.
Non-DG electronics.
Ready Oct 12.
Need 14 free days."

                  ↓
Local Agent
                  ↓
lookup_customer()
                  ↓
search_preferred_agents(...)
                  ↓
send_rfq(agent_A)
send_rfq(agent_B)
send_rfq(agent_C)
                  ↓
wait for responses
                  ↓
parse_quote(A/B/C)
                  ↓
normalize_charges()
                  ↓
check_completeness()
                  ↓
calculate_sell_rate()
                  ↓
present recommendation
```

The agent reasons; the tools perform business operations.

## 6. MCP Architecture

Expose RateScout capabilities as MCP tools:

```text
ratescout-mcp
│
├── freight.customer.search
├── freight.port.search
├── freight.rate.search
├── freight.rate.save
├── freight.quote.normalize
├── freight.quote.compare
├── freight.quote.calculate_sell
├── freight.email.read
├── freight.email.send
├── freight.tms.create_shipment
└── freight.approval.request
```

Architecture:

```text
          Strata Local Model
                  │
                 MCP
                  │
          RateScout Tools
                  │
        ┌─────────┼─────────┐
        ▼         ▼         ▼
     Outlook     DB        TMS
```

## 7. Document Processing

Do not send every document directly to a VLM.

### PDF

```text
PDF
 │
 ├── native PDF?
 │       ↓
 │    PyMuPDF
 │
 └── scanned?
         ↓
      VLM/OCR
         │
         ▼
   extracted blocks
         │
         ▼
       Strata
         │
         ▼
structured freight objects
```

### Excel

```text
XLSX
 ↓
openpyxl / pandas
 ↓
structured rows
 ↓
Strata interprets semantics
```

Use vision only for scans, image-based quotes, poor layouts, and complex tables.

## 8. Example Freight Quote Interpretation

Input:

```text
USD 1,925 / 40HQ

POL: SHA
POD: VAN

incl. BAF / CAF
excl. THC both ends

DTHC CAD 735
DOC USD 50
14 DEM + 7 DET

PSS subject to vessel

valid ETD 12–19 OCT

subject to space/equipment

rail VAN–TOR excluded
```

Target output:

```json
{
  "origin_port": "CNSHA",
  "destination_port": "CAVAN",
  "equipment": "40HC",
  "base_freight": {
    "amount": 1925,
    "currency": "USD"
  },
  "included": ["BAF", "CAF"],
  "excluded": [
    "ORIGIN_THC",
    "DESTINATION_THC",
    "VANCOUVER_TORONTO_RAIL"
  ],
  "charges": [
    {
      "type": "DESTINATION_THC",
      "amount": 735,
      "currency": "CAD"
    },
    {
      "type": "DOCUMENTATION",
      "amount": 50,
      "currency": "USD"
    }
  ],
  "free_time": {
    "demurrage_days": 14,
    "detention_days": 7
  },
  "conditional_charges": ["PSS"],
  "conditions": [
    "subject to space",
    "subject to equipment"
  ]
}
```

## 9. Deterministic Validation

The LLM must not be the final authority for quote safety.

```python
if "DESTINATION_THC" in quote.excluded:
    assert quote.has_destination_thc

if quote.conditional_charges:
    quote.requires_review = True

if not quote.final_delivery_charge:
    quote.completeness_score -= 0.15
```

Validate:

- required charges
- currency
- validity
- route consistency
- equipment type
- inclusion/exclusion
- margin threshold
- duplicate quotes
- customer pricing rules
- free-time terms
- conditional surcharges

## 10. Confidence-Based Escalation

Every extracted field should carry provenance.

```json
{
  "charge": "ocean_freight",
  "amount": 2150,
  "currency": "USD",
  "confidence": 0.98,
  "source": {
    "file": "agent_quote_234.pdf",
    "page": 2,
    "text": "O/F USD 2,150 / 40HC"
  }
}
```

Suggested policy:

```text
confidence > .95
→ accept automatically

.80–.95
→ second local model checks

.60–.80
→ cloud model adjudicates

< .60
→ human review
```

## 11. Producer / Verifier Architecture

```text
                   quote
                     │
              ┌──────▼──────┐
              │ Extractor   │
              │ Model A     │
              └──────┬──────┘
                     │ JSON
                     ▼
              ┌─────────────┐
              │ Validator   │
              │ Model B     │
              └──────┬──────┘
                     │
             agree? / \ disagree?
                  /     \
                 ▼       ▼
              accept   escalate
```

Example:

```text
Model A = fast 14B model
Model B = stronger 32B model
```

## 12. Freight Ontology

Canonical concepts should include:

```text
OCEAN_FREIGHT
ORIGIN_THC
DESTINATION_THC
DOCUMENTATION_FEE
BILL_OF_LADING_FEE
CUSTOMS_CLEARANCE
ISPS
BAF
CAF
PSS
GRI
DEMURRAGE
DETENTION
CHASSIS
DELIVERY
WAREHOUSE_HANDLING
DANGEROUS_GOODS
REEFER_SURCHARGE
```

The model's task becomes:

```text
messy human terminology
            ↓
canonical freight ontology
```

## 13. Persistent Alias Learning

Store counterparty-specific terminology.

Example:

```text
Agent:
Shanghai ABC Logistics

Alias:
PCS

Canonical meaning:
PORT_CONGESTION_SURCHARGE

Evidence:
confirmed by operator
```

Then retrieve those conventions before asking Strata to interpret future quotes.

## 14. Local Private AI as a Product Advantage

Freight data may include:

- customer names
- negotiated rates
- shipment volumes
- supplier identities
- commercial invoices
- Bills of Lading
- margins
- contract rates
- destination information
- proprietary pricing

Positioning:

> **Private AI freight operations without sending commercial data to third-party LLM providers.**

Deployment modes:

### Mode A — Hosted

```text
customer
↓
RateScout cloud
↓
your Strata GPUs
```

### Mode B — Private Deployment

```text
customer infrastructure
↓
Docker / Kubernetes
↓
Strata
↓
local model
↓
RateScout
↓
Outlook + TMS
```

### Mode C — Hybrid

```text
local AI
↓
cloud only for ambiguous exceptions
```

## 15. Dual DGX Spark Deployment

Practical split:

```text
DGX Spark #1
─────────────
main local LLM
complex reasoning
document interpretation

DGX Spark #2
─────────────
vision model
embedding model
reranker
secondary verifier
batch jobs
```

For high-value quotes:

```text
Spark 1:
Extractor / reasoner

Spark 2:
Independent verifier
```

```text
Model A result
     │
     ▼
Model B validation
     │
     ├── agree → continue
     │
     └── disagree → review
```

## 16. Pricing

### RateScout Cloud

Potential pricing:

```text
$399–999/month
```

### RateScout Private AI

Potential pricing:

```text
$2,000–5,000+/month
+ implementation
```

Private tier features can include:

- customer-hosted inference
- Strata local model
- private document processing
- local TMS integration
- local Microsoft 365 connectivity
- local audit log
- optional cloud fallback disabled

## 17. Workflow Safety

Do not build an unconstrained agent with direct access to Outlook and the TMS.

Use:

```text
               STRATA
                  │
             reasoning
                  │
                  ▼
       ┌─────────────────────┐
       │ RateScout State     │
       │ Machine             │
       └─────────┬───────────┘
                 │
       ┌─────────┼──────────────┐
       ▼         ▼              ▼
   deterministic approved    prohibited
      tools       tools        actions
       │           │
       ▼           ▼
 automatic      human approval
```

## 18. Actions That Can Be Automated

The agent may automatically:

- read emails
- classify RFQs
- parse quotations
- normalize charges
- query rate history
- request standard rates
- send routine reminders
- identify missing charges
- compare supplier responses
- prepare internal quote drafts
- prepare customer response drafts
- update workflow state
- create internal records

## 19. Actions Requiring Approval

Require human approval for:

- margin below threshold
- carrier booking commitment
- contractual acceptance
- dangerous-goods assumptions
- unusual credit terms
- unusually large quotations
- new carrier/vendor creation
- inconsistent surcharge interpretation
- uncertain customs treatment
- unusual Incoterm assumptions
- customer-specific pricing exceptions
- manual overrides affecting profit

## 20. Recommended Production Stack

```text
FastAPI
PostgreSQL
Redis / task queue
Microsoft Graph / Gmail
       ↓
document processing
       ↓
Strata local inference server
       ↓
structured JSON
       ↓
Pydantic validation
       ↓
freight rules engine
       ↓
PostgreSQL
       ↓
React dashboard
```

Provider abstraction:

```text
LLMProvider
```

Implementations:

```text
StrataProvider
VLLMProvider
OpenAIProvider
AnthropicProvider
GeminiProvider
```

## 21. Responsibility Split

| Task | Local Strata | Cloud | Deterministic Code | Human |
|---|---:|---:|---:|---:|
| Email classification | ✅ | | | |
| Normal PDF extraction | ✅ | | | |
| Excel parsing | | | ✅ | |
| Freight charge extraction | ✅ | | | |
| Customer/carrier matching | ✅ | | ✅ | |
| Standard charge normalization | ✅ | | ✅ | |
| Follow-up drafting | ✅ | | | |
| Quote comparison | ✅ | | ✅ | |
| Mathematical pricing | | | ✅ | |
| Margin enforcement | | | ✅ | |
| Very ambiguous quotation | | ✅ | | |
| Complex multi-document reasoning | ✅ | ✅ fallback | | |
| Unusual legal/commercial language | | ✅ | | |
| High-value commercial approval | | | | ✅ |

## 22. Core Principle

> **Local AI handles volume. Cloud AI handles ambiguity. Deterministic software handles money. Humans handle consequential exceptions.**

The defensible product is not just the local model. It is the combination of:

- freight ontology
- quote normalization
- historical counterparty knowledge
- exception detection
- controlled tool execution
- approval policies
- deterministic pricing
- auditability
- private local inference
- integration with email and TMS systems
