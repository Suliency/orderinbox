# Publishing OrderInbox AI

Everything is built, tested (29 tests), tagged (`v0.1.0`), and packaged as a
Docker image. Publishing is three commands.

## 1. Push to GitHub

```bash
# create the repo on GitHub first (private until launch, public for the
# partner-facing demo — the source is the marketing)
git remote add origin https://github.com/<your-org>/orderinbox.git
git push origin main
git push origin v0.1.0
```

## 2. Let CI publish the image

The `.github/workflows/ci.yml` workflow:
- runs the test suite on every push/PR
- on any `v*` tag, builds the Docker image and pushes it to GHCR:
  `ghcr.io/<your-org>/orderinbox:v0.1.0` and `:latest`

No tokens needed — the workflow's `GITHUB_TOKEN` already has `packages: write`
permission (declared in the workflow file).

## 3. Point users at the right thing

| Audience | What they get |
|---|---|
| End customer (manufacturer) | The landing page (`site/index.html`, deploy to any static host — GitHub Pages works) + a hosted demo URL or `docker compose up` instructions |
| Odoo partner | The two-minute `docker compose up` demo + the outreach email from SALES.md + the commission terms from PARTNERS.md |
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

- [ ] `pytest` green locally
- [ ] `docker build` succeeds
- [ ] `docker run -e ORDERINBOX_DEMO=1 ...` boots, seeds, dashboard renders
- [ ] approve one order in the console → draft appears (mock or live)
- [ ] bump version in `orderinbox/__init__.py` + `pyproject.toml`
- [ ] tag `vX.Y.Z` and push → GHCR publishes
- [ ] update the landing page if pricing/features changed

## First-3-implementation tracking (the plan's initial goal)

Keep a simple sheet (or the orders table of your own demo instance) with:

| # | Company | SKUs | POs/mo | Plan | Onboarding fee | Status |
|---|---|---:|---:|---|---:|---|
| 1 | | | | Business | $2,000 | demo scheduled |

When row 3 reaches *shadow mode*, the Odoo milestone is hit — and the
SAP/Dynamics/NetSuite conversation in the plan becomes a real one.
