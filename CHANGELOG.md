# Changelog

This repo had no formal versioning before now. This file starts that
practice: `1.0.0` marks the reliability baseline already on `main` going
into today's session (checkpointing, model fallback, `-u` unbuffered
output — see `SESSION_HANDOFF.md` for that history). Everything below is
what changed today.

## [1.2.0] - 2026-07-03

### Added
- **Scoring provenance (`eval_model`)** — every paper now records which model
  actually scored it (Gemma vs. `groq:…`), shown as a 🤖 badge on each card
  (green = Gemma, yellow = Groq).
- **`--reeval-groq` mode** (CLI flag + `reeval_groq` workflow input) — re-scores
  Groq-graded papers on Gemma once quota is available; re-fetches abstracts by
  id and leaves papers untouched if Gemma is still unavailable, so it is safe
  to run repeatedly.
- **Enriched Researcher profiles & dashboard tab**: affiliation, email, a
  distinct AI **applicability** summary (separate from the focus description),
  aggregated **field-tag chips** and TTO **branches**, per-paper expandable
  score breakdowns with model badges, and **branch filter tabs** in the
  Researchers view.

### Fixed
- `cleanup.py` was POSTing `{"papers": …}` — a payload shape the Apps Script
  ignores — so its sheet write silently did nothing; it now reuses
  `papers_pipeline.save_to_sheet` (correct `replace_all` payload + full schema).
- `sync_sheet.py` had a stale hard-coded column list that would have dropped
  `pi_full_name`/`pi_email`/`pi_affiliation`/`eval_model` on write; it now
  reuses `papers_pipeline.save_to_sheet`.
- `weekly_digest_enhanced.py` read PI-trend data from a non-existent
  `sheet=Papers` tab (returned nothing); fixed to read `Sheet1` via `gid=0`.

## [1.1.0] - 2026-07-02

### Added
- **Groq fallback for paper evaluation** (`llama-3.1-8b-instant`) — tried after
  all Gemini/Gemma models fail. Runs on separate infrastructure from Google,
  so it's unaffected by Gemini-side outages, with a much more generous free
  tier (30 RPM / 14,400 requests/day).
- **Main-researcher affiliation** displayed on each paper card, extracted from
  the paper's own author list (in document order) — the data was already
  being fetched to identify the PI but was previously discarded.
- **Full publication dates from PubMed** (`YYYY-MM-DD` when available) instead
  of year-only, via a new date parser that reads `PubDate/Month`/`Day`.
- **One-off metadata backfill** (`--backfill-metadata` CLI flag /
  `backfill_metadata` workflow input) — refreshes affiliation and date
  precision for papers already in the sheet without re-running LLM
  evaluation. Used once to backfill all 279 papers live at the time.

### Fixed
- Removed the dead `gemma-4-4b-it` model and replaced `gemini-2.5-flash`
  with `gemini-3.1-flash-lite` in the evaluation fallback chain (both
  confirmed dead/inferior via live run logs and the rate-limit dashboard).
- `save_to_sheet()` now retries transient Apps Script webhook failures
  (3 attempts, backoff) instead of crashing the whole run on one blip.
- A failed checkpoint mid-run no longer aborts the remaining evaluation
  queue — it logs a warning and the next checkpoint retries.
- CI was reporting false "success" on hard crashes because `python | tee`
  masked the real exit code; added `set -o pipefail` so failures now
  actually fail the workflow run.
- **Circuit breaker**: a model that fails 3x in a row within a run is
  skipped for the rest of that run instead of being retried on every call —
  cut a 3-hour, timeout-killed run down to ~35 minutes once Gemma started
  having a bad day.
- Fixed the pipeline's final "commit generated output" step, which was
  failing in two different ways over the course of the day: first a
  non-fast-forward push race against other concurrent commits to `main`,
  then (after switching to `git rebase`) real merge conflicts in the fully
  regenerated `pipeline_run.log`. Both are resolved by using
  `git reset --soft origin/main` before committing, which can't conflict
  since it never tries to line-merge the regenerated files.
- Kicked a stuck GitHub Pages deployment queue (unrelated timeout caused by
  a burst of same-day commits) so the live site reflects the latest data.

### Changed
- The paper-evaluation fallback chain is now:
  `gemma-4-31b-it → gemma-4-26b-a4b-it → gemini-3.1-flash-lite → Groq llama-3.1-8b-instant`.
