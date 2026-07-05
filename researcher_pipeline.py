#!/usr/bin/env python3
"""
HUJI Researcher Applicability Pipeline
- Selects the top researchers from the existing scored-papers dataset
- Fetches each researcher's PubMed publication history for the last N years
- Grades every paper with the same scoring method as papers_pipeline.py
- Writes a per-researcher profile (avg score, description, graded papers) to
  a separate "Researchers" Google Sheet tab and researchers_data.json
- Regenerates papers_reader.html with the Researchers tab populated
"""

import argparse
import csv
import io
import json
import os
import time
import datetime
import xml.etree.ElementTree as ET
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
            "authors":  all_authors[:3],
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
    free-tier daily quota. Returns empty strings on failure.
    """
    entries = []
    for p in graded_papers[:10]:
        entries.append(f"- {p.get('title', '')}: {p.get('summary', '')}")
    paper_list = "\n".join(entries) or "(no summaries available)"
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
        print(f"  Gemini error (researcher summary): {e}")
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

def build_researcher_profile(candidate, known_papers_by_id):
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
                "fields":          existing.get("fields", []),
                "score_breakdown": existing.get("score_breakdown", {}),
                "eval_model":      existing.get("eval_model", ""),
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
            "fields":          result["fields"],
            "score_breakdown": result["score_breakdown"],
            "eval_model":      result.get("eval_model", ""),
            "pi_affiliation":  paper.get("pi_affiliation", ""),
            "pi_email":        paper.get("pi_email", ""),
        })
        time.sleep(0.4)

    if not graded:
        print(f"  No gradable papers found for {pi_name} in the last {YEARS_BACK} years.")
        return None

    avg_score = round(sum(p["score"] for p in graded) / len(graded), 1)
    summary = generate_researcher_summary(graded)

    # Aggregate researcher-level metadata from their papers
    pi_affiliation = (_most_common([p.get("pi_affiliation", "") for p in graded])
                      or candidate.get("pi_affiliation", ""))
    pi_email = (candidate.get("pi_email", "")
                or _most_common([p.get("pi_email", "") for p in graded]))
    fields = _aggregate_fields(graded)
    branches = sorted({b for b, bf in pp.BRANCHES.items()
                       if any(f in bf for f in fields)})

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


def main():
    print("Checking apps_script.js deployment version...")
    sheet_writes_enabled = check_apps_script_version()
    if sheet_writes_enabled:
        print(f"  OK (version >= {REQUIRED_SCRIPT_VERSION}) — Researchers sheet tab will be updated.")

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

    profiles = []

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
        profile = build_researcher_profile(candidate, known_papers_by_id)
        if profile:
            profiles.append(profile)
        checkpoint(f"{i+1}/{len(candidates)}")
        time.sleep(0.5)  # extra headroom against NCBI's unauthenticated rate limit

    print(f"\nDone. {len(profiles)} researcher profiles written.")


if __name__ == "__main__":
    main()
