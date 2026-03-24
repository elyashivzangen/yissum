# HUJI Research Monitor — Pipeline Documentation

An automated system that continuously discovers HUJI-affiliated research papers, evaluates their commercial potential using AI, and delivers curated weekly digests to the Technology Transfer Office.

---

## Table of Contents

1. [What This System Does](#what-this-system-does)
2. [Weekly Automation Schedule](#weekly-automation-schedule)
3. [Data Sources](#data-sources)
4. [AI Scoring System](#ai-scoring-system)
5. [PI & Email Enrichment](#pi--email-enrichment)
6. [Interactive HTML Viewer](#interactive-html-viewer)
7. [Google Sheet Integration](#google-sheet-integration)
8. [Weekly Digest PDF](#weekly-digest-pdf)
9. [RFP Harvester](#rfp-harvester)
10. [Retention & Cleanup](#retention--cleanup)
11. [GitHub Actions Workflows](#github-actions-workflows)
12. [Repository File Reference](#repository-file-reference)
13. [Required Secrets](#required-secrets)
14. [Running Manually](#running-manually)
15. [Architecture Overview](#architecture-overview)

---

## What This System Does

Every Monday, the pipeline automatically:

1. Searches PubMed, Europe PMC, and Semantic Scholar for new papers published by HUJI-affiliated researchers.
2. Runs each paper through a Gemini AI evaluation that scores it across five commercial dimensions.
3. Identifies the Principal Investigator (PI) and attempts to find their email address.
4. Syncs all data to a shared Google Sheet (accessible to the entire TTO team).
5. Generates a self-contained interactive HTML viewer (`papers_reader.html`) with full filtering and search.
6. Generates a curated weekly digest PDF highlighting the 8–12 most commercially promising papers.

---

## Weekly Automation Schedule

All automation runs on GitHub Actions with no manual intervention required:

| Time (UTC, Mondays) | Workflow | What it does |
|---------------------|----------|--------------|
| 02:00 | `rfp_scrape.yml` | Harvests RFP/grant documents from pharma company websites |
| 06:00 | `papers_pipeline.yml` | Fetches papers, evaluates them, updates Sheet + HTML viewer |
| 09:00 | `weekly_digest.yml` | Reads top papers from Sheet, generates curated PDF digest |
| 07:00 (1st of every 2nd month) | `cleanup.yml` | Removes old low-scoring papers to keep the database fresh |

The digest workflow is intentionally scheduled 3 hours after the pipeline to ensure it always reads the freshest data.

---

## Data Sources

The pipeline queries three independent academic databases to maximise coverage:

### PubMed (via NCBI E-Utilities API)
- Affiliation query: `"Hebrew University"[Affiliation] OR "Hadassah"[Affiliation]`
- Returns: title, authors, journal, publication date, PMID
- PI enrichment: fetches full XML to extract emails from author affiliation strings

### Europe PMC (via EBI API)
- Affiliation query: `AFF:"Hebrew University of Jerusalem" OR AFF:"Hadassah"`
- Returns: same fields plus full abstract and detailed author affiliation records

### Semantic Scholar (via Official API)
- Text query: `"Hebrew University of Jerusalem"`
- Post-filters results by checking author affiliation strings for HUJI keywords
- Returns: Semantic Scholar ID, DOI, venue, abstract, publication year

### HUJI Affiliation Keywords

The system recognises any of these as a HUJI paper:

- Hebrew University of Jerusalem
- Hebrew University
- Hadassah
- Einstein Institute of Mathematics
- Silberman Institute

### Deduplication

After fetching from all three sources, papers are deduplicated by normalising their titles (lowercased, punctuation removed). Only genuinely new papers not already in the Google Sheet are sent for evaluation.

---

## AI Scoring System

Each new paper is evaluated by Gemini AI (`gemma-3-27b-it`) on **five separate dimensions**, each scored from 1 to 10. The composite score is the sum of all five, giving a scale of **1–50**.

| Dimension | What it measures | Max |
|-----------|-----------------|-----|
| **Novelty** | Scientific innovation compared to prior art; is this a genuinely new finding or method? | 10 |
| **Commercial Potential** | How clearly does this translate to a licensable technology, product, or service? | 10 |
| **Market Size** | Size and accessibility of the addressable market for a derived product | 10 |
| **Tech Readiness (TRL)** | Distance from lab to real-world application; near-market scores higher | 10 |
| **IP Strength** | Likely patentability and defensibility of the core innovation | 10 |
| **Total** | | **50** |

### Score Colour Bands

| Range | Colour | Interpretation |
|-------|--------|---------------|
| 38–50 | 🟢 Green | Strong commercial potential — prioritise for TTO outreach |
| 28–37 | 🟡 Yellow | Moderate potential — worth monitoring |
| 0–27 | 🔴 Red | Lower immediate potential |

### What Else Gemini Generates

In addition to scores, for each paper Gemini produces:
- **Summary** — 2-sentence plain-English explanation of what the research does
- **Commercial opportunity** — 1-sentence description of the commercial angle
- **Per-parameter reasoning** — A short justification for each of the five scores (visible in the Score Breakdown panel in the viewer)
- **Field tags** — 1 to 4 tags from the standardised tag list (see below)

### Field Tags

Papers are tagged with one or more of 18 field categories:

`Drug Discovery` · `Medical Device` · `Diagnostics` · `Vaccines` · `AgriTech` · `FoodTech` · `Materials` · `Clean Energy` · `Software/AI` · `Quantum` · `Neuroscience` · `Genomics` · `Imaging` · `Synthetic Biology` · `Proteomics` · `Immunology` · `Clinical` · `Other`

---

## PI & Email Enrichment

For every paper, the system attempts to identify the most senior HUJI-affiliated researcher (the PI) and find their contact email.

### Strategy

1. **PubMed papers** — fetches the full XML record and scans author affiliation strings for HUJI keywords, working backwards from the last author. Extracts both full name and email (if present in the affiliation field).
2. **All papers with a DOI** — queries the CrossRef API for the corresponding author's email as a secondary fallback.

### Result

- `pi` — last name or short identifier found in source data
- `pi_full_name` — full first + last name (from enrichment)
- `pi_email` — email address (shown behind a toggle button in the viewer)

Note: Most academic APIs do not expose author emails publicly. Only a subset of papers (~10–15%) will have an email found. The backfill process retries missing emails on every pipeline run as new enrichment logic is added.

---

## Interactive HTML Viewer

`papers_reader.html` is a fully self-contained single-file dashboard — no server or internet connection required. Open it directly in a browser.

### Controls

**Search** — Free-text search across paper titles, AI summaries, and commercial opportunity descriptions. Results update instantly.

**Sort** — Sort all papers by Score (highest first) or by Date (most recently published first).

**Period filter** — Filters by paper *publication date*:
- All time
- Published in the last 7 days
- Published in the last 30 days

Each chip shows the live count of matching papers, updating as other filters change.

**Score slider** — Set a minimum composite score (0–50). The label shows the current threshold and live matching count, e.g. `28+ /50 · 45`.

**Parameter filter** — Select a specific dimension (Novelty, Commercial Potential, Market Size, Tech Readiness, IP Strength) and set a minimum score for that dimension (1–10). Useful for finding, for example, all papers where IP Strength ≥ 7.

**Field filter** — Filter by any of the 18 field tags. Each chip shows the live count of papers in that field given the other active filters.

### Paper Cards

Each card displays:

- **Title** and **score badge** (green/yellow/red)
- **Main Researcher** — PI full name, with an "Email ▾" toggle button if an email was found
- **Authors** — first three authors, journal, publication date
- **AI Summary** — 2-sentence plain-English description
- **Commercial Opportunity** — highlighted in a purple box
- **Field tags**
- **Open Paper** link (to PubMed, Europe PMC, or Semantic Scholar)
- **Score Breakdown ▾** — expandable panel showing all five parameter scores with colour-coded bars and Gemini's reasoning for each score

---

## Google Sheet Integration

All paper data is synced to a shared Google Sheet after every pipeline run. This is the canonical data store — `papers_reader.html` and `papers_data.json` are generated *from* the Sheet.

The Sheet columns are:

`id` · `title` · `authors` · `journal` · `date` · `url` · `source` · `score` · `summary` · `opportunity` · `fields` · `added_date` · `score_breakdown` · `pi` · `pi_full_name` · `pi_email`

The sync is performed via a Google Apps Script web app (`apps_script.js`). The script receives a full data payload, clears the sheet, and rewrites all rows. The deployment URL must be stored as the `APPS_SCRIPT_URL` GitHub secret.

---

## Weekly Digest PDF

Every Monday at 09:00 UTC, `weekly_digest.py` runs and produces a PDF in `digests/HUJI_digest_{YEAR}_W{WEEK}.pdf`.

### Process

1. Loads all papers added to the Sheet in the last 7 days.
2. Takes the top 20 by score.
3. Sends them to Gemini (`gemini-2.5-flash`, fallback `gemini-2.0-flash`) with a curation prompt asking it to select the 8–12 most commercially promising and write investor-facing headlines.
4. Gemini returns:
   - **Executive Summary** — 3–4 sentence overview of the week's standout research themes
   - **Selected papers** — each with a commercial headline (max 20 words) and a "Why Now" justification (1–2 sentences)
5. PDF is generated using ReportLab and committed to the `digests/` folder.

### PDF Contents

- Header: HUJI Research Monitor, week number and date
- Executive Summary section
- One card per selected paper:
  - Title, score badge, PI name
  - Field tags
  - AI-written commercial headline
  - "Why Now" section
  - "Commercial Angle" section
  - Direct URL to the paper
- Footer: generation timestamp and model credit

---

## RFP Harvester

`scrape.py` runs every Monday at 02:00 UTC and harvests RFP / grant documents from pharmaceutical company websites.

**Sources currently configured:**
- Pfizer Competitive Grants
- Bayer Grants4Targets (Japan)
- Bayer Open Innovation

**What it does:**
1. Visits each landing page and extracts links to PDF or Word documents.
2. Downloads each document (deduplicates by SHA-1 hash, stored in `data/`).
3. Parses the first page of each PDF for issued/deadline dates.
4. Merges results into `latest_rfps.json`.

Each entry in `latest_rfps.json` includes the portal name, document URL, posted date, deadline, and a snippet from page 1.

---

## Retention & Cleanup

### Pipeline Retention (90 days)

At the end of every pipeline run, papers older than **90 days** (by `added_date`) are removed before writing back to the Sheet. This prevents the Sheet from growing without bound.

### Bimonthly Cleanup (selective)

On the 1st of every second month, `cleanup.py` runs a more targeted removal:

- Removes papers where: **age > 60 days AND score < 28**
- Keeps all recent papers regardless of score
- Keeps all high-scoring papers (≥ 28) regardless of age

This means high-potential papers are retained as long-term references, while older low-scoring papers are pruned sooner.

---

## GitHub Actions Workflows

### `papers_pipeline.yml`

```yaml
trigger: schedule (Monday 06:00 UTC) + workflow_dispatch
inputs:
  period: week (default) | month
secrets: GEMINI_API_KEY, GOOGLE_SHEET_ID, APPS_SCRIPT_URL
outputs: papers_reader.html, papers_data.json, pipeline_run.log
```

Can be triggered manually from the GitHub Actions tab with `period=month` to do a full 30-day lookback (useful after the pipeline has been down or for initial population).

### `weekly_digest.yml`

```yaml
trigger: schedule (Monday 09:00 UTC) + workflow_dispatch
secrets: GEMINI_API_KEY, GOOGLE_SHEET_ID
outputs: digests/HUJI_digest_YYYY_WNN.pdf, digest_run.log
```

### `cleanup.yml`

```yaml
trigger: schedule (1st of every 2nd month, 07:00 UTC) + workflow_dispatch
secrets: GEMINI_API_KEY, GOOGLE_SHEET_ID, APPS_SCRIPT_URL
outputs: papers_reader.html, papers_data.json, cleanup_run.log
```

### `rfp_scrape.yml`

```yaml
trigger: schedule (Monday 02:00 UTC) + workflow_dispatch
secrets: GH_TOKEN
outputs: latest_rfps.json, data/*.pdf
```

---

## Repository File Reference

| File | Description |
|------|-------------|
| `papers_pipeline.py` | Main pipeline: fetch, evaluate, enrich, sync, generate HTML |
| `weekly_digest.py` | Curated PDF digest generator |
| `scrape.py` | RFP/grant document harvester |
| `sync_sheet.py` | One-time utility to push local JSON to the Sheet |
| `cleanup.py` | Bimonthly selective retention cleanup |
| `apps_script.js` | Google Apps Script for Sheet write access |
| `papers_reader.html` | **Generated** — interactive viewer (open in browser) |
| `papers_data.json` | **Generated** — raw JSON for all papers |
| `pipeline_run.log` | **Generated** — log of the last pipeline run |
| `digest_run.log` | **Generated** — log of the last digest run |
| `latest_rfps.json` | **Generated** — latest harvested RFP documents |
| `digests/` | Folder of all historical digest PDFs |
| `data/` | Downloaded RFP PDF documents |
| `.github/workflows/` | GitHub Actions workflow definitions |
| `requirements.txt` | Python dependencies |
| `CLAUDE.md` | AI assistant instructions for this project |

---

## Required Secrets

Configure these in the GitHub repository under **Settings → Secrets and variables → Actions**:

| Secret | Used by | Description |
|--------|---------|-------------|
| `GEMINI_API_KEY` | pipeline, digest, cleanup | Google AI API key for Gemini |
| `GOOGLE_SHEET_ID` | pipeline, digest, cleanup | ID from the Google Sheet URL |
| `APPS_SCRIPT_URL` | pipeline, cleanup | Deployed Apps Script web app URL |
| `GH_TOKEN` | rfp_scrape | GitHub token (for pushing scraped data) |

---

## Running Manually

### Trigger via GitHub UI

Go to **Actions → Papers Pipeline → Run workflow** and choose `period: week` or `period: month`.

### Run locally

```bash
pip install -r requirements.txt

# Run the main pipeline
GEMINI_API_KEY=... GOOGLE_SHEET_ID=... APPS_SCRIPT_URL=... \
  python papers_pipeline.py --period week

# Generate a digest PDF
GEMINI_API_KEY=... GOOGLE_SHEET_ID=... \
  python weekly_digest.py

# Run cleanup
GEMINI_API_KEY=... GOOGLE_SHEET_ID=... APPS_SCRIPT_URL=... \
  python cleanup.py
```

The `pipeline_run.log` file written after each run contains a full trace of what was fetched, evaluated, and synced — check it first if anything looks wrong.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                   GitHub Actions                    │
│  Mon 02:00  Mon 06:00  Mon 09:00  1st/2mo 07:00    │
│  rfp_scrape pipeline   digest     cleanup           │
└────┬────────────┬──────────┬─────────────┬──────────┘
     │            │          │             │
     ▼            ▼          │             ▼
  data/*.pdf   PubMed     Google        Sheet
  latest_      EuropePMC  Sheet ────►  (cleanup)
  rfps.json    Semantic   (top 20) │
               Scholar       │     │
                  │           ▼     ▼
                  │       Gemini  papers_
                  │      curation reader.html
                  ▼           │   papers_
              Gemini AI       │   data.json
           (5-dim scoring)    ▼
                  │      HUJI_digest
                  ▼      _YYYY_WNN.pdf
             Google Sheet
           (canonical store)
                  │
                  ▼
           papers_reader.html
           papers_data.json
           (committed to repo)
```

**Data flow in brief:**
1. Papers are fetched from three academic APIs and filtered for HUJI affiliation.
2. New papers are scored by Gemini across 5 commercial dimensions (1–50 scale).
3. PI contact info is enriched via PubMed XML and CrossRef.
4. Everything is written to the Google Sheet (shared with TTO team).
5. An interactive HTML viewer and JSON file are generated and committed to the repo.
6. Three hours later, the digest script reads the top papers and generates a curated PDF.
