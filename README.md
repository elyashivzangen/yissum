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

Every week, a second pipeline (`researcher_pipeline.py`) builds researcher-level
profiles: it identifies the top-scoring HUJI researchers, pulls each one's last
3 years of publications, grades them with the same scoring method, and produces
an average applicability score, an AI-written description of their focus area,
and the full list of graded papers per researcher — surfaced in a second
"Researchers" tab in the same `papers_reader.html`. Profiles are additive: an
existing researcher only has newly-published papers added to their profile
each week, never rebuilt from scratch. See
[Researcher Applicability Dataset](#researcher-applicability-dataset).

---

## Weekly Automation Schedule

All automation runs on GitHub Actions with no manual intervention required. Every scheduled workflow can also be triggered manually from the Actions tab (`workflow_dispatch`) — see [Running Manually](#running-manually).

### Daily

| Time (UTC) | Workflow | What it does |
|------------|----------|---------------|
| 04:13 | `reeval_to_gemma.yml` | Re-scores any paper (main sheet + researcher profiles) not yet scored by Gemma, so the dataset keeps converging onto one consistent model as Gemma's availability recovers. See [Converging every paper onto Gemma](#converging-every-paper-onto-gemma). |

### Weekly (Mondays)

| Time (UTC) | Workflow | What it does |
|------------|----------|---------------|
| 02:00 | `rfp_scrape.yml` | Harvests RFP/grant documents from pharma company websites — fully independent of the paper pipeline |
| 06:00 | `papers_pipeline.yml` | Fetches new papers, evaluates them, updates the Sheet + HTML viewer |
| 09:00 | `weekly_digest.yml` | Reads top papers from the Sheet, generates + emails the curated weekly PDF digest |
| 11:00 | `researcher_pipeline.yml` | Adds this week's new papers to the top-20 researchers' profiles (or bootstraps a profile for anyone new to the top 20); researchers who fall out of the top 20 keep their existing profile untouched |

`weekly_digest.yml` and `researcher_pipeline.yml` are deliberately scheduled after `papers_pipeline.yml` (3h and 5h respectively) so they always read that week's freshest data.

### Monthly (1st of the month)

| Time (UTC) | Workflow | What it does |
|------------|----------|---------------|
| 10:00 | `monthly_digest.yml` | Reads a wider window of top papers, generates + emails the curated monthly PDF digest |

### Bimonthly (1st of every 2nd month)

| Time (UTC) | Workflow | What it does |
|------------|----------|---------------|
| 07:00 | `cleanup.yml` | Removes low-scoring papers to keep the dataset focused |

### Manual-only (no schedule)

| Workflow | What it does |
|----------|---------------|
| `model_comparison_pilot.yml` | One-off diagnostic: forces each model in the fallback chain to score the same sample of papers, to compare real scoring behavior across models. See [Comparing what each model actually says](#comparing-what-each-model-actually-says). |
| `weekly_digest_enhanced.yml` | Experimental digest variant, kept around for testing — not part of the regular pipeline. |

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
3. **ORCID** — as a last resort, searches by PI name + HUJI affiliation for a public email.
4. **Cross-paper fallback** — the same PI's email/affiliation often lands on some of their papers but not others (a PubMed record only embeds it in the affiliation free-text some of the time). At HTML-generation time, any paper still missing `pi_email`/`pi_affiliation` is backfilled from that PI's aggregated value in `researchers_data.json` (if they're one of the profiled researchers), rather than only ever depending on that one paper's own source record.

### Result

- `pi` — last name or short identifier found in source data
- `pi_full_name` — full first + last name (from enrichment)
- `pi_email` — email address (shown behind a toggle button in the viewer)

Note: Most academic APIs do not expose author emails publicly. Only a subset of papers (~10–15%) will have an email found directly from their own source record — the cross-paper fallback above recovers a further slice of these for PIs who are also in the Researchers dataset. The backfill process retries missing emails on every pipeline run as new enrichment logic is added.

---

## Interactive HTML Viewer

`papers_reader.html` is a fully self-contained single-file dashboard — no server or internet connection required. Open it directly in a browser.

It has two top-level tabs:

- **📄 Papers** — the paper-by-paper view described below.
- **🧑‍🔬 Researchers** — researcher applicability profiles; see [Researcher Applicability Dataset](#researcher-applicability-dataset).

Next to the tab switcher is a **🏛️ HUJI-primary only** toggle, on by default, shared across both tabs. It hides any paper/researcher whose *first* listed affiliation segment isn't Hebrew University — e.g. a paper whose PI's affiliation string is `"Hadassah Medical Center; Hebrew University of Jerusalem"` (Hadassah listed first) is hidden until the toggle is switched off. Affiliation order comes from whatever order the source (PubMed/EuropePMC/Semantic Scholar) reported it in — this is a heuristic, not a guarantee, since the underlying data is a single joined string rather than a structured, priority-ordered list.

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
- **Main Researcher** — PI full name, with an "Email ▾" toggle button if an email was found (including via the cross-paper fallback described in [PI & Email Enrichment](#pi--email-enrichment))
- **Authors** — the first 3 authors, journal, publication date. Papers with more than 4 authors show `A, B, C, +N more, LastAuthor` — the last author is always visible even collapsed; clicking "+N more" expands to the full list inline. (Papers fetched before author-list truncation was removed only have up to 3 authors persisted — see `--backfill-authors` under [Running Manually](#running-manually) to recover the rest.)
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

Runs weekly. Profiles are **additive** — an existing researcher only ever has
newly-published papers added to their profile; nothing already in it is
re-fetched, re-graded, or replaced.

1. Loads the current scored papers from the Sheet, and the existing
   `researchers_data.json` profiles from the previous run (if any).
2. Groups papers by PI (`pi_full_name`, falling back to `pi`) and ranks
   researchers by their single highest-scoring paper.
3. Takes the **top 20** researchers (`TOP_N_RESEARCHERS`) as this week's
   candidates. A researcher who already has a profile but falls out of the
   top 20 this week **keeps their existing profile as-is** — they're simply
   not re-queried this run, not dropped from the dataset.
4. For each candidate, queries PubMed by author name + HUJI affiliation for
   their publications from the **last 3 years** (`YEARS_BACK`), capped at
   `MAX_PAPERS_PER_RESEARCHER` (default 15, most recent first):
   - **New candidate** (no existing profile): bootstraps a fresh profile
     from this fetch, same as before.
   - **Existing candidate**: diffs the fetch against papers already in
     their profile; only genuinely new papers are graded and appended. If
     nothing new turns up, the existing profile is returned completely
     untouched — no wasted Gemini calls on a quiet week.
5. Grades every new paper with the exact same Gemini scoring method used by
   `papers_pipeline.py` (`evaluate_paper()` — same five dimensions, same
   1–50 scale). Papers already scored in the main dataset are reused as-is
   instead of being re-graded, to save API calls and keep scores consistent.
6. Recomputes the researcher's average score across their **full** paper
   list (old + new), and — only when new papers were actually added — asks
   Gemini (one combined call) for an updated **description** of their
   research focus and a separate **applicability** assessment of the
   commercial / translational potential of their work.
7. Aggregates researcher-level metadata: their **affiliation** and **email**
   (from the paper author records, falling back to the existing profile's
   values), the union of **field tags** across their papers, and the TTO
   **branches** those fields map to.
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
  its field tags and scoring model badge, plus two independently-expandable
  panels: **"Abstract & Summary ▾"** (the AI-generated summary and commercial
  opportunity, and — for papers graded since this field was added, with older
  ones backfilled over subsequent daily reeval runs — the original paper
  abstract) and **"Score Breakdown ▾"** (the same five-dimension breakdown as
  the Papers tab). The tab also has **branch filter tabs** (All / Healthcare /
  Agriculture & Food / Exact & Social Sciences) mirroring the Papers tab, and
  is subject to the same **🏛️ HUJI-primary only** toggle described above.
- **`researchers_data.json`** — the raw JSON, same shape as the Sheet rows.

Because this is a heavier job than the main pipeline (a publication-history
fetch per researcher, plus grading whatever's new), it runs as its own
workflow (`researcher_pipeline.yml`) — scheduled weekly, after
`papers_pipeline.yml` and `weekly_digest.yml` so it always sees that week's
freshest data — rather than as part of `papers_pipeline.yml`. It checkpoints
after every researcher, so a killed or timed-out run still keeps whatever
profiles it finished (both newly-updated ones and untouched carried-forward
ones).

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
6. If SMTP is configured (see below), the generated PDF(s) are emailed as attachments to every address in `digest_recipients.txt`.

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

### Email Delivery

`digest_recipients.txt` (repo root) holds one email address per line — `#` comments and blank lines are ignored — and is read fresh on every digest run. Currently just `elyashiv.zangen@mail.huji.ac.il`; add more addresses by editing the file directly.

Sending is entirely optional and self-disabling: if `SMTP_USER`/`SMTP_PASSWORD` aren't configured (as passed into `weekly_digest.py` — see below), or the recipients file is empty, it logs a clear line and skips emailing — the PDF is still generated and committed exactly as before. The script's env-var interface:

| Name | Type | Default | Notes |
|------|------|---------|-------|
| `SMTP_HOST` | repo variable | `smtp.gmail.com` | SMTP server hostname |
| `SMTP_PORT` | repo variable | `465` | `465` = implicit SSL, anything else = STARTTLS |
| `SMTP_USER` | secret | — | SMTP login/username |
| `SMTP_PASSWORD` | secret | — | SMTP password (an app-specific password for Gmail/Google-backed accounts, which require 2-Step Verification to be enabled to generate one) |
| `MAIL_FROM` | secret | falls back to `SMTP_USER` | "From" address, if different from the login |

**Current wiring** (in `weekly_digest.yml`/`monthly_digest.yml`): sends from a dedicated account, `elyashiv.zangen.ai@gmail.com` (2-Step Verification + App Password enabled), hardcoded as `SMTP_USER`/`MAIL_FROM` directly in the workflow files (not a secret — it's just an address, not sensitive). The App Password itself is stored as the repo secret **`GMAIL_APP_PASSWORD`** and mapped to `SMTP_PASSWORD` in the workflow's `env:` block. `SMTP_HOST`/`SMTP_PORT` are left unset, so the script's `smtp.gmail.com:465` defaults apply.

Repo variables are set the same place as secrets — **Settings → Secrets and variables → Actions → Variables tab** — the difference is variables aren't hidden in logs, so only put non-sensitive values there.

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
  days_back: 90 (default) — actual fetch window in days, for multi-month backfills
  max_results: 300 (default) — max papers requested per source
  backfill_metadata: false (default) — one-off: refresh pi_affiliation/date precision only
  reeval_to_gemma: false (default) — one-off: re-score non-Gemma papers onto Gemma only
  backfill_authors: false (default) — one-off: re-fetch full author lists for existing papers
secrets: GEMINI_API_KEY, GOOGLE_SHEET_ID, APPS_SCRIPT_URL, GROQ_API_KEY
outputs: papers_reader.html, papers_data.json, pipeline_run.log
```

Can be triggered manually from the GitHub Actions tab with `period=month` to do a full 30-day lookback (useful after the pipeline has been down or for initial population). The three boolean inputs are mutually-exclusive one-off modes — each skips fetching new papers entirely and only touches the existing dataset.

### `reeval_to_gemma.yml`

```yaml
trigger: schedule (04:13 UTC daily) + workflow_dispatch
secrets: GEMINI_API_KEY, GOOGLE_SHEET_ID, APPS_SCRIPT_URL, GROQ_API_KEY
outputs: papers_reader.html, papers_data.json, researchers_data.json, reeval_papers_run.log, reeval_researchers_run.log
```

Runs `papers_pipeline.py --reeval-to-gemma` and `researcher_pipeline.py --reeval-to-gemma` back to back, once a day, so both datasets keep converging onto Gemma without manual intervention. See [Converging every paper onto Gemma](#converging-every-paper-onto-gemma).

### `model_comparison_pilot.yml`

```yaml
trigger: workflow_dispatch only (manual, one-off diagnostic — not scheduled)
inputs:
  sample_size: 5 (default)
secrets: GEMINI_API_KEY, GOOGLE_SHEET_ID, APPS_SCRIPT_URL, GROQ_API_KEY
outputs: model_comparison_pilot.json, model_comparison_pilot_run.log
```

Runs `model_comparison_pilot.py`. See [Comparing what each model actually says](#comparing-what-each-model-actually-says).

### `weekly_digest.yml`

```yaml
trigger: schedule (Monday 09:00 UTC) + workflow_dispatch
secrets: GEMINI_API_KEY, GOOGLE_SHEET_ID, GMAIL_APP_PASSWORD (optional; SMTP_USER/MAIL_FROM hardcoded in-workflow)
vars: SMTP_HOST, SMTP_PORT (optional, non-secret)
outputs: digests/HUJI_digest_YYYY_WNN*.pdf, digest_run.log
```

### `monthly_digest.yml`

```yaml
trigger: schedule (1st of every month, 10:00 UTC) + workflow_dispatch
secrets: GEMINI_API_KEY, GOOGLE_SHEET_ID, GMAIL_APP_PASSWORD (optional; SMTP_USER/MAIL_FROM hardcoded in-workflow)
vars: SMTP_HOST, SMTP_PORT (optional, non-secret)
outputs: digests/HUJI_digest_YYYY_MNN*.pdf, digest_run.log
```

Both run `weekly_digest.py` (the latter with `--monthly`) — see [Weekly Digest PDF](#weekly-digest-pdf).

### `cleanup.yml`

```yaml
trigger: schedule (1st of every 2nd month, 07:00 UTC) + workflow_dispatch
secrets: GEMINI_API_KEY, GOOGLE_SHEET_ID, APPS_SCRIPT_URL
outputs: papers_reader.html, papers_data.json, cleanup_run.log
```

### `researcher_pipeline.yml`

```yaml
trigger: schedule (every Monday, 11:00 UTC) + workflow_dispatch
inputs:
  top_n: 20 (default)
  years_back: 3 (default)
  max_papers_per_researcher: 15 (default)
  papers_snapshot_ref: '' (default) — one-off backfill, see below
secrets: GEMINI_API_KEY, GOOGLE_SHEET_ID, APPS_SCRIPT_URL, GROQ_API_KEY
outputs: papers_reader.html, researchers_data.json, researcher_pipeline_run.log
note: additive — existing profiles are updated with new papers, not rebuilt from scratch
```

**Backfilling past weeks** (`papers_snapshot_ref` input): since candidate
selection normally reads the *live* Sheet, running the workflow multiple
times today always recomputes the same top-20 and finds nothing new to add.
To reconstruct history — e.g. running "as if" this were 8 weeks ago, 7 weeks
ago, etc. — set `papers_snapshot_ref` to a past git commit SHA that touched
`papers_data.json` (`papers_pipeline.yml` commits one weekly). The workflow
extracts that commit's `papers_data.json` via `git show <ref>:papers_data.json`
and passes it to `researcher_pipeline.py --papers-snapshot <file>`, which
uses it only for top-N candidate selection and known-paper score reuse —
everything else (real PubMed fetch, real Gemini grading, writing to the
live Sheet/`researchers_data.json`) behaves exactly as a normal run. Run
these oldest-to-newest, one at a time, so each backfill run merges onto the
previous one correctly. Requires the checkout to have full git history
(`fetch-depth: 0`, already set in this workflow) since old commits aren't
reachable from a shallow clone.

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
| `researcher_pipeline.py` | Weekly pipeline: builds/updates researcher applicability profiles (Researchers Sheet tab + dashboard tab), additively |
| `model_comparison_pilot.py` | Manual, one-off: forces each of the 4 models to independently score the same sample of papers |
| `weekly_digest.py` | Curated PDF digest generator (weekly + monthly modes); also emails the PDF(s) if SMTP is configured |
| `digest_recipients.txt` | Email addresses (one per line) that receive the digest PDF(s) |
| `scrape.py` | RFP/grant document harvester |
| `sync_sheet.py` | One-time utility to push local JSON to the Sheet |
| `cleanup.py` | Bimonthly score-based cleanup of low-scoring papers |
| `apps_script.js` | Google Apps Script for Sheet write access (supports multiple named tabs) |
| `papers_reader.html` | **Generated** — interactive viewer with Papers + Researchers tabs (open in browser) |
| `papers_data.json` | **Generated** — raw JSON for all papers |
| `researchers_data.json` | **Generated** — raw JSON for all researcher applicability profiles |
| `model_comparison_pilot.json` | **Generated** — per-model score comparison from the last pilot run |
| `pipeline_run.log` | **Generated** — log of the last pipeline run |
| `researcher_pipeline_run.log` | **Generated** — log of the last researcher pipeline run |
| `reeval_papers_run.log` | **Generated** — log of the last daily papers reeval-to-Gemma pass |
| `reeval_researchers_run.log` | **Generated** — log of the last daily researcher reeval-to-Gemma pass |
| `model_comparison_pilot_run.log` | **Generated** — log of the last model comparison pilot run |
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
| `GEMINI_API_KEY` | pipeline, researcher pipeline, reeval, model comparison pilot, digest, cleanup | Google AI API key for Gemini/Gemma |
| `GOOGLE_SHEET_ID` | pipeline, researcher pipeline, reeval, model comparison pilot, digest, cleanup | ID from the Google Sheet URL |
| `APPS_SCRIPT_URL` | pipeline, researcher pipeline, reeval, model comparison pilot, cleanup | Deployed Apps Script web app URL |
| `GROQ_API_KEY` | pipeline, researcher pipeline, reeval, model comparison pilot | Groq API key — last-resort fallback model when every Gemini/Gemma model in the chain fails |
| `GMAIL_APP_PASSWORD` | digest (weekly + monthly) | App Password for the dedicated `elyashiv.zangen.ai@gmail.com` digest-sender account (2-Step Verification enabled); optional — sending is skipped if unset. `SMTP_USER`/`MAIL_FROM` are hardcoded to that address directly in the two digest workflow files, not stored as secrets |
| `GH_TOKEN` | rfp_scrape | GitHub token (for pushing scraped data) |

Also configurable as non-secret **repo variables** (same Settings page, "Variables" tab): `SMTP_HOST` (default `smtp.gmail.com`), `SMTP_PORT` (default `465`). See [Email Delivery](#email-delivery) for details.

---

## Running Manually

### Trigger via GitHub UI

Go to **Actions → Papers Pipeline → Run workflow** and choose `period: week` or `period: month`, or enable one of the one-off boolean inputs (`backfill_metadata`, `reeval_to_gemma`, `backfill_authors`) to run just that pass over the existing dataset without fetching new papers.

For researcher profiles, go to **Actions → Researcher Pipeline → Run workflow** and optionally override `top_n`, `years_back`, `max_papers_per_researcher`.

To force the daily Gemma-convergence pass or the model comparison pilot on demand, use **Actions → Daily Reeval to Gemma → Run workflow** or **Actions → Model Comparison Pilot → Run workflow** (the latter takes a `sample_size` input).

### Run locally

```bash
pip install -r requirements.txt

# Run the main pipeline
GEMINI_API_KEY=... GOOGLE_SHEET_ID=... APPS_SCRIPT_URL=... \
  python papers_pipeline.py --period week

# One-off: re-fetch full author lists for existing papers (no new fetch/scoring)
GEMINI_API_KEY=... GOOGLE_SHEET_ID=... APPS_SCRIPT_URL=... \
  python papers_pipeline.py --backfill-authors

# One-off: re-score any paper not currently scored by Gemma
GEMINI_API_KEY=... GOOGLE_SHEET_ID=... APPS_SCRIPT_URL=... GROQ_API_KEY=... \
  python papers_pipeline.py --reeval-to-gemma

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
7. Every week, `researcher_pipeline.py` reads the Sheet, picks the top-scoring
   researchers, and fetches + grades new publications for each — adding them
   to that researcher's existing profile (or bootstrapping a new one) rather
   than rebuilding from scratch — writing to a second `Researchers` Sheet
   tab, `researchers_data.json`, and the "Researchers" tab of the same
   `papers_reader.html`.
