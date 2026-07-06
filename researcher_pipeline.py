#!/usr/bin/env python3
"""
HUJI Researcher Applicability Pipeline
- Selects the top researchers from the existing scored-papers dataset
- Fetches each researcher's PubMed publication history for the last N years
- Grades every paper with the same scoring method as papers_pipeline.py
- Writes a per-researcher profile (avg score, description, graded papers) to
  a separate "Researchers" Google Sheet tab and researchers_data.json
- Regenerates papers_reader.html with the Researchers tab populated

Runs weekly. Profiles are additive, not rebuilt from scratch each run: a
researcher already in researchers_data.json only has newly-published papers
added to their existing profile (existing papers are never re-fetched,
re-graded, or replaced); a researcher new to this week's top-N candidate
list gets a fresh profile bootstrapped the same way as before. A researcher
who falls out of the top-N list keeps their existing profile untouched
rather than being dropped.
"""

import argparse
import csv
import io
import json
import os
import time
import datetime
import xml.etree.ElementTree as ET
from pathlib import Path
import requests

import papers_pipeline as pp

# ── Config ─────────────────────────────────────────────────────────────────────
GOOGLE_SHEET_ID  = os.environ["GOOGLE_SHEET_ID"]
APPS_SCRIPT_URL  = os.environ["APPS_SCRIPT_URL"]
RESEARCHERS_SHEET_NAME = "Researchers"
OUTPUT_JSON      = pp.RESEARCHERS_JSON

# apps_script.js must be redeployed to a version that understands sheet_name
# routing before we're allowed to post anything — older deployments silently
# ignore sheet_name and always overwrite Sheet1 (the main papers tab) instead
# of the intended "Researchers" tab. See check_apps_script_version().
REQUIRED_SCRIPT_VERSION = 2

TOP_N_RESEARCHERS         = int(os.environ.get("TOP_N_RESEARCHERS", "20"))
YEARS_BACK                = int(os.environ.get("YEARS_BACK", "3"))
MAX_PAPERS_PER_RESEARCHER = int(os.environ.get("MAX_PAPERS_PER_RESEARCHER", "15"))


def _parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--top-n", type=int, default=None)
    p.add_argument("--reeval-to-gemma", action="store_true",
                   help="One-off: re-score papers in the existing researchers_data.json "
                        "that aren't currently scored by Gemma (skips PubMed "
                        "fetch/candidate selection entirely — works off the committed "
                        "file). Safe to run repeatedly; papers that still fail on "
                        "Gemma are left untouched.")
    p.add_argument("--papers-snapshot", type=str, default=None,
                   help="One-off: select this run's top-N candidates (and known-paper "
                        "reuse lookups) from a local papers_data.json snapshot instead "
                        "of the live Sheet — for backfilling researcher profiles as if "
                        "this run happened at a past point in time (e.g. a historical "
                        "git commit's papers_data.json). Everything else (PubMed fetch, "
                        "grading, writing to the Sheet/researchers_data.json) is unchanged.")
    return p.parse_known_args()[0]


ARGS = _parse_args()
if ARGS.top_n:
    TOP_N_RESEARCHERS = ARGS.top_n

RESEARCHER_PROMPT = """You are a life-science and deep-tech investment analyst.
Below are titles and summaries of recent papers by one Hebrew University of
Jerusalem researcher.

{paper_list}

Return a JSON object (no markdown) with exactly these keys:
- description: 2-4 sentence plain-English description of this researcher's
  focus area(s) and the kind of research they do.
- applicability: 2-3 sentence assessment of the commercial / translational
  applicability of their work — what products, licensable technologies, or
  industry partnerships it could lead to, and how near-term that is.
"""


# ── Sheet I/O for the Researchers tab ───────────────────────────────────────────

def check_apps_script_version():
    """Return True if the deployed apps_script.js supports sheet_name routing.

    A "ping" action is a safe no-op on every version of the script (old
    deployments fall through their replace_all check and never touch any
    sheet), so this is always safe to call. Older deployments respond
    without a "version" field, or don't understand "ping" at all — in
    either case Sheet writes must be skipped, since a sheet_name payload
    would otherwise silently overwrite Sheet1 instead of the intended tab.
    The rest of the pipeline (researchers_data.json + the dashboard tab)
    doesn't need the Apps Script at all, so it proceeds either way.
    """
    try:
        resp = requests.post(APPS_SCRIPT_URL, json={"action": "ping"}, timeout=30)
        resp.raise_for_status()
        version = resp.json().get("version", 0)
    except Exception as e:
        print(f"  Could not reach APPS_SCRIPT_URL to check its version: {e}")
        version = 0

    if version < REQUIRED_SCRIPT_VERSION:
        print(
            f"  WARNING: deployed apps_script.js reports version={version} "
            f"(need >={REQUIRED_SCRIPT_VERSION}). Skipping ALL Google Sheet "
            "writes this run — the old deployment ignores sheet_name and "
            "would overwrite Sheet1. researchers_data.json and the dashboard "
            "Researchers tab will still be built. To also get the Researchers "
            "sheet tab, redeploy apps_script.js (Extensions > Apps Script > "
            "paste latest source > Deploy > Manage deployments > Edit > New "
            "version) and re-run."
        )
        return False
    return True


def load_researchers_from_sheet():
    """Read existing researcher profiles from the 'Researchers' sheet tab, if any."""
    url = (
        f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}"
        f"/gviz/tq?tqx=out:csv&sheet={RESEARCHERS_SHEET_NAME}"
    )
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        reader = csv.DictReader(io.StringIO(r.text))
        researchers = []
        for row in reader:
            r_ = {k: pp.fix_encoding(v) for k, v in row.items()}
            try:
                r_["avg_score"] = float(r_.get("avg_score", 0))
            except Exception:
                r_["avg_score"] = 0.0
            try:
                r_["paper_count"] = int(r_.get("paper_count", 0))
            except Exception:
                r_["paper_count"] = 0
            for jcol in ("papers", "fields", "branches"):
                raw = r_.get(jcol, "")
                if isinstance(raw, str) and raw.strip():
                    try:
                        r_[jcol] = json.loads(raw)
                    except Exception:
                        r_[jcol] = []
                else:
                    r_[jcol] = []
            if r_.get("pi"):
                researchers.append(r_)
        return researchers
    except Exception as e:
        print(f"  Could not read Researchers sheet (first run?): {e}")
        return []


def save_researchers_to_sheet(researchers):
    columns = ["pi", "pi_full_name", "pi_email", "pi_affiliation", "avg_score",
               "paper_count", "description", "applicability", "fields", "branches",
               "papers"]
    rows = [columns]
    for r in researchers:
        rows.append([
            r.get("pi", ""),
            r.get("pi_full_name", ""),
            r.get("pi_email", ""),
            r.get("pi_affiliation", ""),
            r.get("avg_score", 0),
            r.get("paper_count", 0),
            r.get("description", ""),
            r.get("applicability", ""),
            json.dumps(r.get("fields", []), ensure_ascii=False),
            json.dumps(r.get("branches", []), ensure_ascii=False),
            json.dumps(r.get("papers", []), ensure_ascii=False),
        ])
    backoffs = [3, 8, 15]
    for attempt, delay in enumerate([0] + backoffs):
        if delay:
            time.sleep(delay)
        try:
            resp = requests.post(
                APPS_SCRIPT_URL,
                json={"action": "replace_all", "rows": rows, "sheet_name": RESEARCHERS_SHEET_NAME},
                timeout=120,
            )
            resp.raise_for_status()
            print(f"Researchers sheet updated: {resp.text[:200]}")
            return
        except requests.exceptions.RequestException as e:
            if attempt == len(backoffs):
                raise
            print(f"  save_researchers_to_sheet attempt {attempt+1} failed ({e}), retrying...")


# ── Researcher selection ─────────────────────────────────────────────────────────

def canonical_pi_key(paper):
    name = (paper.get("pi_full_name") or paper.get("pi") or "").strip()
    return name


def select_top_researchers(papers, top_n):
    """Group papers by PI, rank by each PI's single highest-scoring paper."""
    best_score = {}
    display_name = {}
    email = {}
    affiliation = {}
    for p in papers:
        key = canonical_pi_key(p)
        if not key:
            continue
        score = p.get("score", 0)
        if key not in best_score or score > best_score[key]:
            best_score[key] = score
        if p.get("pi_full_name") and not display_name.get(key):
            display_name[key] = p["pi_full_name"]
        if p.get("pi_email") and not email.get(key):
            email[key] = p["pi_email"]
        if p.get("pi_affiliation") and not affiliation.get(key):
            affiliation[key] = p["pi_affiliation"]

    ranked = sorted(best_score.items(), key=lambda kv: kv[1], reverse=True)
    return [
        {"pi": key, "pi_full_name": display_name.get(key, key),
         "pi_email": email.get(key, ""), "pi_affiliation": affiliation.get(key, "")}
        for key, _score in ranked[:top_n]
    ]


# ── PubMed author-history fetch ───────────────────────────────────────────────────

def _pubmed_author_search_name(pi_name):
    """Convert a "First Last" display name into PubMed's expected [Author]
    search format: "Lastname Initials" (e.g. "Tamar Harel" -> "Harel T").
    PubMed's [Author] field does not match on "First Last" order at all —
    querying with the display name as-is silently returns zero results.
    """
    parts = pi_name.strip().split()
    if len(parts) < 2:
        return pi_name
    last = parts[-1]
    initials = "".join(p[0] for p in parts[:-1] if p)
    return f"{last} {initials}"


def _ncbi_get(url, params, timeout):
    """GET against NCBI E-Utilities with backoff on 429s.

    NCBI rate-limits unauthenticated clients to ~3 req/sec; with 20 researchers
    each issuing 2 requests back-to-back, a full run can trip that limit and
    lose whole candidates outright (confirmed live: 9/20 candidates failed
    with 429 in one run). Three retries with growing backoff is enough
    headroom without an NCBI API key.
    """
    backoffs = [1, 3, 8]
    for attempt, delay in enumerate([0] + backoffs):
        if delay:
            time.sleep(delay)
        r = requests.get(url, params=params, timeout=timeout)
        if r.status_code != 429:
            r.raise_for_status()
            return r
        if attempt == len(backoffs):
            r.raise_for_status()
    return r  # unreachable, keeps linters happy


def fetch_pubmed_for_author(pi_name, years_back=YEARS_BACK, max_results=50):
    """Fetch this author's HUJI-affiliated papers from the last `years_back` years.

    Includes AbstractText (unlike papers_pipeline.fetch_pubmed, which doesn't need
    it for the weekly feed) since researcher grading needs real abstracts.
    """
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    since = (datetime.date.today() - datetime.timedelta(days=365 * years_back)).isoformat()
    today = datetime.date.today().isoformat()
    author_term = _pubmed_author_search_name(pi_name)
    query = (
        f'"{author_term}"[Author] '
        'AND ("Hebrew University"[Affiliation] OR "Hadassah"[Affiliation]) '
        f'AND ("{since}"[PDAT] : "{today}"[PDAT])'
    )
    r = _ncbi_get(f"{base}/esearch.fcgi", {
        "db": "pubmed", "term": query,
        "retmax": max_results, "retmode": "json",
    }, timeout=20)
    ids = r.json().get("esearchresult", {}).get("idlist", [])
    if not ids:
        return []

    time.sleep(0.4)  # stay under NCBI's ~3 req/sec unauthenticated limit
    r2 = _ncbi_get(f"{base}/efetch.fcgi", {
        "db": "pubmed", "id": ",".join(ids), "rettype": "xml", "retmode": "xml",
    }, timeout=30)
    root = ET.fromstring(r2.text)

    papers = []
    for article in root.findall(".//PubmedArticle"):
        uid = article.findtext(".//PMID", "")
        title = article.findtext(".//ArticleTitle", "")
        journal = article.findtext(".//Journal/Title", "") or article.findtext(".//MedlineTA", "")
        pub_date = (article.findtext(".//PubDate/Year") or
                    article.findtext(".//PubDate/MedlineDate", "")[:4])
        abstract = " ".join(
            (el.text or "") for el in article.findall(".//Abstract/AbstractText")
        ).strip()

        author_affs = []
        all_authors = []
        for a in article.findall(".//AuthorList/Author"):
            last = a.findtext("LastName", "")
            fore = a.findtext("ForeName", "") or a.findtext("Initials", "")
            name = f"{fore} {last}".strip() if fore else last
            if name:
                all_authors.append(name)
            affs = [el.text or "" for el in a.findall(".//AffiliationInfo/Affiliation")]
            author_affs.append(affs)

        if not pp.is_huji_paper(author_affs):
            continue

        # Capture the last HUJI-affiliated author's affiliation string + email
        pi_affiliation = ""
        for _name, affs in reversed(list(zip(all_authors, author_affs))):
            if any(h.lower() in af.lower() for h in pp.HUJI_AFFILIATIONS for af in affs):
                pi_affiliation = "; ".join(a for a in affs if a)
                break
        if not pi_affiliation and author_affs:
            pi_affiliation = "; ".join(a for a in author_affs[-1] if a)
        email_match = pp.EMAIL_RE.search(pi_affiliation)
        pi_email = email_match.group(0) if email_match else ""

        papers.append({
            "id":       f"pubmed_{uid}",
            "title":    title,
            "abstract": abstract,
            "authors":  all_authors,
            "journal":  journal,
            "date":     pub_date,
            "url":      f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
            "source":   "PubMed",
            "pi_affiliation": pi_affiliation,
            "pi_email": pi_email,
        })
    return papers


# ── Researcher description ────────────────────────────────────────────────────────

def generate_researcher_summary(graded_papers):
    """One Gemini call → {description, applicability} for a researcher.

    Combined into a single call (rather than two) to conserve the scarce
    free-tier daily quota. Retries a few times on transient failure (all
    models/cooldowns down at once) rather than permanently blanking the
    field — the whole fallback chain being briefly unavailable is common
    and shouldn't be indistinguishable from "no summary possible".
    """
    entries = []
    for p in graded_papers[:10]:
        entries.append(f"- {p.get('title', '')}: {p.get('summary', '')}")
    paper_list = "\n".join(entries) or "(no summaries available)"
    last_error = None
    for attempt in range(3):
        if attempt:
            time.sleep(5 * attempt)
        try:
            # Ranking the whole researcher is a single, high-stakes call — use the
            # strongest model first (same tier as the per-paper meta call).
            data, _model = pp._call_gemini(
                RESEARCHER_PROMPT.format(paper_list=paper_list),
                candidates=pp.STRONG_MODEL_CANDIDATES, chain="strong",
            )
            return {
                "description": pp.fix_encoding(data.get("description", "")),
                "applicability": pp.fix_encoding(data.get("applicability", "")),
            }
        except Exception as e:
            last_error = e
            print(f"  Gemini error (researcher summary, attempt {attempt+1}/3): {e}")
    print(f"  giving up on researcher summary after 3 attempts: {last_error}")
    return {"description": "", "applicability": ""}


def _most_common(values):
    """Return the most frequent non-empty string in a list, or ''."""
    counts = {}
    for v in values:
        v = (v or "").strip()
        if v:
            counts[v] = counts.get(v, 0) + 1
    if not counts:
        return ""
    return max(counts, key=counts.get)


def _aggregate_fields(graded_papers):
    """Union of all field tags across a researcher's papers, most-frequent first."""
    counts = {}
    for p in graded_papers:
        for f in p.get("fields", []) or []:
            counts[f] = counts.get(f, 0) + 1
    return [f for f, _ in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)]


# ── Main ───────────────────────────────────────────────────────────────────────

def _grade_history(history, known_papers_by_id):
    """Grade a list of freshly-fetched PubMed papers, reusing an existing
    main-pipeline score when available instead of re-spending a Gemini call."""
    graded = []
    for paper in history:
        existing = known_papers_by_id.get(paper["id"])
        if existing and existing.get("score_breakdown"):
            # Already graded by the main pipeline — reuse rather than re-spend Gemini calls.
            graded.append({
                "id":              paper["id"],
                "title":           paper["title"],
                "date":            paper.get("date", ""),
                "url":             paper.get("url", ""),
                "journal":         paper.get("journal", ""),
                "score":           existing.get("score", 0),
                "summary":         existing.get("summary", ""),
                "opportunity":     existing.get("opportunity", ""),
                "abstract":        paper.get("abstract", ""),
                "fields":          existing.get("fields", []),
                "score_breakdown": existing.get("score_breakdown", {}),
                "eval_model":      existing.get("eval_model", ""),
                "prev_score":      existing.get("prev_score", ""),
                "prev_eval_model": existing.get("prev_eval_model", ""),
                "pi_affiliation":  paper.get("pi_affiliation", "") or existing.get("pi_affiliation", ""),
                "pi_email":        paper.get("pi_email", "") or existing.get("pi_email", ""),
            })
            continue

        print(f"    grading: {paper['title'][:70]}")
        result = pp.evaluate_paper(paper)
        if not result:
            continue
        print(f"      score={result['score']} ({result.get('eval_model','?')})")
        graded.append({
            "id":              paper["id"],
            "title":           paper["title"],
            "date":            paper.get("date", ""),
            "url":             paper.get("url", ""),
            "journal":         paper.get("journal", ""),
            "score":           result["score"],
            "summary":         result["summary"],
            "opportunity":     result["opportunity"],
            "abstract":        paper.get("abstract", ""),
            "fields":          result["fields"],
            "score_breakdown": result["score_breakdown"],
            "eval_model":      result.get("eval_model", ""),
            "prev_score":      "",
            "prev_eval_model": "",
            "pi_affiliation":  paper.get("pi_affiliation", ""),
            "pi_email":        paper.get("pi_email", ""),
        })
        time.sleep(0.4)
    return graded


def _researcher_metadata(candidate, graded, existing_profile=None):
    """Aggregate pi_affiliation/pi_email/fields/branches from a researcher's
    full graded-papers list, falling back to an existing profile's values
    (for a merge) and then the candidate's (from the main papers Sheet)."""
    existing_profile = existing_profile or {}
    pi_affiliation = (_most_common([p.get("pi_affiliation", "") for p in graded])
                      or existing_profile.get("pi_affiliation", "")
                      or candidate.get("pi_affiliation", ""))
    pi_email = (existing_profile.get("pi_email", "")
                or candidate.get("pi_email", "")
                or _most_common([p.get("pi_email", "") for p in graded]))
    fields = _aggregate_fields(graded)
    branches = sorted({b for b, bf in pp.BRANCHES.items()
                       if any(f in bf for f in fields)})
    return pi_affiliation, pi_email, fields, branches


def build_researcher_profile(candidate, known_papers_by_id):
    """Bootstrap a brand-new researcher profile from scratch."""
    pi_name = candidate["pi"]
    print(f"Fetching last {YEARS_BACK} years of publications for {pi_name}...")
    try:
        history = fetch_pubmed_for_author(pi_name)
    except Exception as e:
        print(f"  fetch error for {pi_name}: {e}")
        return None

    history = pp.dedup_by_title(history)
    history.sort(key=lambda p: p.get("date", ""), reverse=True)
    history = history[:MAX_PAPERS_PER_RESEARCHER]

    graded = _grade_history(history, known_papers_by_id)
    if not graded:
        print(f"  No gradable papers found for {pi_name} in the last {YEARS_BACK} years.")
        return None

    avg_score = round(sum(p["score"] for p in graded) / len(graded), 1)
    summary = generate_researcher_summary(graded)
    pi_affiliation, pi_email, fields, branches = _researcher_metadata(candidate, graded)

    return {
        "pi":            pi_name,
        "pi_full_name":  candidate.get("pi_full_name", pi_name),
        "pi_email":      pi_email,
        "pi_affiliation": pi_affiliation,
        "avg_score":     avg_score,
        "paper_count":   len(graded),
        "description":   summary["description"],
        "applicability": summary["applicability"],
        "fields":        fields,
        "branches":      branches,
        "papers":        graded,
    }


def merge_researcher_profile(candidate, existing_profile, known_papers_by_id):
    """Add newly-published papers to an already-existing researcher profile,
    instead of rebuilding it from scratch — papers already in the profile
    are left completely untouched (never re-fetched, re-graded, or dropped).
    Returns the existing profile unchanged if nothing new was found, so a
    quiet week costs no extra Gemini calls.
    """
    pi_name = candidate["pi"]
    existing_papers = existing_profile.get("papers", [])
    print(f"Checking for new publications for {pi_name} "
          f"(existing profile: {len(existing_papers)} paper(s))...")
    try:
        history = fetch_pubmed_for_author(pi_name)
    except Exception as e:
        print(f"  fetch error for {pi_name}: {e} — keeping existing profile as-is")
        return existing_profile

    history = pp.dedup_by_title(history)
    existing_ids = {p.get("id") for p in existing_papers}
    new_history = [p for p in history if p.get("id") not in existing_ids]
    new_history.sort(key=lambda p: p.get("date", ""), reverse=True)
    new_history = new_history[:MAX_PAPERS_PER_RESEARCHER]

    if not new_history:
        print(f"  No new papers for {pi_name}.")
        return existing_profile

    graded_new = _grade_history(new_history, known_papers_by_id)
    if not graded_new:
        print(f"  No gradable new papers for {pi_name}.")
        return existing_profile

    all_papers = existing_papers + graded_new
    avg_score = round(sum(p["score"] for p in all_papers) / len(all_papers), 1)
    # Only spend a Gemini call re-describing the researcher when there's
    # actually something new for it to reflect.
    summary = generate_researcher_summary(all_papers)
    pi_affiliation, pi_email, fields, branches = _researcher_metadata(
        candidate, all_papers, existing_profile)

    print(f"  Added {len(graded_new)} new paper(s) for {pi_name} "
          f"({len(existing_papers)} -> {len(all_papers)}).")
    return {
        "pi":            pi_name,
        "pi_full_name":  candidate.get("pi_full_name", existing_profile.get("pi_full_name", pi_name)),
        "pi_email":      pi_email,
        "pi_affiliation": pi_affiliation,
        "avg_score":     avg_score,
        "paper_count":   len(all_papers),
        "description":   summary["description"] or existing_profile.get("description", ""),
        "applicability": summary["applicability"] or existing_profile.get("applicability", ""),
        "fields":        fields,
        "branches":      branches,
        "papers":        all_papers,
    }


def reeval_researchers_to_gemma():
    """Re-score papers inside the already-committed researchers_data.json
    that aren't scored by Gemma yet — no PubMed re-fetch, no researcher
    re-selection, just upgrading existing paper scores in place. Recomputes
    avg_score for any profile that changes. Also backfills any profile still
    missing a description (e.g. from a run where the summary call failed
    outright) by retrying it against the current data. Safe to run
    repeatedly; anything that still fails is left untouched.
    """
    if not OUTPUT_JSON.exists():
        print("  No researchers_data.json found — nothing to re-evaluate.")
        return 0

    profiles = json.loads(OUTPUT_JSON.read_text(encoding="utf-8"))
    total_rescored = 0
    for profile in profiles:
        papers = profile.get("papers", [])
        targets = [p for p in papers if not pp._is_gemma_model(p.get("eval_model", ""))]
        if not targets:
            continue
        name = profile.get("pi_full_name") or profile.get("pi")
        print(f"\n{name}: {len(targets)} non-Gemma paper(s) to re-evaluate.")
        rescored_here = 0
        for p in targets:
            print(f"  re-eval: {p.get('title', '')[:70]}")
            p["abstract"] = pp._fetch_abstract_for_paper(p)
            result = pp.evaluate_paper(p, candidates=pp.GEMMA_ONLY_CANDIDATES, allow_groq=False)
            if not result or not pp._is_gemma_model(result.get("eval_model", "")):
                print("    still no Gemma result — leaving as-is")
                continue
            if not p.get("prev_eval_model"):
                p["prev_score"] = p.get("score", 0)
                p["prev_eval_model"] = p.get("eval_model", "")
            p.update({
                "score":           result["score"],
                "summary":         result["summary"],
                "opportunity":     result["opportunity"],
                "fields":          result["fields"],
                "score_breakdown": result["score_breakdown"],
                "eval_model":      result["eval_model"],
            })
            rescored_here += 1
            total_rescored += 1
            time.sleep(0.4)
        if rescored_here:
            scores = [p.get("score", 0) for p in papers]
            profile["avg_score"] = round(sum(scores) / len(scores), 1) if scores else 0

    # Backfill missing abstracts (for profiles built before this field existed,
    # or reused-from-main-pipeline papers that never had one persisted). NCBI
    # fetches are free/rate-limited rather than paid Gemini calls, but still
    # cap the per-run volume so a huge backlog doesn't blow out the runtime.
    total_abstracts = 0
    MAX_ABSTRACT_BACKFILL = 60
    for profile in profiles:
        for p in profile.get("papers", []):
            if total_abstracts >= MAX_ABSTRACT_BACKFILL:
                break
            if p.get("abstract", "").strip():
                continue
            p["abstract"] = pp._fetch_abstract_for_paper(p)
            if p["abstract"]:
                total_abstracts += 1
            time.sleep(0.2)
        if total_abstracts >= MAX_ABSTRACT_BACKFILL:
            break
    if total_abstracts:
        print(f"\nBackfilled {total_abstracts} missing abstract(s).")

    total_backfilled = 0
    for profile in profiles:
        if profile.get("description", "").strip():
            continue
        name = profile.get("pi_full_name") or profile.get("pi")
        print(f"\n{name}: missing description — regenerating summary.")
        summary = generate_researcher_summary(profile.get("papers", []))
        if summary["description"].strip():
            profile["description"] = summary["description"]
            profile["applicability"] = summary["applicability"]
            total_backfilled += 1
        else:
            print("    still failed — leaving blank for next run")

    if total_rescored or total_backfilled or total_abstracts:
        OUTPUT_JSON.write_text(json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8")
        if check_apps_script_version():
            try:
                save_researchers_to_sheet(profiles)
            except Exception as e:
                print(f"  sheet-save failed: {e}")
        try:
            main_papers = pp.load_from_sheet()
        except Exception as e:
            print(f"  Could not reload main papers for HTML regeneration: {e}")
            main_papers = []
        pp.generate_html(main_papers, researchers=profiles)

    print(f"\nRe-evaluated {total_rescored} paper(s) onto Gemma, "
          f"backfilled {total_backfilled} missing researcher description(s), "
          f"backfilled {total_abstracts} missing abstract(s).")
    return total_rescored + total_backfilled + total_abstracts


def main():
    if ARGS.reeval_to_gemma:
        reeval_researchers_to_gemma()
        return

    print("Checking apps_script.js deployment version...")
    sheet_writes_enabled = check_apps_script_version()
    if sheet_writes_enabled:
        print(f"  OK (version >= {REQUIRED_SCRIPT_VERSION}) — Researchers sheet tab will be updated.")

    if ARGS.papers_snapshot:
        print(f"Loading papers from snapshot {ARGS.papers_snapshot} "
              "(candidate selection + known-paper reuse only — backfill mode)...")
        try:
            papers = json.loads(Path(ARGS.papers_snapshot).read_text(encoding="utf-8"))
            print(f"  {len(papers)} papers loaded from snapshot.")
        except Exception as e:
            print(f"  Could not read snapshot: {e}")
            return
    else:
        print("Loading existing papers from Google Sheet...")
        try:
            papers = pp.load_from_sheet()
            print(f"  {len(papers)} papers loaded.")
        except Exception as e:
            print(f"  Could not read sheet: {e}")
            return

    known_papers_by_id = {p["id"]: p for p in papers}

    candidates = select_top_researchers(papers, TOP_N_RESEARCHERS)
    print(f"\nSelected {len(candidates)} researchers to profile.")

    existing_profiles = []
    if OUTPUT_JSON.exists():
        try:
            existing_profiles = json.loads(OUTPUT_JSON.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  Could not read existing {OUTPUT_JSON}: {e}")
    existing_by_pi = {p.get("pi"): p for p in existing_profiles if p.get("pi")}
    candidate_pis = {c["pi"] for c in candidates}

    # Profiles are additive: a researcher who profiled well in a past run but
    # falls out of this week's top-N candidate list keeps their existing
    # profile untouched (just not re-queried/updated this run), rather than
    # being dropped from the dataset.
    carried_forward = [p for pi, p in existing_by_pi.items() if pi not in candidate_pis]
    if carried_forward:
        print(f"Carrying forward {len(carried_forward)} existing profile(s) "
              f"not in this run's top {TOP_N_RESEARCHERS}.")

    profiles = list(carried_forward)

    def checkpoint(label):
        print(f"  [checkpoint: {label}] writing {len(profiles)} researcher profile(s)...")
        OUTPUT_JSON.write_text(json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8")
        if sheet_writes_enabled:
            try:
                save_researchers_to_sheet(profiles)
            except Exception as e:
                print(f"  checkpoint sheet-save failed (will retry at next checkpoint): {e}")
        pp.generate_html(papers, researchers=profiles)

    for i, candidate in enumerate(candidates):
        print(f"\n[{i+1}/{len(candidates)}] {candidate['pi']}")
        existing = existing_by_pi.get(candidate["pi"])
        if existing:
            profile = merge_researcher_profile(candidate, existing, known_papers_by_id)
        else:
            profile = build_researcher_profile(candidate, known_papers_by_id)
        if profile:
            profiles.append(profile)
        checkpoint(f"{i+1}/{len(candidates)}")
        time.sleep(0.5)  # extra headroom against NCBI's unauthenticated rate limit

    print(f"\nDone. {len(profiles)} researcher profiles written "
          f"({len(carried_forward)} carried forward unchanged).")


if __name__ == "__main__":
    main()
