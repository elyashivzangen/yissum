# HUJI Research Monitor — Pipeline Documentation

An automated system that continuously discovers HUJI-affiliated research papers, evaluates their commercial potential using AI, and delivers curated weekly digests to the Technology Transfer Office.

## Quick Links

| Resource | Link |
|----------|------|
| 🌐 **Web App (HTML Viewer)** | https://elyashivzangen.github.io/yissum/papers_reader.html |
| 📊 **All Papers Spreadsheet** | https://docs.google.com/spreadsheets/d/1oyewjr_pojhyBJHXw_yLUrWVvVoFPbd352dAXPqpGyM |
| 📁 **Digest PDFs** | https://github.com/elyashivzangen/yissum/tree/main/digests |
| ⚙️ **GitHub Actions** | https://github.com/elyashivzangen/yissum/actions |
| 🔧 **Repository** | https://github.com/elyashivzangen/yissum |

---


## Table of Contents

1. [What This System Does](#what-this-system-does)
2. [Weekly Automation Schedule](#weekly-automation-schedule)
3. [Data Sources](#data-sources)
4. [AI Scoring System](#ai-scoring-system)
5. [PI & Email Enrichment](#pi--email-enrichment)
6. [Interactive HTML Viewer](#interactive-html-viewer)
7. [Researcher Applicability Dataset](#researcher-applicability-dataset)
8. [Google Sheet Integration](#google-sheet-integration)
9. [Weekly Digest PDF](#weekly-digest-pdf)
10. [RFP Harvester](#rfp-harvester)
11. [Retention & Cleanup](#retention--cleanup)
12. [GitHub Actions Workflows](#github-actions-workflows)
13. [Repository File Reference](#repository-file-reference)
14. [Required Secrets](#required-secrets)
15. [Running Manually](#running-manually)
16. [Architecture Overview](#architecture-overview)

---

## What This System Does

Every Monday, the pipeline automatically:

1. Searches PubMed, Europe PMC, and Semantic Scholar for new papers published by HUJI-affiliated researchers.
2. Runs each paper through a Gemini AI evaluation that scores it across five commercial dimensions.
3. Identifies the Principal Investigator (PI) and attempts to find their email address.
4. Syncs all data to a shared Google Sheet (accessible to the entire TTO team).
5. Generates a self-contained interactive HTML viewer (`papers_reader.html`) with full filtering and search.
6. Generates a curated weekly digest PDF highlighting the 8–12 most commercially promising papers.

Once a month, a second pipeline (`researcher_pipeline.py`) builds researcher-level
profiles: it identifies the top-scoring HUJI researchers, pulls each one's last
3 years of publications, grades them with the same scoring method, and produces
an average applicability score, an AI-written description of their focus area,
and the full list of graded papers per researcher — surfaced in a second
"Researchers" tab in the same `papers_reader.html`. See
[Researcher Applicability Dataset](#researcher-applicability-dataset).

---

## Weekly Automation Schedule

All automation runs on GitHub Actions with no manual intervention required:

| Time (UTC, Mondays) | Workflow | What it does |
|---------------------|----------|--------------|
| 02:00 | `rfp_scrape.yml` | Harvests RFP/grant documents from pharma company websites |
| 06:00 | `papers_pipeline.yml` | Fetches papers, evaluates them, updates Sheet + HTML viewer |
| 09:00 | `weekly_digest.yml` | Reads top papers from Sheet, generates curated PDF digest |
| 07:00 (1st of every 2nd month) | `cleanup.yml` | Removes low-scoring papers to keep the database focused |
| 08:00 (1st of every month) | `researcher_pipeline.yml` | Rebuilds researcher applicability profiles (Researchers Sheet tab + dashboard tab) |

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

Each new paper is evaluated by Gemini (Gemma) AI on **five separate dimensions**, each scored from 1 to 10. The composite score is the sum of all five, giving a scale of **1–50**.

### Model fallback chain & scoring provenance

Strength order, confirmed via benchmark comparison (BenchLM has Gemma 4 31B
beating Gemini 3.1 Flash-Lite 62-49 across benchmark categories; Flash-Lite's
only real edge is speed/cost/context window, not quality):

`gemma-4-31b-it` (31B dense, #3 on the Arena leaderboard) >
`gemma-4-26b-a4b-it` (26B MoE, ~3.8B active/token, #6 Arena) >
`gemini-3.1-flash-lite` (cost/speed tier) > Groq `llama-3.1-8b-instant`
(8B, weakest — last resort only).

**Every call** (the per-paper meta call, all 5 per-paper dimension scores,
and the once-per-researcher applicability summary) tries models in exactly
that order, falling through to the next only on failure — Gemma is the best
available model, so it's preferred everywhere, not just for some call types.

Each paper records whichever model actually scored it in an `eval_model`
column, surfaced as a small 🤖 badge on the card (green = Gemma, yellow =
Groq). Per-model calls are throttled to each model's real free-tier RPM
(~15 RPM for the Gemini/Gemma models, ~30 RPM for Groq) — a model that still
errors out 3 times in a row is benched for 90 seconds, not the rest of the
run, so a transient overload recovers instead of cascading. Gemma calls also
set `thinking_level="minimal"` — Gemma 4's extended-reasoning mode adds real
latency, which is a likely contributor to the 504 DEADLINE_EXCEEDED errors
seen live; turning it off is Google's own documented mitigation.

**Note on Gemma reliability:** the newly-launched (April 2026) Gemma 4
endpoints on the Gemini API are known, as of this writing, to throw frequent
500/503/504 errors under load — this is a widely-reported Google-side
capacity issue, not something fixable from this codebase beyond good
fallback design (which is what the chain above, throttling, and cooldown
all exist for).

### Converging every paper onto Gemma

Because Gemma can still fail (quota, overload) and fall back to Gemini or
Groq, a paper's `eval_model` may not always be Gemma. To converge the whole
dataset onto one consistent model:

- **`--reeval-to-gemma`** (papers_pipeline.py) / **`--reeval-to-gemma`**
  (researcher_pipeline.py) re-score any paper whose `eval_model` isn't Gemma,
  using *only* the two Gemma models (no falling through to Gemini/Groq within
  this pass — that would defeat the point). Papers that still fail are left
  untouched, so it's safe to run repeatedly.
- The paper's previous score and model are preserved in `prev_score` /
  `prev_eval_model` (set once, on the first successful re-eval, so repeated
  runs don't overwrite the original baseline) — the dashboard shows this as
  a small "↑ was N" badge next to the model badge.
- **`reeval_to_gemma.yml`** runs both scripts' reeval mode automatically once
  a day, so the dataset keeps converging without manual intervention. It can
  also be triggered manually, as can `--reeval-to-gemma` via the
  `papers_pipeline.yml` workflow input (`--reeval-groq` remains a deprecated
  CLI alias for the same flag).

### Comparing what each model actually says

`model_comparison_pilot.py` is a manual, one-off analysis tool: it forces
each of the 4 models (bypassing the fallback chain) to independently score
the *same* sample of papers, so you can see the real spread in scores a
paper gets depending on which model evaluates it — not just which model
happened to answer first in production.

```bash
GEMINI_API_KEY=... GOOGLE_SHEET_ID=... [GROQ_API_KEY=...] \
  python model_comparison_pilot.py [--sample-size 5]
```

Cost: `sample_size × 4 models × 6 calls` (the default of 5 papers is 120
calls). Writes `model_comparison_pilot.json` (full per-model results) and
prints a summary table (per-paper score spread, per-model averages/failures).

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

It has two top-level tabs:

- **📄 Papers** — the paper-by-paper view described below.
- **🧑‍🔬 Researchers** — researcher applicability profiles; see [Researcher Applicability Dataset](#researcher-applicability-dataset).

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

## Researcher Applicability Dataset

`researcher_pipeline.py` builds a second, researcher-centric dataset on top of
the paper-level data — for identifying which HUJI researchers consistently
produce applicable, commercially-relevant work.

### Process

1. Loads the current scored papers from the Sheet.
2. Groups them by PI (`pi_full_name`, falling back to `pi`) and ranks
   researchers by their single highest-scoring paper.
3. Takes the **top 20** researchers (`TOP_N_RESEARCHERS`).
4. For each one, queries PubMed by author name + HUJI affiliation for their
   publications from the **last 3 years** (`YEARS_BACK`), capped at
   `MAX_PAPERS_PER_RESEARCHER` (default 15, most recent first).
5. Grades every paper with the exact same Gemini scoring method used by
   `papers_pipeline.py` (`evaluate_paper()` — same five dimensions, same
   1–50 scale). Papers already scored in the main dataset are reused as-is
   instead of being re-graded, to save API calls and keep scores consistent.
6. Computes the researcher's average score across their graded papers, and
   asks Gemini (one combined call) for both a **description** of their research
   focus and a separate **applicability** assessment of the commercial /
   translational potential of their work.
7. Aggregates researcher-level metadata: their **affiliation** and **email**
   (from the paper author records), the union of **field tags** across their
   papers, and the TTO **branches** those fields map to.
8. Writes one profile per researcher — `pi`, `pi_full_name`, `pi_email`,
   `pi_affiliation`, `avg_score`, `paper_count`, `description`, `applicability`,
   `fields`, `branches`, and the full list of graded `papers` (each with its
   own score breakdown, field tags, and `eval_model`) — to a separate
   **Researchers** Sheet tab and to `researchers_data.json`, then regenerates
   `papers_reader.html` with the Researchers tab populated.

### Where it shows up

- **Google Sheet** — a second tab named `Researchers` (created automatically
  the first time the pipeline posts to it, via the `sheet_name` field the
  Apps Script now supports).
- **`papers_reader.html`** — the "🧑‍🔬 Researchers" tab: one card per
  researcher showing their average-score badge, affiliation, email, focus
  description, a highlighted **applicability** summary, aggregated field-tag
  chips, and an expandable "Graded Publications ▾" list where each paper shows
  its field tags, scoring model badge, and its own expandable score breakdown.
  The tab also has **branch filter tabs** (All / Healthcare / Agriculture &
  Food / Exact & Social Sciences) mirroring the Papers tab.
- **`researchers_data.json`** — the raw JSON, same shape as the Sheet rows.

Because this is a much heavier job than the weekly pipeline (a full
publication-history fetch + re-scoring per researcher), it runs on its own
monthly schedule (`researcher_pipeline.yml`) rather than as part of
`papers_pipeline.yml`. It checkpoints after every researcher, so a killed or
timed-out run still keeps whatever profiles it finished.

Researcher grouping is a simple string match on `pi_full_name`/`pi` (the same
approach already used elsewhere in this repo for PI trend analysis) — it does
not do full author disambiguation, so researchers who publish under
inconsistent name variants may be split across multiple entries.

---

## Google Sheet Integration

All paper data is synced to a shared Google Sheet after every pipeline run. This is the canonical data store — `papers_reader.html` and `papers_data.json` are generated *from* the Sheet.

The `Sheet1` tab columns are:

`id` · `title` · `authors` · `journal` · `date` · `url` · `source` · `score` · `summary` · `opportunity` · `fields` · `added_date` · `score_breakdown` · `pi` · `pi_full_name` · `pi_email` · `pi_affiliation` · `eval_model` · `prev_score` · `prev_eval_model`

A second tab, `Researchers`, holds the researcher applicability profiles
(see [Researcher Applicability Dataset](#researcher-applicability-dataset)),
with columns:

`pi` · `pi_full_name` · `pi_email` · `pi_affiliation` · `avg_score` · `paper_count` · `description` · `applicability` · `fields` · `branches` · `papers`

The sync is performed via a Google Apps Script web app (`apps_script.js`). The script receives a full data payload (with an optional `sheet_name`, defaulting to `Sheet1`), clears that sheet (creating it first if it doesn't exist), and rewrites all rows. The deployment URL must be stored as the `APPS_SCRIPT_URL` GitHub secret.

> **Redeploying `apps_script.js`:** because Apps Script runs in your Google
> account, its web-app deployment can only be updated from the Apps Script
> editor — not from CI or this repo. After changing `apps_script.js`, open the
> Sheet → **Extensions → Apps Script**, paste the latest source, then **Deploy →
> Manage deployments → Edit → New version → Deploy**. The `researcher_pipeline`
> checks the deployed script's version via a safe `ping` and skips Sheet writes
> (building the JSON + dashboard only) until it is redeployed, so a stale
> deployment can never overwrite `Sheet1`.

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

Papers are kept indefinitely — there is no age-based expiry. The only thing
that removes a paper is score:

### Bimonthly Cleanup (score-based only)

On the 1st of every second month, `cleanup.py` removes papers scoring below
`LOW_SCORE_THRESHOLD` (default **25**, `score < 25` out of 50), regardless of
how old they are. All papers scoring 25 or above are kept indefinitely.

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

### `researcher_pipeline.yml`

```yaml
trigger: schedule (1st of every month, 08:00 UTC) + workflow_dispatch
inputs:
  top_n: 20 (default)
  years_back: 3 (default)
  max_papers_per_researcher: 15 (default)
secrets: GEMINI_API_KEY, GOOGLE_SHEET_ID, APPS_SCRIPT_URL
outputs: papers_reader.html, researchers_data.json, researcher_pipeline_run.log
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
| `researcher_pipeline.py` | Monthly pipeline: builds researcher applicability profiles (Researchers Sheet tab + dashboard tab) |
| `model_comparison_pilot.py` | Manual, one-off: forces each of the 4 models to independently score the same sample of papers |
| `weekly_digest.py` | Curated PDF digest generator |
| `scrape.py` | RFP/grant document harvester |
| `sync_sheet.py` | One-time utility to push local JSON to the Sheet |
| `cleanup.py` | Bimonthly score-based cleanup of low-scoring papers |
| `apps_script.js` | Google Apps Script for Sheet write access (supports multiple named tabs) |
| `papers_reader.html` | **Generated** — interactive viewer with Papers + Researchers tabs (open in browser) |
| `papers_data.json` | **Generated** — raw JSON for all papers |
| `researchers_data.json` | **Generated** — raw JSON for all researcher applicability profiles |
| `pipeline_run.log` | **Generated** — log of the last pipeline run |
| `researcher_pipeline_run.log` | **Generated** — log of the last researcher pipeline run |
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
| `GEMINI_API_KEY` | pipeline, researcher pipeline, digest, cleanup | Google AI API key for Gemini |
| `GOOGLE_SHEET_ID` | pipeline, researcher pipeline, digest, cleanup | ID from the Google Sheet URL |
| `APPS_SCRIPT_URL` | pipeline, researcher pipeline, cleanup | Deployed Apps Script web app URL |
| `GH_TOKEN` | rfp_scrape | GitHub token (for pushing scraped data) |

---

## Running Manually

### Trigger via GitHub UI

Go to **Actions → Papers Pipeline → Run workflow** and choose `period: week` or `period: month`.

For researcher profiles, go to **Actions → Researcher Pipeline → Run workflow** and optionally override `top_n`, `years_back`, `max_papers_per_researcher`.

### Run locally

```bash
pip install -r requirements.txt

# Run the main pipeline
GEMINI_API_KEY=... GOOGLE_SHEET_ID=... APPS_SCRIPT_URL=... \
  python papers_pipeline.py --period week

# Build researcher applicability profiles
GEMINI_API_KEY=... GOOGLE_SHEET_ID=... APPS_SCRIPT_URL=... \
  python researcher_pipeline.py

# Generate a digest PDF
GEMINI_API_KEY=... GOOGLE_SHEET_ID=... \
  python weekly_digest.py

# Run cleanup
GEMINI_API_KEY=... GOOGLE_SHEET_ID=... APPS_SCRIPT_URL=... \
  python cleanup.py
```

The `pipeline_run.log` / `researcher_pipeline_run.log` files written after each run contain a full trace of what was fetched, evaluated, and synced — check them first if anything looks wrong.

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
7. Once a month, `researcher_pipeline.py` reads the Sheet, picks the top-scoring
   researchers, fetches + grades each one's last 3 years of publications, and
   writes profiles to a second `Researchers` Sheet tab, `researchers_data.json`,
   and the "Researchers" tab of the same `papers_reader.html`.
