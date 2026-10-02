# Publishing OrderInbox AI

Everything is built, tested (36 appliance tests + Odoo module tests against a
real Odoo 18 instance), tagged, and packaged as a Docker image plus an
Odoo Apps Store module. Publishing is three commands — plus one store
submission for the marketplace.

## 1. Push to GitHub

```bash
# create the repo on GitHub first (private until launch, public for the
# partner-facing demo — the source is the marketing)
git remote add origin https://github.com/<your-org>/orderinbox.git
git push origin main
git push origin v0.2.0
```

## 2. Let CI publish the image

The `.github/workflows/ci.yml` workflow:
- runs the test suite on every push/PR
- on any `v*` tag, builds the Docker image and pushes it to GHCR:
  `ghcr.io/<your-org>/orderinbox:v0.2.0` and `:latest`

No tokens needed — the workflow's `GITHUB_TOKEN` already has `packages: write`
permission (declared in the workflow file).

## 2b. Submit the Odoo module to the Apps Store

`addons/orderinbox` is a complete, installable Odoo module (tested on Odoo
18). Submission flow (per Odoo's vendor guidelines, verified Oct 2026):

1. Have an Odoo account with access to the Apps Store
   (https://apps.odoo.com/apps/upload).
2. Link this GitHub repo in the Apps Store dashboard; Odoo scans the repo
   and picks up the module from `addons/orderinbox/` (the repo-root Python
   package has no `__manifest__.py`, so it is not detected as a module).
3. Set the listing: name **"OrderInbox AI — PO Drafts"**, description from
   `static/description/index.html` (hero + screenshots + external-service
   notice already in place), icon + cover generated in
   `static/description/` (icon.png, cover.png, screenshots/).
4. Publish as **free** (the module is the funnel; the appliance subscription
   is the revenue). If you later price it, it must be ≤ the direct-sale
   price, and paid apps may require partner status — check at submission
   time.
5. The listing goes into review (typically days–weeks). Keep the repo's
   default branch updated; store reviews track the linked repo.

The module's `version` field is `18.0.1.0.0` (Odoo 18). For other major
versions, branch the module and bump the prefix (17.0/19.0) — the code is
view-syntax compatible with 17 and 19, but ship per-version branches per
store convention.

## 3. Point users at the right thing

| Audience | What they get |
|---|---|
| End customer (manufacturer) | The landing page (`site/index.html`, deploy to any static host — GitHub Pages works) + a hosted demo URL or `docker compose up` instructions |
| Odoo partner | The two-minute `docker compose up` demo + the **free Apps Store module** they can install in client instances + the outreach email from SALES.md + the commission terms from PARTNERS.md |
| Skeptic | `docker compose up` and three of their own past POs run through the pipeline (the closing move in SALES.md §4) |

## Deployment options once the image is public

```bash
# Option A: hosted by you (Mode A) — one GPU box, many tenants
docker pull ghcr.io/<your-org>/orderinbox:latest
docker run -p 8501:8501 -e WEB_PASSWORD=... -e ODOO_MODE=live \
  -e ODOO_URL=... -e IMAP_USERNAME=... ... ghcr.io/<your-org>/orderinbox:latest

# Option B: on the customer's server (Mode B) — the private-AI story
git clone https://github.com/<your-org>/orderinbox.git && cd orderinbox
docker compose up        # app + local Qwen model, nothing leaves the box

# Option C: local model + cloud fallback (Mode C)
docker run ... -e LLM_MODE=auto \
  -e LLM_CLOUD_BASE_URL=https://api.openai.com/v1 \
  -e LLM_CLOUD_API_KEY=sk-... ghcr.io/<your-org>/orderinbox:latest
```

## Release checklist (every version)

- [ ] `pytest` green locally (appliance: 36 tests)
- [ ] Odoo module tests green (`odoo -d testdb -i orderinbox --test-enable --test-tags orderinbox` on Odoo 18)
- [ ] `docker build` succeeds
- [ ] `docker run -e ORDERINBOX_DEMO=1 ...` boots, seeds, dashboard renders
- [ ] approve one order in the console → draft appears (mock or live)
- [ ] bump version in `orderinbox/__init__.py` + `pyproject.toml` (+ module manifest `version` for Odoo-side changes)
- [ ] tag `vX.Y.Z` and push → GHCR publishes
- [ ] update the landing page if pricing/features changed
- [ ] if the module changed: re-link/refresh the Apps Store listing from the repo

## First-3-implementation tracking (the plan's initial goal)

Keep a simple sheet (or the orders table of your own demo instance) with:

| # | Company | SKUs | POs/mo | Plan | Onboarding fee | Status |
|---|---|---:|---:|---|---:|---|
| 1 | | | | Business | $2,000 | demo scheduled |

When row 3 reaches *shadow mode*, the Odoo milestone is hit — and the
SAP/Dynamics/NetSuite conversation in the plan becomes a real one.
