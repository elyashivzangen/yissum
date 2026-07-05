#!/usr/bin/env python3
"""
Hebrew University Paper Evaluation Pipeline
- Reads existing papers from public Google Sheet (CSV export, no auth)
- Fetches new HUJI-affiliated papers from PubMed + Europe PMC + Semantic Scholar
- Evaluates with Gemini (score, summary, commercial opportunity, field tags)
- Writes updated papers back via Apps Script web app (no service account needed)
- Generates standalone papers_reader.html committed to the repo
"""

import argparse
import csv
import io
import json
import os
import re
import time
import datetime
import xml.etree.ElementTree as ET
import requests
import ftfy
from google import genai
from google.genai import types
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────────────
GEMINI_API_KEY   = os.environ["GEMINI_API_KEY"]
GOOGLE_SHEET_ID  = os.environ["GOOGLE_SHEET_ID"]
APPS_SCRIPT_URL  = os.environ["APPS_SCRIPT_URL"]   # deployed Apps Script web app URL
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY", "")  # optional last-resort fallback, runs on Groq's own infra
GROQ_MODEL       = "llama-3.1-8b-instant"
OUTPUT_HTML      = Path("papers_reader.html")
OUTPUT_JSON      = Path("papers_data.json")
RESEARCHERS_JSON = Path("researchers_data.json")  # produced by researcher_pipeline.py
MAX_RESULTS      = int(os.environ.get("MAX_RESULTS", "50"))    # per source

def _parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--period", choices=["week", "month"], default="week",
                   help="Fetch window: week=7 days, month=30 days")
    p.add_argument("--backfill-metadata", action="store_true",
                   help="One-off: refresh pi_affiliation/date precision for existing "
                        "papers only, skipping new-paper fetch and evaluation")
    p.add_argument("--reeval-to-gemma", "--reeval-groq", dest="reeval_to_gemma", action="store_true",
                   help="One-off: re-score any paper NOT currently scored by a Gemma "
                        "model (i.e. scored by gemini-3.1-flash-lite or Groq) using "
                        "Gemma only, so every paper converges on the same model over "
                        "time (skips new-paper fetch). The paper's previous score/model "
                        "are kept in prev_score/prev_eval_model for comparison. Papers "
                        "whose Gemma re-score still fails (e.g. it's still down) are "
                        "left untouched, so it is safe to run repeatedly. "
                        "--reeval-groq is a deprecated alias for the same flag.")
    # parse_known_args so this module stays importable from other scripts
    # (e.g. researcher_pipeline.py) that define their own unrelated CLI flags.
    return p.parse_known_args()[0]

ARGS     = _parse_args()
DAYS_BACK = int(os.environ.get("DAYS_BACK", "7" if ARGS.period == "week" else "30"))

HUJI_AFFILIATIONS = [
    "Hebrew University of Jerusalem",
    "Hebrew University",
    "Hadassah",
    "Einstein Institute of Mathematics",
    "Silberman Institute",
]

EMAIL_RE = re.compile(r'[\w.+\-]+@[\w\-]+\.[\w.\-]+')

FIELD_TAGS = [
    "Drug Discovery", "Medical Device", "Diagnostics", "Vaccines",
    "AgriTech", "FoodTech", "Materials", "Clean Energy",
    "Software/AI", "Quantum", "Neuroscience", "Genomics",
    "Imaging", "Synthetic Biology", "Proteomics", "Immunology",
    "Clinical", "Other",
]

# Three main Yissum TTO branches — each field tag belongs to exactly one branch
BRANCHES = {
    "Healthcare": [
        "Drug Discovery", "Medical Device", "Diagnostics", "Vaccines",
        "Neuroscience", "Genomics", "Imaging", "Synthetic Biology",
        "Proteomics", "Immunology", "Clinical",
    ],
    "Agriculture & Food": ["AgriTech", "FoodTech"],
    "Exact & Social Sciences": ["Materials", "Clean Energy", "Software/AI", "Quantum", "Other"],
}

SHEET_COLUMNS = [
    "id", "title", "authors", "journal", "date", "url", "source",
    "score", "summary", "opportunity", "fields", "added_date", "score_breakdown", "pi",
    "pi_full_name", "pi_email", "pi_affiliation", "eval_model",
    "prev_score", "prev_eval_model",
]

SCORE_PARAMS = [
    ("novelty",              "Scientific novelty and innovation — how groundbreaking is this research compared to prior art?"),
    ("commercial_potential", "Commercial opportunity strength — how clearly does this translate to a product, service, or licensable technology?"),
    ("market_size",          "Market size and addressable demand — how large and valuable is the target market?"),
    ("trl",                  "Technology readiness — how close is this to real-world application or commercialization (lab-stage vs. near-market)?"),
    ("ip_strength",          "IP and defensibility — how patentable or otherwise defensible is the underlying innovation?"),
]

client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=types.HttpOptions(timeout=30_000),  # ms — prevent an indefinite hang on a stalled call
)

# ── Google Sheets (no service account) ────────────────────────────────────────

def fix_encoding(text):
    """Repair mojibake using ftfy (handles multi-level UTF-8/Latin-1 mismatches)."""
    if not isinstance(text, str):
        return text
    return ftfy.fix_text(text)


def load_from_sheet():
    """Read all papers from the public Google Sheet via CSV export."""
    url = (
        f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}"
        f"/export?format=csv&gid=0"
    )
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    # Force UTF-8 decode — requests may misdetect text/csv as latin-1
    reader = csv.DictReader(io.StringIO(r.content.decode("utf-8")))
    papers = []
    for row in reader:
        p = {k: fix_encoding(v) for k, v in row.items()}
        for f in ("authors", "fields"):
            val = p.get(f, "")
            if isinstance(val, str) and val.strip():
                try:
                    p[f] = json.loads(val)
                except Exception:
                    p[f] = []
            else:
                p[f] = []
        try:
            p["score"] = int(p.get("score", 0))
        except Exception:
            p["score"] = 0
        sb = p.get("score_breakdown", "")
        if isinstance(sb, str) and sb.strip():
            try:
                p["score_breakdown"] = json.loads(sb)
            except Exception:
                p["score_breakdown"] = {}
        else:
            p["score_breakdown"] = {}
        papers.append(p)
    return papers


def save_to_sheet(papers):
    """Rewrite the sheet via the Apps Script web app."""
    rows = [SHEET_COLUMNS]
    for p in papers:
        rows.append([
            p.get("id", ""),
            p.get("title", ""),
            json.dumps(p.get("authors", []), ensure_ascii=False),
            p.get("journal", ""),
            p.get("date", ""),
            p.get("url", ""),
            p.get("source", ""),
            p.get("score", 0),
            p.get("summary", ""),
            p.get("opportunity", ""),
            json.dumps(p.get("fields", []), ensure_ascii=False),
            p.get("added_date", today_str()),
            json.dumps(p.get("score_breakdown", {}), ensure_ascii=False),
            p.get("pi", ""),
            p.get("pi_full_name", ""),
            p.get("pi_email", ""),
            p.get("pi_affiliation", ""),
            p.get("eval_model", ""),
            p.get("prev_score", ""),
            p.get("prev_eval_model", ""),
        ])
    backoffs = [3, 8, 15]
    for attempt, delay in enumerate([0] + backoffs):
        if delay:
            time.sleep(delay)
        try:
            r = requests.post(
                APPS_SCRIPT_URL,
                json={"action": "replace_all", "rows": rows},
                timeout=120,
            )
            r.raise_for_status()
            print(f"Sheet updated: {r.text[:200]}")
            return
        except requests.exceptions.RequestException as e:
            if attempt == len(backoffs):
                raise
            print(f"  save_to_sheet attempt {attempt+1} failed ({e}), retrying...")


# ── Helpers ────────────────────────────────────────────────────────────────────

def today_str():
    return datetime.date.today().isoformat()

def days_ago(n):
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()

def norm_title(t):
    return re.sub(r"\W+", " ", (t or "").lower()).strip()


def is_huji_paper(authors_with_affs):
    """True if the last author OR the majority of authors are HUJI/Hadassah-affiliated.

    Hadassah is the clinical arm of HUJI and is treated identically to HUJI:
    keep if the last author or >50% of authors are from Hadassah or Hebrew University.

    authors_with_affs: list (one element per author) of lists of affiliation strings.
    """
    if not authors_with_affs:
        return False

    def has_huji(affs):
        return any(h.lower() in af.lower() for h in HUJI_AFFILIATIONS for af in affs)

    if has_huji(authors_with_affs[-1]):
        return True
    huji_count = sum(1 for affs in authors_with_affs if has_huji(affs))
    return huji_count > len(authors_with_affs) / 2

def existing_ids(papers):
    return {p["id"] for p in papers}

def dedup_by_title(papers):
    """Deduplicate a list of papers by normalised title.

    When two papers share a title, keep the one with the most data
    (prefers: has score_breakdown > higher score > has email > PubMed source).
    Merges email/pi_full_name from the dropped copy into the kept one.
    """
    best = {}  # norm_title → paper
    for p in papers:
        key = norm_title(p.get("title", ""))
        if not key:
            continue
        if key not in best:
            best[key] = p
        else:
            existing = best[key]
            # Decide which copy to keep
            e_bd = bool(existing.get("score_breakdown"))
            p_bd = bool(p.get("score_breakdown"))
            keep, drop = (existing, p) if (
                (e_bd and not p_bd) or
                (e_bd == p_bd and existing.get("score", 0) >= p.get("score", 0))
            ) else (p, existing)
            # Merge contact info from the dropped copy
            keep.setdefault("pi_full_name", drop.get("pi_full_name", ""))
            keep.setdefault("pi_email", drop.get("pi_email", ""))
            best[key] = keep
    return list(best.values())

def _pmid_from_paper_id(paper_id):
    """Return the PMID string if paper_id is pubmed_NNNN or epmc_NNNN (numeric = PMID for MED-source papers)."""
    for prefix in ("pubmed_", "epmc_"):
        if paper_id.startswith(prefix):
            suffix = paper_id[len(prefix):]
            if suffix.isdigit():
                return suffix
    return None


def _verify_huji_pubmed(papers, batch_size=50):
    """Remove papers where HUJI is not the last/majority author.

    Covers pubmed_* and epmc_* papers (EuropePMC MED-source IDs are PMIDs).
    Uses batch PubMed efetch for per-author affiliation data.
    Non-PMID papers (ss_* etc.) are kept unchanged.
    """
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    # Map PMID → paper for all papers with a verifiable PMID
    pmid_map = {}
    for p in papers:
        pmid = _pmid_from_paper_id(p["id"])
        if pmid and pmid not in pmid_map:
            pmid_map[pmid] = p

    if not pmid_map:
        return papers

    print(f"  Verifying HUJI affiliation for {len(pmid_map)} papers via PubMed efetch...")
    verified_pmids = set()
    ids = list(pmid_map.keys())
    for i in range(0, len(ids), batch_size):
        batch = ids[i:i + batch_size]
        try:
            r = requests.get(f"{base}/efetch.fcgi", params={
                "db": "pubmed", "id": ",".join(batch),
                "rettype": "xml", "retmode": "xml",
            }, timeout=30)
            r.raise_for_status()
            root = ET.fromstring(r.text)
            for article in root.findall(".//PubmedArticle"):
                uid = article.findtext(".//PMID", "")
                author_affs = [
                    [el.text or "" for el in a.findall(".//AffiliationInfo/Affiliation")]
                    for a in article.findall(".//AuthorList/Author")
                ]
                if is_huji_paper(author_affs):
                    verified_pmids.add(uid)
        except Exception as e:
            print(f"  PubMed affiliation check error (batch {i}): {e}")
            verified_pmids.update(batch)   # keep on transient error
        time.sleep(0.3)

    # Build set of paper IDs to drop
    drop_ids = {p["id"] for pmid, p in pmid_map.items() if pmid not in verified_pmids}
    if drop_ids:
        print(f"  Removed {len(drop_ids)} paper(s) with no HUJI last/majority author:")
        for p in papers:
            if p["id"] in drop_ids:
                print(f"    - {p['title'][:70]}")
    return [p for p in papers if p["id"] not in drop_ids]


def backfill_metadata(papers, batch_size=50):
    """One-off: refresh pi_affiliation and date precision for existing papers
    that are missing them, without touching score/summary/opportunity."""
    needs = [p for p in papers if not p.get("pi_affiliation") or len(p.get("date", "")) <= 4]
    if not needs:
        print("  No papers need metadata backfill.")
        return
    print(f"  Backfilling metadata for {len(needs)} papers...")

    pmid_map = {}
    for p in needs:
        pmid = _pmid_from_paper_id(p["id"])
        if pmid:
            pmid_map[pmid] = p

    if pmid_map:
        base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
        ids = list(pmid_map.keys())
        for i in range(0, len(ids), batch_size):
            batch = ids[i:i + batch_size]
            try:
                r = requests.get(f"{base}/efetch.fcgi", params={
                    "db": "pubmed", "id": ",".join(batch),
                    "rettype": "xml", "retmode": "xml",
                }, timeout=30)
                r.raise_for_status()
                root = ET.fromstring(r.text)
                for article in root.findall(".//PubmedArticle"):
                    uid = article.findtext(".//PMID", "")
                    p = pmid_map.get(uid)
                    if not p:
                        continue
                    if len(p.get("date", "")) <= 4:
                        p["date"] = _pubmed_pub_date(article)
                    if not p.get("pi_affiliation"):
                        all_authors, author_affs = [], []
                        for a in article.findall(".//AuthorList/Author"):
                            last = a.findtext("LastName", "")
                            fore = a.findtext("ForeName", "") or a.findtext("Initials", "")
                            all_authors.append(f"{fore} {last}".strip() if fore else last)
                            author_affs.append([el.text or "" for el in a.findall(".//AffiliationInfo/Affiliation")])
                        pi_aff = ""
                        for name, affs in reversed(list(zip(all_authors, author_affs))):
                            if any(h.lower() in af.lower() for h in HUJI_AFFILIATIONS for af in affs):
                                pi_aff = "; ".join(a for a in affs if a)
                                break
                        if not pi_aff and author_affs:
                            pi_aff = "; ".join(a for a in author_affs[-1] if a)
                        if pi_aff:
                            p["pi_affiliation"] = pi_aff
            except Exception as e:
                print(f"  Metadata backfill PubMed error (batch {i}): {e}")
            time.sleep(0.3)

    ss_map = {p["id"][3:]: p for p in needs if p["id"].startswith("ss_")}
    if ss_map:
        try:
            r = requests.post(
                "https://api.semanticscholar.org/graph/v1/paper/batch",
                params={"fields": "publicationDate,year,authors.name,authors.affiliations"},
                json={"ids": list(ss_map.keys())},
                timeout=30,
            )
            r.raise_for_status()
            for item in r.json():
                if not item:
                    continue
                p = ss_map.get(item.get("paperId", ""))
                if not p:
                    continue
                pub_date = item.get("publicationDate") or (f"{item['year']}-01-01" if item.get("year") else "")
                if pub_date and len(p.get("date", "")) <= 4:
                    p["date"] = pub_date
                if not p.get("pi_affiliation"):
                    all_author_objs = item.get("authors") or []
                    pi_aff = ""
                    for a in reversed(all_author_objs):
                        affs = a.get("affiliations") or []
                        if any(h.lower() in (aff or "").lower() for h in HUJI_AFFILIATIONS for aff in affs):
                            pi_aff = "; ".join(aff for aff in affs if aff)
                            break
                    if not pi_aff and all_author_objs:
                        pi_aff = "; ".join(aff for aff in (all_author_objs[-1].get("affiliations") or []) if aff)
                    if pi_aff:
                        p["pi_affiliation"] = pi_aff
        except Exception as e:
            print(f"  Metadata backfill Semantic Scholar error: {e}")

    print("  Metadata backfill done.")


# ── PI Contact Enrichment ──────────────────────────────────────────────────────

def _pubmed_efetch_pi(pmid):
    """Return (full_name, email) for the HUJI PI from PubMed full XML."""
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    try:
        r = requests.get(f"{base}/efetch.fcgi", params={
            "db": "pubmed", "id": pmid, "rettype": "xml", "retmode": "xml",
        }, timeout=15)
        r.raise_for_status()
        root = ET.fromstring(r.text)
    except Exception:
        return None, None

    candidates = []
    for a in root.findall(".//AuthorList/Author"):
        last = a.findtext("LastName", "")
        fore = a.findtext("ForeName", "") or a.findtext("Initials", "")
        full = f"{fore} {last}".strip() if fore else last
        affs = [el.text or "" for el in a.findall(".//AffiliationInfo/Affiliation")]
        is_huji = any(h.lower() in af.lower() for h in HUJI_AFFILIATIONS for af in affs)
        m = next((EMAIL_RE.search(af) for af in affs if EMAIL_RE.search(af)), None)
        candidates.append((full, is_huji, m.group() if m else None))

    # Prefer last HUJI author with email, then without, then last author
    for full, is_huji, email in reversed(candidates):
        if is_huji and email:
            return full, email
    for full, is_huji, email in reversed(candidates):
        if is_huji and full:
            return full, None
    if candidates:
        full, _, email = candidates[-1]
        return full, email
    return None, None


def _crossref_pi_email(doi):
    """Try CrossRef for a corresponding-author email (best-effort)."""
    try:
        r = requests.get(
            f"https://api.crossref.org/works/{doi}",
            headers={"User-Agent": "HUJI-Pipeline/1.0"},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        for author in r.json().get("message", {}).get("author", []):
            email = author.get("email", "")
            if email:
                return email
    except Exception as e:
        print(f"  CrossRef lookup error: {e}")
    return None


def _orcid_lookup(name):
    """Search ORCID public API for a researcher's public email by name + HUJI affiliation."""
    parts = name.strip().split()
    if not parts:
        return None, None
    last = parts[-1]
    first = parts[0] if len(parts) > 1 else ""
    query = f'family-name:{last} AND affiliation-org-name:"Hebrew University"'
    if first:
        query += f" AND given-names:{first}"
    try:
        r = requests.get(
            "https://pub.orcid.org/v3.0/search",
            params={"q": query, "rows": 3},
            headers={"Accept": "application/json"},
            timeout=10,
        )
        if r.status_code != 200:
            return None, None
        results = r.json().get("result") or []
        for res in results:
            orcid_id = (res.get("orcid-identifier") or {}).get("path")
            if not orcid_id:
                continue
            # Try to get a public email for this ORCID profile
            er = requests.get(
                f"https://pub.orcid.org/v3.0/{orcid_id}/email",
                headers={"Accept": "application/json"},
                timeout=10,
            )
            if er.status_code != 200:
                continue
            email_data = er.json() or {}
            # ORCID v3 wraps emails under {"email": [...]} or {"emails": {"email": [...]}}
            entries = email_data.get("email") or (email_data.get("emails") or {}).get("email") or []
            if not isinstance(entries, list):
                entries = []
            for entry in entries:
                addr = entry.get("email")
                if addr:
                    return addr, orcid_id
    except Exception as e:
        print(f"  ORCID lookup error: {e}")
    return None, None


def enrich_pi_contact(paper):
    """Resolve full name and email for the PI. Updates paper dict in-place.

    Strategy (in order):
    1. PubMed full-XML efetch  — extracts email from affiliation strings
    2. CrossRef                — corresponding-author email via DOI
    3. ORCID public API        — searches by name + institution, reads public email
    """
    pid = paper.get("id", "")

    pmid = _pmid_from_paper_id(pid)
    if pmid:
        full_name, email = _pubmed_efetch_pi(pmid)
        if full_name:
            paper.setdefault("pi_full_name", full_name)
        if email:
            paper.setdefault("pi_email", email)
        if paper.get("pi_full_name") or paper.get("pi_email"):
            if paper.get("pi_email"):
                return  # have both name and email — done

    # CrossRef: try DOI URL for corresponding-author email
    url = paper.get("url", "")
    if "doi.org/" in url and not paper.get("pi_email"):
        doi = url.split("doi.org/", 1)[-1].rstrip("/")
        email = _crossref_pi_email(doi)
        if email:
            paper["pi_email"] = email
            return

    # ORCID: search by PI name + HUJI affiliation for a public email
    if not paper.get("pi_email"):
        name = paper.get("pi_full_name") or paper.get("pi", "")
        if name:
            email, _ = _orcid_lookup(name)
            if email:
                paper["pi_email"] = email


# ── Fetchers ───────────────────────────────────────────────────────────────────

_PUBMED_MONTHS = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
    "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}

def _pubmed_pub_date(article):
    """Build the most precise ISO date available from a PubmedArticle's <PubDate>."""
    year = article.findtext(".//PubDate/Year", "")
    month = article.findtext(".//PubDate/Month", "")
    day = article.findtext(".//PubDate/Day", "")
    if not year:
        return article.findtext(".//PubDate/MedlineDate", "")[:4]
    if month:
        month = month.strip().lower()
        month = _PUBMED_MONTHS.get(month[:3], month if month.isdigit() else "")
    if year and month and day:
        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
    if year and month:
        return f"{year}-{month.zfill(2)}"
    return year

def fetch_pubmed(max_results=MAX_RESULTS):
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    since = days_ago(DAYS_BACK)
    query = (
        '("Hebrew University"[Affiliation] OR "Hadassah"[Affiliation]) '
        f'AND ("{since}"[PDAT] : "{today_str()}"[PDAT])'
    )
    r = requests.get(f"{base}/esearch.fcgi", params={
        "db": "pubmed", "term": query,
        "retmax": max_results, "retmode": "json",
    }, timeout=20)
    r.raise_for_status()
    ids = r.json().get("esearchresult", {}).get("idlist", [])
    if not ids:
        return []

    # Use efetch (full XML) so we have per-author affiliations for HUJI validation
    r2 = requests.get(f"{base}/efetch.fcgi", params={
        "db": "pubmed", "id": ",".join(ids), "rettype": "xml", "retmode": "xml",
    }, timeout=30)
    r2.raise_for_status()
    root = ET.fromstring(r2.text)

    papers = []
    for article in root.findall(".//PubmedArticle"):
        uid = article.findtext(".//PMID", "")
        title = article.findtext(".//ArticleTitle", "")
        journal = article.findtext(".//Journal/Title", "") or article.findtext(".//MedlineTA", "")
        pub_date = _pubmed_pub_date(article)

        # Collect per-author affiliations for HUJI validation
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

        # Require last author or majority to be HUJI-affiliated
        if not is_huji_paper(author_affs):
            continue

        pi = ""
        pi_affiliation = ""
        for name, affs in reversed(list(zip(all_authors, author_affs))):
            if any(h.lower() in af.lower() for h in HUJI_AFFILIATIONS for af in affs):
                pi = name
                pi_affiliation = "; ".join(a for a in affs if a)
                break
        if not pi and all_authors:
            pi = all_authors[-1]
            pi_affiliation = "; ".join(a for a in author_affs[-1] if a) if author_affs else ""

        papers.append({
            "id":       f"pubmed_{uid}",
            "title":    title,
            "abstract": "",
            "authors":  all_authors[:3],
            "journal":  journal,
            "date":     pub_date,
            "url":      f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
            "source":   "PubMed",
            "pi":       pi,
            "pi_affiliation": pi_affiliation,
        })
    return papers


def fetch_europepmc(max_results=MAX_RESULTS):
    since = days_ago(DAYS_BACK)
    query = (
        '(AFF:"Hebrew University of Jerusalem" OR AFF:"Hadassah") '
        f'AND FIRST_PDATE:[{since} TO {today_str()}]'
    )
    r = requests.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search", params={
        "query": query, "resultType": "core",
        "pageSize": max_results, "format": "json",
    }, timeout=20)
    r.raise_for_status()
    items = r.json().get("resultList", {}).get("result", [])
    papers = []
    for item in items:
        all_author_objs = (item.get("authorList") or {}).get("author", [])
        all_authors = []
        for a in all_author_objs:
            name = f"{a.get('firstName','')} {a.get('lastName','')}".strip()
            if name:
                all_authors.append(name)
        # Build per-author affiliation lists for HUJI validation
        author_affs = [
            [x.get("affiliation", "") for x in
             (a.get("authorAffiliationDetailsList") or {}).get("authorAffiliation", [])]
            for a in all_author_objs
        ]
        if not is_huji_paper(author_affs):
            continue

        # Find last HUJI-affiliated author as PI
        pi = ""
        pi_affiliation = ""
        for a, affs in reversed(list(zip(all_author_objs, author_affs))):
            if any(h.lower() in af.lower() for h in HUJI_AFFILIATIONS for af in affs):
                pi = f"{a.get('firstName','')} {a.get('lastName','')}".strip()
                pi_affiliation = "; ".join(x for x in affs if x)
                break
        if not pi and all_authors:
            pi = all_authors[-1]
            pi_affiliation = "; ".join(x for x in author_affs[-1] if x) if author_affs else ""
        papers.append({
            "id":       f"epmc_{item.get('id','')}",
            "title":    item.get("title", ""),
            "abstract": item.get("abstractText", ""),
            "authors":  all_authors[:3],
            "journal":  item.get("journalTitle", ""),
            "date":     item.get("firstPublicationDate", ""),
            "url":      f"https://europepmc.org/article/{item.get('source','')}/{item.get('id','')}",
            "source":   "Europe PMC",
            "pi":       pi,
            "pi_affiliation": pi_affiliation,
        })
    return papers


def fetch_semantic_scholar(max_results=MAX_RESULTS):
    r = requests.get("https://api.semanticscholar.org/graph/v1/paper/search", params={
        "query": "Hebrew University of Jerusalem",
        "fields": "title,abstract,authors,year,venue,externalIds,publicationDate,authors.affiliations",
        "limit": max_results,
    }, timeout=20)
    r.raise_for_status()
    since = days_ago(DAYS_BACK)
    papers = []
    for item in r.json().get("data", []):
        pub_date = item.get("publicationDate") or f"{item.get('year','')}-01-01"
        if pub_date < since:
            continue
        all_author_objs = item.get("authors") or []
        all_authors = [a.get("name", "") for a in all_author_objs]
        author_affs = [a.get("affiliations") or [] for a in all_author_objs]

        if not is_huji_paper(author_affs):
            continue

        pid = item.get("paperId", "")
        ext = item.get("externalIds") or {}
        url = (f"https://doi.org/{ext['DOI']}" if ext.get("DOI")
               else f"https://www.semanticscholar.org/paper/{pid}")

        # Find last HUJI-affiliated author as PI
        pi = ""
        pi_affiliation = ""
        for a, affs in reversed(list(zip(all_author_objs, author_affs))):
            if any(h.lower() in (aff or "").lower() for h in HUJI_AFFILIATIONS for aff in affs):
                pi = a.get("name", "")
                pi_affiliation = "; ".join(aff for aff in affs if aff)
                break
        if not pi and all_authors:
            pi = all_authors[-1]
            pi_affiliation = "; ".join(aff for aff in author_affs[-1] if aff) if author_affs else ""
        papers.append({
            "id":       f"ss_{pid}",
            "title":    item.get("title", ""),
            "abstract": item.get("abstract", "") or "",
            "authors":  all_authors[:3],
            "journal":  item.get("venue", ""),
            "date":     pub_date,
            "url":      url,
            "source":   "Semantic Scholar",
            "pi":       pi,
            "pi_affiliation": pi_affiliation,
        })
    return papers

# ── Gemini Evaluation ──────────────────────────────────────────────────────────

META_PROMPT = """You are a life-science and deep-tech investment analyst.
Evaluate this paper from Hebrew University of Jerusalem researchers
for relevance to emerging commercial opportunities in biotech, medtech,
agritech, materials, clean energy, or AI/software tools for science.

Title: {title}
Abstract: {abstract}

Return a JSON object (no markdown) with these exact keys:
- summary: 2-sentence plain-English summary
- opportunity: 1-sentence commercial angle
- fields: list of 1-4 tags from: {fields}
"""

PARAM_PROMPT = """You are a life-science and deep-tech investment analyst.
Score this paper on ONE specific dimension only.

Dimension: {param_name}
Definition: {param_desc}

Title: {title}
Abstract: {abstract}

Use the full 1-10 scale. Scores of 8-10 are appropriate for clearly protectable,
clinically or industrially relevant, platform-enabling, or near-market work;
do not reserve 8-10 only for already-marketed products.

Return a JSON object (no markdown) with exactly these keys:
- score: integer 1-10 (10 = excellent on this dimension)
- reason: 1-2 sentence explanation for the score on this specific dimension
"""

# Single strength-ordered chain, used for every call (meta, per-param, and
# the researcher-level summary) — the strongest model is always tried
# first, falling through to weaker models only on failure:
#
#   gemma-4-31b-it (31B dense, #3 on the Arena leaderboard; BenchLM has it
#   beating gemini-3.1-flash-lite 62-49 across benchmark categories)
#     > gemma-4-26b-a4b-it (26B MoE, ~3.8B active/token, #6 Arena)
#       > gemini-3.1-flash-lite (cost/speed tier — its only edge is
#         latency/context window, not quality)
#         > groq llama-3.1-8b-instant (8B, weakest — last resort only)
#
# An earlier version of this chain split into two tiers (strong-first for
# meta/summary calls, flash-lite-first for the high-volume per-param calls)
# to spread load off Gemma. That's intentionally reverted: Gemma is the
# best model available here, so every call prefers it regardless of volume;
# only fall back when it actually fails. Gemma's real reliability problem
# (frequent 500/503/504 from Google's side on the newly-launched Gemma 4
# endpoints) is mitigated instead via thinking_level=minimal, throttling,
# and cooldown-not-permanent-ban (see below) — and via reeval_to_gemma()
# which upgrades any paper that had to fall back, once capacity frees up.
#
# (gemma-3-27b-it and gemma-4-4b-it were dropped after confirming via live
# runs that they 404 on every single call — pure dead latency in the chain.)
STRONG_MODEL_CANDIDATES = [
    os.environ.get("GEMINI_MODEL", "gemma-4-31b-it"),
    "gemma-4-26b-a4b-it",
    "gemini-3.1-flash-lite",
]
GEMMA_ONLY_CANDIDATES = [
    os.environ.get("GEMINI_MODEL", "gemma-4-31b-it"),
    "gemma-4-26b-a4b-it",
]
EVAL_MODEL_CANDIDATES = STRONG_MODEL_CANDIDATES  # back-compat alias

# Minimum seconds between two calls to the SAME model, sized to each model's
# documented free-tier RPM with headroom. Each paper needs 6 calls (1 meta +
# 5 score dimensions); without this, calls fired every ~0.4s ran at ~150/min —
# ~10x over every one of these models' real limits — which is what was
# actually causing the "500/504" errors and the 429s, not genuine exhaustion
# of a generous quota. Confirmed live: once all models got banned from
# overload, every paper after that point in a run failed outright.
_MIN_CALL_INTERVAL = {
    "gemma-4-31b-it":        4.5,   # ~15 RPM free tier
    "gemma-4-26b-a4b-it":    4.5,   # ~15 RPM free tier
    "gemini-3.1-flash-lite": 4.5,   # ~15 RPM free tier
    "groq":                  2.2,   # ~30 RPM free tier
}
_last_call_time = {}   # model_id -> time.monotonic() of last call, for throttling

def _throttle(model_key):
    min_interval = _MIN_CALL_INTERVAL.get(model_key, 0.4)
    last = _last_call_time.get(model_key, 0)
    wait = min_interval - (time.monotonic() - last)
    if wait > 0:
        time.sleep(wait)
    _last_call_time[model_key] = time.monotonic()

_last_good_idx = {}        # chain name -> index of the model that worked last in that chain
_cooldown_until = {}       # model_id -> time.monotonic() before which it's skipped (shared across chains)
_fail_streak = {}          # model_id -> consecutive fail count, resets on any success (shared across chains)
_COOLDOWN_SECONDS = 90     # bench a misbehaving model for a while, not the whole run —
                            # transient overload/500s recover; a permanent ban just
                            # dumps 100% of traffic onto whatever's left, which then
                            # overloads too (this is exactly what happened live).

# Gemma 4's "thinking"/extended-reasoning mode adds real latency, which is a
# likely contributor to the live 504 DEADLINE_EXCEEDED errors (Google's own
# guidance: 504s on reasoning models are typically fixed by cutting response
# time). thinking_level="minimal" turns that off. include_thoughts=False is
# reportedly silently ignored by this model — thinking_level is what actually
# works. Built once and reused; if the installed google-genai SDK is too old
# to have ThinkingConfig, this degrades to plain calls instead of crashing.
try:
    _GEMMA_GEN_CONFIG = types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_level="minimal")
    )
except Exception as e:
    print(f"  (thinking_level config unavailable in this SDK, calling Gemma without it: {e})")
    _GEMMA_GEN_CONFIG = None

def _call_gemini(prompt, allow_groq=True, candidates=None, chain="strong"):
    """Call a model fallback chain and return (parsed_json, model_id).

    `candidates` selects which strength tier to try first (defaults to
    STRONG_MODEL_CANDIDATES); `chain` is just a label so each tier remembers
    its own last-good-model index independently. Cooldowns/fail-streaks are
    shared across chains since they key off the actual model_id — a model
    cooling down for one caller is cooling down for both. Groq is only a
    last resort and only when allow_groq is True. model_id is the identifier
    of whichever model actually produced the answer — Groq answers are
    tagged ``groq:<model>`` so callers can tell a Gemma score from a Groq
    score and later re-score the Groq ones on Gemma.
    """
    if candidates is None:
        candidates = STRONG_MODEL_CANDIDATES
    last_err = None
    n = len(candidates)
    now = time.monotonic()
    start_idx = _last_good_idx.get(chain, 0)
    for offset in range(n):
        idx = (start_idx + offset) % n
        model_id = candidates[idx]
        if _cooldown_until.get(model_id, 0) > now:
            continue
        _throttle(model_id)
        try:
            if "gemma" in model_id and _GEMMA_GEN_CONFIG is not None:
                resp = client.models.generate_content(
                    model=model_id, contents=prompt, config=_GEMMA_GEN_CONFIG)
            else:
                resp = client.models.generate_content(model=model_id, contents=prompt)
            text = re.sub(r"^```(?:json)?\s*", "", resp.text.strip())
            text = re.sub(r"\s*```$", "", text)
            data = json.loads(text)
            _last_good_idx[chain] = idx
            _fail_streak[model_id] = 0
            return data, model_id
        except Exception as e:
            last_err = e
            is_rate_limit = "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)
            print(f"    {model_id} failed ({'rate limit' if is_rate_limit else 'error'}): {e}")
            if is_rate_limit:
                time.sleep(2)
            _fail_streak[model_id] = _fail_streak.get(model_id, 0) + 1
            if _fail_streak[model_id] >= 3:
                _cooldown_until[model_id] = time.monotonic() + _COOLDOWN_SECONDS
                print(f"    {model_id} failed 3x in a row — cooling down {_COOLDOWN_SECONDS}s")
    if allow_groq and GROQ_API_KEY:
        try:
            _throttle("groq")
            data = _call_groq(prompt)
            print(f"    {GROQ_MODEL} (groq) succeeded")
            return data, f"groq:{GROQ_MODEL}"
        except Exception as e:
            print(f"    {GROQ_MODEL} (groq) failed: {e}")
            last_err = e
    if last_err is None:
        last_err = RuntimeError("all Gemini models are marked dead this run and Groq is unavailable")
    raise last_err

def _call_groq(prompt):
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={
            "model": GROQ_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
        },
        timeout=30,
    )
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"].strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)

def _summarize_eval_model(models):
    """Collapse the per-call model ids used for one paper into a single label.

    Returns the most common model. If any call fell back to Groq, the paper is
    labelled with that Groq id so ``--reeval-groq`` can find and re-score it on
    Gemma once quota is available.
    """
    if not models:
        return ""
    groq = [m for m in models if m.startswith("groq:")]
    if groq:
        return groq[0]
    return max(set(models), key=models.count)


def evaluate_paper(paper, allow_groq=True, force_model=None, candidates=None):
    """Score a paper. Every call prefers the strongest model (Gemma) first,
    falling through the candidate list only on failure.

    candidates overrides the default STRONG_MODEL_CANDIDATES chain — e.g.
    GEMMA_ONLY_CANDIDATES for reeval_to_gemma(), where falling through to
    gemini-3.1-flash-lite would defeat the point of the re-eval.

    force_model pins every call in this evaluation to exactly one model with
    no fallback at all — used only by model_comparison_pilot.py to see what
    each model independently produces for the same paper. Pass a Gemini/Gemma
    model id, or ``"groq:<model>"`` to force Groq.
    """
    abstract = paper.get("abstract", "").strip() or "(no abstract available)"
    truncated = abstract[:1200]
    models_used = []

    if force_model is not None:
        is_groq_force = force_model.startswith("groq:")
        eval_candidates = [] if is_groq_force else [force_model]
        eval_allow_groq = True if is_groq_force else False
    else:
        eval_candidates = candidates if candidates is not None else STRONG_MODEL_CANDIDATES
        eval_allow_groq = allow_groq

    # 1. Meta: summary, opportunity, fields
    try:
        meta, meta_model = _call_gemini(META_PROMPT.format(
            title=paper["title"], abstract=truncated, fields=json.dumps(FIELD_TAGS),
        ), allow_groq=eval_allow_groq, candidates=eval_candidates, chain="strong")
        models_used.append(meta_model)
    except Exception as e:
        print(f"  Gemini error (meta): {e}")
        return None
    time.sleep(0.4)

    # 2. Per-parameter scoring (separate call each)
    breakdown = {}
    scores = []
    for param_name, param_desc in SCORE_PARAMS:
        try:
            data, param_model = _call_gemini(PARAM_PROMPT.format(
                param_name=param_name, param_desc=param_desc,
                title=paper["title"], abstract=truncated,
            ), allow_groq=eval_allow_groq, candidates=eval_candidates, chain="strong")
            s = max(1, min(10, int(data.get("score", 5))))
            breakdown[param_name] = {"score": s, "reason": data.get("reason", "")}
            scores.append(s)
            models_used.append(param_model)
        except Exception as e:
            print(f"  Gemini error ({param_name}): {e}")
            continue
        time.sleep(0.4)

    if not scores:
        return None

    composite = sum(scores)  # total out of 50
    return {
        "score":           composite,
        "summary":         fix_encoding(meta.get("summary", "")),
        "opportunity":     fix_encoding(meta.get("opportunity", "")),
        "fields":          meta.get("fields", []),
        "score_breakdown": breakdown,
        "eval_model":      _summarize_eval_model(models_used),
    }

# ── HTML Generation ────────────────────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>HUJI Research Monitor</title>
<style>
  :root {{
    --bg:#f5f6fa; --card:#ffffff; --card2:#eef0f7;
    --accent:#5b50e8; --accent2:#4338ca; --accent3:#0284c7;
    --text:#1e2130; --muted:#6b7280; --faint:#d1d5db;
    --border:#dde1ef; --tag-bg:#eef0f8;
    --green:#16a34a; --yellow:#b45309; --red:#dc2626;
    --green-bg:#dcfce7; --yellow-bg:#fef9c3; --red-bg:#fee2e2;
    --shadow:0 4px 24px rgba(0,0,0,.08);
    --header-bg:linear-gradient(135deg,#4338ca 0%,#5b50e8 100%);
    --header-border:rgba(255,255,255,.15);
    --header-shadow:0 2px 16px rgba(67,56,202,.25);
    --header-h1:#ffffff; --header-subtitle:rgba(255,255,255,.75);
    --controls-bg:linear-gradient(to bottom,rgba(238,240,247,.95),transparent);
  }}
  [data-theme=dark] {{
    --bg:#0b0d18; --card:#13162a; --card2:#1a1d35;
    --accent:#7c6ff7; --accent2:#b39dff; --accent3:#38bdf8;
    --text:#dde4f0; --muted:#7a8499; --faint:#3a3f5c;
    --border:#252a45; --tag-bg:#1e2240;
    --green:#34d399; --yellow:#fbbf24; --red:#f87171;
    --green-bg:#052e1c; --yellow-bg:#2d1f02; --red-bg:#2a0a0a;
    --shadow:0 4px 24px rgba(0,0,0,.45);
    --header-bg:linear-gradient(135deg,#0f1225 0%,#161a36 100%);
    --header-border:var(--border);
    --header-shadow:0 2px 16px rgba(0,0,0,.4);
    --header-h1:var(--accent2); --header-subtitle:var(--muted);
    --controls-bg:linear-gradient(to bottom,rgba(22,26,54,.9),transparent);
  }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh}}

  /* ── Header ── */
  header{{
    background:var(--header-bg);
    border-bottom:1px solid var(--header-border);
    padding:14px 24px;display:flex;align-items:center;gap:14px;flex-wrap:wrap;
    box-shadow:var(--header-shadow);
  }}
  .logo{{width:34px;height:34px;border-radius:8px;
    background:linear-gradient(135deg,var(--accent) 0%,var(--accent3) 100%);
    display:flex;align-items:center;justify-content:center;
    font-size:.75rem;font-weight:900;color:#fff;letter-spacing:-.5px;flex-shrink:0}}
  .header-title{{display:flex;flex-direction:column;gap:1px}}
  header h1{{font-size:1.1rem;font-weight:700;color:var(--header-h1);letter-spacing:-.01em}}
  header .subtitle{{font-size:.72rem;color:var(--header-subtitle)}}
  .header-links{{display:flex;gap:6px;margin-left:auto;flex-wrap:wrap;align-items:center}}
  .header-link{{
    padding:5px 11px;border-radius:6px;
    border:1px solid rgba(255,255,255,.25);background:rgba(255,255,255,.12);
    color:rgba(255,255,255,.85);font-size:.72rem;text-decoration:none;
    transition:all .18s;white-space:nowrap
  }}
  .header-link:hover{{background:rgba(255,255,255,.25);border-color:rgba(255,255,255,.5);color:#fff}}
  [data-theme=dark] .header-link{{border-color:var(--border);background:rgba(255,255,255,.04);color:var(--muted)}}
  [data-theme=dark] .header-link:hover{{background:var(--accent);border-color:var(--accent);color:#fff;box-shadow:0 0 12px rgba(124,111,247,.4)}}

  /* ── Controls ── */
  .controls{{
    padding:14px 24px 10px;display:flex;flex-direction:column;gap:10px;
    background:var(--controls-bg);
    border-bottom:1px solid var(--border);
  }}
  .row{{display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
  .row label{{font-size:.68rem;color:var(--muted);min-width:48px;text-transform:uppercase;letter-spacing:.07em;font-weight:600}}
  .chip{{
    padding:4px 11px;border-radius:999px;border:1px solid var(--border);
    background:var(--tag-bg);color:var(--muted);font-size:.72rem;cursor:pointer;
    transition:all .15s;
  }}
  .chip:hover{{background:var(--faint);color:var(--text);border-color:var(--accent)}}
  .chip.active{{background:var(--accent);border-color:var(--accent);color:#fff;box-shadow:0 0 10px rgba(124,111,247,.35)}}
  .search-wrap{{position:relative;flex:1}}
  .search-icon{{position:absolute;left:11px;top:50%;transform:translateY(-50%);color:var(--muted);font-size:.85rem;pointer-events:none}}
  input[type=text]{{
    width:100%;background:var(--card2);border:1px solid var(--border);border-radius:8px;
    padding:8px 14px 8px 32px;color:var(--text);font-size:.85rem;outline:none;transition:border-color .18s
  }}
  input[type=text]:focus{{border-color:var(--accent);box-shadow:0 0 0 3px rgba(124,111,247,.15)}}
  .sort-select{{
    background:var(--card2);border:1px solid var(--border);border-radius:8px;
    padding:8px 12px;color:var(--text);font-size:.82rem;cursor:pointer;outline:none
  }}
  .search-row{{display:flex;gap:8px}}

  /* ── Grid ── */
  .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:16px;padding:16px 24px 40px}}
  .count{{font-size:.78rem;color:var(--muted);padding:10px 24px 2px;letter-spacing:.02em}}

  /* ── Card ── */
  .card{{
    background:var(--card);border:1px solid var(--border);border-radius:14px;
    padding:18px;display:flex;flex-direction:column;gap:10px;
    transition:border-color .2s,box-shadow .2s,transform .15s;
    position:relative;overflow:hidden;
  }}
  .card::before{{
    content:'';position:absolute;inset:0;border-radius:14px;
    background:linear-gradient(135deg,rgba(124,111,247,.06) 0%,transparent 60%);
    opacity:0;transition:opacity .25s;pointer-events:none
  }}
  .card:hover{{border-color:var(--accent);box-shadow:0 6px 28px rgba(0,0,0,.5),0 0 0 1px rgba(124,111,247,.2);transform:translateY(-1px)}}
  .card:hover::before{{opacity:1}}

  .card-header{{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}}
  .title{{font-size:.88rem;font-weight:600;line-height:1.45;color:var(--text);flex:1}}
  .score-badge{{
    flex-shrink:0;display:flex;flex-direction:column;align-items:center;justify-content:center;
    min-width:52px;padding:5px 6px;border-radius:10px;font-weight:800;gap:1px
  }}
  .score-num{{font-size:1rem;line-height:1}}
  .score-denom{{font-size:.6rem;opacity:.7;line-height:1}}
  .score-high{{background:var(--green-bg);color:var(--green);border:1px solid rgba(52,211,153,.25)}}
  .score-mid{{background:var(--yellow-bg);color:var(--yellow);border:1px solid rgba(251,191,36,.25)}}
  .score-low{{background:var(--red-bg);color:var(--red);border:1px solid rgba(248,113,113,.25)}}

  .meta-row{{display:flex;align-items:center;gap:6px;flex-wrap:wrap}}
  .meta{{font-size:.72rem;color:var(--muted)}}
  .source-badge{{
    font-size:.62rem;padding:1px 7px;border-radius:4px;font-weight:600;letter-spacing:.03em;
    background:rgba(56,189,248,.12);color:var(--accent3);border:1px solid rgba(56,189,248,.2)
  }}
  .model-badge{{
    font-size:.6rem;padding:1px 7px;border-radius:4px;font-weight:600;letter-spacing:.02em;
    background:var(--tag-bg);color:var(--muted);border:1px solid var(--faint)
  }}
  .model-badge.model-groq{{background:var(--yellow-bg);color:var(--yellow);border-color:rgba(251,191,36,.3)}}
  .model-badge.model-gemma{{background:var(--green-bg);color:var(--green);border-color:rgba(52,211,153,.25)}}

  .summary{{font-size:.8rem;color:#9aa5bc;line-height:1.6}}
  .opportunity{{
    font-size:.8rem;
    background:linear-gradient(135deg,#1a1552 0%,#0f1535 100%);
    border-left:3px solid var(--accent);
    padding:8px 12px;border-radius:0 8px 8px 0;
    color:var(--accent2);line-height:1.5;
    box-shadow:inset 0 0 20px rgba(124,111,247,.05)
  }}

  .tags{{display:flex;flex-wrap:wrap;gap:5px}}
  .tag{{
    padding:2px 9px;border-radius:999px;
    background:var(--tag-bg);border:1px solid var(--faint);
    font-size:.68rem;color:var(--muted);transition:all .12s;cursor:default
  }}
  .tag:hover{{border-color:var(--accent);color:var(--accent2)}}

  .pi{{display:flex;align-items:center;flex-wrap:wrap;gap:6px;padding:6px 10px;
    background:rgba(255,255,255,.03);border-radius:8px;border:1px solid var(--border)}}
  .pi-label{{font-size:.62rem;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);font-weight:700;flex-shrink:0}}
  .pi-name{{font-size:.83rem;font-weight:700;color:var(--accent2)}}
  .pi-email-btn{{padding:2px 8px;font-size:.65rem;margin-left:auto}}
  .pi-email{{font-size:.75rem;padding:2px 0}}
  .pi-email a{{color:var(--accent3);text-decoration:none}}
  .pi-email a:hover{{text-decoration:underline}}
  .pi-affiliation{{font-size:.72rem;color:var(--muted);padding:0 2px}}

  .actions{{display:flex;gap:7px;margin-top:2px}}
  .btn{{
    padding:5px 13px;border-radius:7px;border:1px solid var(--border);
    background:transparent;color:var(--muted);font-size:.73rem;cursor:pointer;
    transition:all .15s;text-decoration:none;display:inline-block
  }}
  .btn:hover,.btn.active{{background:var(--accent);border-color:var(--accent);color:#fff;box-shadow:0 0 10px rgba(124,111,247,.3)}}

  .empty{{text-align:center;padding:60px 24px;color:var(--muted)}}

  /* ── Score Breakdown ── */
  .breakdown{{display:none;flex-direction:column;gap:8px;border-top:1px solid var(--border);padding-top:12px;margin-top:2px}}
  .breakdown.open{{display:flex}}
  .bd-row{{display:flex;flex-direction:column;gap:4px}}
  .bd-label{{display:flex;justify-content:space-between;align-items:center}}
  .bd-name{{font-size:.7rem;font-weight:600;color:var(--text);text-transform:capitalize;letter-spacing:.02em}}
  .bd-score{{font-size:.7rem;font-weight:800}}
  .bd-bar-bg{{height:4px;background:var(--border);border-radius:2px;overflow:hidden}}
  .bd-bar{{height:100%;border-radius:2px;transition:width .4s cubic-bezier(.4,0,.2,1)}}
  .bd-reason{{font-size:.68rem;color:var(--muted);line-height:1.4;margin-top:1px}}

  /* ── Slider ── */
  .slider{{-webkit-appearance:none;appearance:none;height:4px;border-radius:2px;background:var(--border);outline:none;cursor:pointer;width:140px;vertical-align:middle}}
  .slider::-webkit-slider-thumb{{-webkit-appearance:none;appearance:none;width:15px;height:15px;border-radius:50%;background:var(--accent);cursor:pointer;box-shadow:0 0 6px rgba(124,111,247,.5)}}
  .slider::-moz-range-thumb{{width:15px;height:15px;border-radius:50%;background:var(--accent);cursor:pointer;border:none}}
  .slider:disabled{{opacity:.3;cursor:not-allowed}}
  .slider-val{{font-size:.73rem;color:var(--accent2);font-weight:700;min-width:44px;display:inline-block}}

  .theme-toggle{{cursor:pointer;font-size:.72rem;background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.25);color:rgba(255,255,255,.85);padding:5px 11px;border-radius:6px;transition:all .18s;white-space:nowrap}}
  .theme-toggle:hover{{background:rgba(255,255,255,.25);border-color:rgba(255,255,255,.5);color:#fff}}
  [data-theme=dark] .theme-toggle{{background:rgba(255,255,255,.04);border-color:var(--border);color:var(--muted)}}
  [data-theme=dark] .theme-toggle:hover{{background:var(--accent);border-color:var(--accent);color:#fff}}

  /* ── Branch Tabs ── */
  .branch-tabs{{display:flex;gap:0;background:var(--card2);border-bottom:2px solid var(--border);padding:0 24px;overflow-x:auto;flex-shrink:0}}
  .branch-tab{{padding:10px 18px;border:none;background:transparent;color:var(--muted);font-size:.82rem;font-weight:600;cursor:pointer;border-bottom:3px solid transparent;margin-bottom:-2px;transition:all .18s;white-space:nowrap;letter-spacing:.01em}}
  .branch-tab:hover{{color:var(--text)}}
  .branch-tab.active{{color:var(--accent);border-bottom-color:var(--accent)}}

  /* ── Page Tabs (Papers / Researchers) ── */
  .page-tabs{{display:flex;gap:8px;background:var(--header-bg);padding:0 24px;overflow-x:auto;flex-shrink:0}}
  .page-tab{{padding:11px 20px;border:none;background:transparent;color:rgba(255,255,255,.65);font-size:.85rem;font-weight:700;cursor:pointer;border-bottom:3px solid transparent;margin-bottom:-1px;transition:all .18s;white-space:nowrap;letter-spacing:.01em}}
  .page-tab:hover{{color:#fff}}
  .page-tab.active{{color:#fff;border-bottom-color:#fff}}

  /* ── Researcher Card ── */
  .r-card{{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:18px;display:flex;flex-direction:column;gap:10px}}
  .r-card-header{{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}}
  .r-name{{font-size:1rem;font-weight:700;color:var(--text)}}
  .r-meta{{font-size:.72rem;color:var(--muted)}}
  .r-meta a{{color:var(--accent3);text-decoration:none}}
  .r-meta a:hover{{text-decoration:underline}}
  .r-affiliation{{font-size:.72rem;color:var(--muted);line-height:1.4}}
  .r-desc{{font-size:.8rem;color:#9aa5bc;line-height:1.6}}
  .r-applic{{
    font-size:.8rem;
    background:linear-gradient(135deg,#1a1552 0%,#0f1535 100%);
    border-left:3px solid var(--accent);
    padding:8px 12px;border-radius:0 8px 8px 0;
    color:var(--accent2);line-height:1.5;
    box-shadow:inset 0 0 20px rgba(124,111,247,.05)
  }}
  .r-applic-label{{font-size:.6rem;text-transform:uppercase;letter-spacing:.07em;font-weight:700;opacity:.8;display:block;margin-bottom:3px}}
  .r-papers{{display:none;flex-direction:column;gap:12px;border-top:1px solid var(--border);padding-top:12px;margin-top:2px}}
  .r-papers.open{{display:flex}}
  .r-paper{{display:flex;flex-direction:column;gap:6px}}
  .r-paper-row{{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;font-size:.76rem}}
  .r-paper-title{{color:var(--text);flex:1}}
  .r-paper-title a{{color:inherit;text-decoration:none}}
  .r-paper-title a:hover{{text-decoration:underline;color:var(--accent2)}}
  .r-paper-score{{font-weight:800;flex-shrink:0}}
  .r-paper .breakdown{{display:flex;margin-top:0;padding-top:8px}}
  .r-paper-toggle{{align-self:flex-start;font-size:.66rem;padding:2px 8px}}

  @media(max-width:600px){{
    .grid{{grid-template-columns:1fr;padding:12px 12px 32px}}
    .controls{{padding:10px 12px}}
    .slider{{width:100px}}
    .header-links{{gap:4px}}
    .branch-tab{{padding:8px 12px;font-size:.75rem}}
  }}
</style>
</head>
<body>
<header>
  <div class="logo">HU</div>
  <div class="header-title">
    <h1>HUJI Research Monitor</h1>
    <span class="subtitle" id="updated"></span>
  </div>
  <div class="header-links">{header_links}<button class="header-link theme-toggle" id="themeToggle" title="Toggle dark/light mode">🌙 Dark</button></div>
</header>
<div class="page-tabs">
  <button class="page-tab active" data-view="papers">📄 Papers</button>
  <button class="page-tab" data-view="researchers">🧑‍🔬 Researchers</button>
</div>
<div id="papers-view">
<div class="branch-tabs">
  <button class="branch-tab active" data-branch="all">All Branches</button>
  <button class="branch-tab" data-branch="Healthcare">🏥 Healthcare</button>
  <button class="branch-tab" data-branch="Agriculture &amp; Food">🌾 Agriculture &amp; Food</button>
  <button class="branch-tab" data-branch="Exact &amp; Social Sciences">💡 Exact &amp; Social Sciences</button>
</div>
<div class="controls">
  <div class="row search-row">
    <div class="search-wrap">
      <span class="search-icon">🔍</span>
      <input type="text" id="search" placeholder="Search titles, summaries, opportunities…"/>
    </div>
    <select class="sort-select" id="sort">
      <option value="score">Score ↓</option>
      <option value="date">Date ↓</option>
    </select>
  </div>
  <div class="row">
    <label>Period</label>
    <button class="chip active" data-filter="period" data-val="all" data-label="All time">All time</button>
    <button class="chip" data-filter="period" data-val="7" data-label="Last week">Last week</button>
    <button class="chip" data-filter="period" data-val="30" data-label="Last month">Last month</button>
  </div>
  <div class="row">
    <label>Score</label>
    <input type="range" id="score-slider" min="0" max="50" value="0" step="1" class="slider"/>
    <span id="score-val" class="slider-val">Any</span>
  </div>
  <div class="row">
    <label>Param</label>
    <select id="param-select" class="sort-select">
      <option value="">Any parameter</option>
      <option value="novelty">Novelty</option>
      <option value="commercial_potential">Commercial Potential</option>
      <option value="market_size">Market Size</option>
      <option value="trl">Tech Readiness</option>
      <option value="ip_strength">IP Strength</option>
    </select>
    <input type="range" id="param-slider" min="1" max="10" value="1" step="1" class="slider" disabled/>
    <span id="param-val" class="slider-val">—</span>
  </div>
  <div class="row">
    <label>Field</label>
    <button class="chip active" data-filter="field" data-val="all" data-label="All">All</button>
    {field_chips}
  </div>
</div>
<div class="count" id="count"></div>
<div class="grid" id="grid"></div>
</div>
<div id="researchers-view" style="display:none">
  <div class="branch-tabs">
    <button class="branch-tab r-branch-tab active" data-rbranch="all">All Branches</button>
    <button class="branch-tab r-branch-tab" data-rbranch="Healthcare">🏥 Healthcare</button>
    <button class="branch-tab r-branch-tab" data-rbranch="Agriculture &amp; Food">🌾 Agriculture &amp; Food</button>
    <button class="branch-tab r-branch-tab" data-rbranch="Exact &amp; Social Sciences">💡 Exact &amp; Social Sciences</button>
  </div>
  <div class="controls">
    <div class="row search-row">
      <div class="search-wrap">
        <span class="search-icon">🔍</span>
        <input type="text" id="r-search" placeholder="Search researchers, descriptions, fields…"/>
      </div>
      <select class="sort-select" id="r-sort">
        <option value="avg_score">Avg Applicability ↓</option>
        <option value="paper_count">Paper Count ↓</option>
        <option value="pi">Name A-Z</option>
      </select>
    </div>
  </div>
  <div class="count" id="r-count"></div>
  <div class="grid" id="r-grid"></div>
</div>
<script>
const papers = {papers_json};
const researchers = {researchers_json};
const BRANCHES = {branches_json};
const DIGEST_URLS = {digest_urls_json};
document.getElementById('updated').textContent = 'Updated {updated}';
const TODAY=new Date(); TODAY.setHours(0,0,0,0);
function parseDate(d){{
  if(!d)return null;
  // Try ISO: 2026-03-19
  let m=d.match(/^(\d{{4}})-(\d{{2}})-(\d{{2}})/);
  if(m)return new Date(+m[1],+m[2]-1,+m[3]);
  // Try: 2026 Mar 19 or 2026 Mar
  const MONTHS={{Jan:0,Feb:1,Mar:2,Apr:3,May:4,Jun:5,Jul:6,Aug:7,Sep:8,Oct:9,Nov:10,Dec:11}};
  m=d.match(/^(\d{{4}})\s+([A-Za-z]{{3}})(?:\s+(\d{{1,2}}))?/);
  if(m)return new Date(+m[1],MONTHS[m[2]]||0,m[3]?+m[3]:1);
  // Try year only
  m=d.match(/^(\d{{4}})$/);
  if(m)return new Date(+m[1],0,1);
  return null;
}}
function daysAgo(d){{const t=parseDate(d);if(!t)return 9999;t.setHours(0,0,0,0);return Math.round((TODAY-t)/86400000);}}
let activeScore=0, activeField='all', activePeriod='all', activeBranch='all', sortBy='score', searchQ='', activeParam='', activeParamMin=1;
const PARAM_LABELS = {{
  novelty:'Novelty', commercial_potential:'Commercial Potential',
  market_size:'Market Size', trl:'Tech Readiness', ip_strength:'IP Strength'
}};
function scoreClass(s){{return s>=38?'score-high':s>=28?'score-mid':'score-low';}}
function barColor(s){{return s>=8?'#22c55e':s>=5?'#eab308':'#ef4444';}}
function modelBadge(m,prevScore,prevModel){{
  if(!m)return '';
  const isGroq=m.indexOf('groq')===0;
  const cls=isGroq?'model-groq':(m.indexOf('gemma')>=0?'model-gemma':'');
  const label=isGroq?('groq: '+m.replace('groq:','')):m;
  const tip=isGroq?'Scored by Groq fallback — pending re-score on Gemma':'Scored by '+m;
  const upgraded=(prevScore!==undefined&&prevScore!==null&&prevScore!==''&&prevModel)
    ?` <span class="model-badge" title="Re-scored on Gemma — was ${{prevScore}}/50 via ${{prevModel}}">↑ was ${{prevScore}}</span>`:'';
  return `<span class="model-badge ${{cls}}" title="${{tip}}">🤖 ${{label}}</span>${{upgraded}}`;
}}
function renderBreakdown(bd){{
  if(!bd||!Object.keys(bd).length) return '';
  const rows=Object.entries(bd).map(([k,v])=>{{
    const label=PARAM_LABELS[k]||k;
    const pct=(v.score||0)*10;
    return `<div class="bd-row">
      <div class="bd-label"><span class="bd-name">${{label}}</span><span class="bd-score" style="color:${{barColor(v.score)}}">${{v.score}}/10</span></div>
      <div class="bd-bar-bg"><div class="bd-bar" style="width:${{pct}}%;background:${{barColor(v.score)}}"></div></div>
      ${{v.reason?`<div class="bd-reason">${{v.reason}}</div>`:''}}
    </div>`;
  }}).join('');
  return `<div class="breakdown">${{rows}}</div>`;
}}
function branchMatches(p){{
  // Returns an object {{branch: bool}} — true if this branch has the highest field-tag match.
  // Ties: the paper appears in ALL tied branches.
  const fields=p.fields||[];
  const counts={{}};
  for(const [b,bf] of Object.entries(BRANCHES)) counts[b]=fields.filter(f=>bf.includes(f)).length;
  const best=Math.max(...Object.values(counts));
  if(best===0)return null; // unclassified — only shows under "All"
  const result={{}};
  for(const b of Object.keys(BRANCHES)) result[b]=counts[b]===best;
  return result;
}}
function applyFilters(list,{{skipPeriod,skipField}}){{
  if(activeBranch!=='all')list=list.filter(p=>{{const m=branchMatches(p);return m&&m[activeBranch];}});
  if(searchQ){{const q=searchQ.toLowerCase();list=list.filter(p=>(p.title||'').toLowerCase().includes(q)||(p.summary||'').toLowerCase().includes(q)||(p.opportunity||'').toLowerCase().includes(q));}}
  if(!skipPeriod&&activePeriod!=='all')list=list.filter(p=>daysAgo(p.date)<=parseInt(activePeriod));
  if(activeScore>0)list=list.filter(p=>p.score>=activeScore);
  if(activeParam)list=list.filter(p=>((p.score_breakdown||{{}})[activeParam]||{{}}).score>=activeParamMin);
  if(!skipField&&activeField!=='all')list=list.filter(p=>(p.fields||[]).includes(activeField));
  return list;
}}
function render(){{
  let list=applyFilters(papers.slice(),{{}});
  list.sort(sortBy==='score'?(a,b)=>b.score-a.score:(a,b)=>(b.date||'').localeCompare(a.date||''));
  const n=list.length;
  document.getElementById('count').textContent=n+' paper'+(n!==1?'s':'')+' shown';
  // Update chip counts
  document.querySelectorAll('.chip').forEach(b=>{{
    const f=b.dataset.filter,v=b.dataset.val,lbl=b.dataset.label||v;
    let sub=applyFilters(papers.slice(),{{skipPeriod:f==='period',skipField:f==='field'}});
    if(f==='period'&&v!=='all')sub=sub.filter(p=>daysAgo(p.date)<=parseInt(v));
    if(f==='field'&&v!=='all')sub=sub.filter(p=>(p.fields||[]).includes(v));
    b.textContent=lbl+' ('+sub.length+')';
  }});
  // Update slider labels with current result count
  document.getElementById('score-val').textContent=activeScore>0?activeScore+'+ /50 · '+n:'Any · '+n;
  if(activeParam)document.getElementById('param-val').textContent=activeParamMin+'/10 · '+n;
  const grid=document.getElementById('grid');
  if(!list.length){{grid.innerHTML='<div class="empty">No papers match.</div>';return;}}
  grid.innerHTML=list.map((p,i)=>{{
    const tags=(p.fields||[]).map(f=>`<span class="tag">${{f}}</span>`).join('');
    const authors=(p.authors||[]).join(', ');
    const hasBd=p.score_breakdown&&Object.keys(p.score_breakdown).length>0;
    return `<div class="card">
      <div class="card-header"><div class="title">${{p.title}}</div><div class="score-badge ${{scoreClass(p.score)}}"><span class="score-num">${{p.score}}</span><span class="score-denom">/50</span></div></div>
      ${{(p.pi||p.pi_full_name)?`<div class="pi"><span class="pi-label">Main Researcher</span><span class="pi-name">👤 ${{p.pi_full_name||p.pi}}</span>${{p.pi_email?`<button class="btn pi-email-btn" onclick="toggleEmail(this)">Email ▾</button>`:''}} </div>${{p.pi_affiliation?`<div class="pi-affiliation">${{p.pi_affiliation}}</div>`:''}}${{p.pi_email?`<div class="pi-email" style="display:none"><a href="mailto:${{p.pi_email}}">${{p.pi_email}}</a></div>`:''}}`:''}}
      <div class="meta-row"><div class="meta">${{authors?authors+' · ':''}}${{p.journal||''}}${{p.date?' · '+p.date:''}}</div>${{p.source?`<span class="source-badge">${{p.source}}</span>`:''}}${{modelBadge(p.eval_model,p.prev_score,p.prev_eval_model)}}</div>
      ${{p.summary?`<div class="summary">${{p.summary}}</div>`:''}}
      ${{p.opportunity?`<div class="opportunity">${{p.opportunity}}</div>`:''}}
      ${{hasBd?renderBreakdown(p.score_breakdown):''}}
      ${{tags?`<div class="tags">${{tags}}</div>`:''}}
      <div class="actions">
        <a class="btn" href="${{p.url}}" target="_blank">Open Paper</a>
        ${{hasBd?`<button class="btn" onclick="toggleBd(this)">Score Breakdown ▾</button>`:''}}
      </div>
    </div>`;
  }}).join('');
}}
function toggleBd(btn){{
  const container=btn.closest('.r-paper')||btn.closest('.card');
  if(!container)return;
  const bd=container.querySelector('.breakdown');
  if(!bd)return;
  bd.classList.toggle('open');
  btn.classList.toggle('active');
  btn.textContent=bd.classList.contains('open')?'Score Breakdown ▴':'Score Breakdown ▾';
}}
function toggleEmail(btn){{
  const div=btn.closest('.card').querySelector('.pi-email');
  if(!div)return;
  const open=div.style.display!=='none';
  div.style.display=open?'none':'block';
  btn.textContent=open?'Email ▾':'Email ▴';
  btn.classList.toggle('active',!open);
}}
document.querySelectorAll('.chip').forEach(b=>b.addEventListener('click',()=>{{
  const f=b.dataset.filter,v=b.dataset.val;
  document.querySelectorAll(`.chip[data-filter="${{f}}"]`).forEach(x=>x.classList.remove('active'));
  b.classList.add('active');
  if(f==='field')activeField=v; else activePeriod=v;
  render();
}}));
document.querySelectorAll('.branch-tab').forEach(tab=>tab.addEventListener('click',()=>{{
  document.querySelectorAll('.branch-tab').forEach(t=>t.classList.remove('active'));
  tab.classList.add('active');
  activeBranch=tab.dataset.branch;
  // Reset field filter to 'all' when switching branches
  activeField='all';
  document.querySelectorAll('.chip[data-filter="field"]').forEach(x=>x.classList.remove('active'));
  document.querySelector('.chip[data-filter="field"][data-val="all"]').classList.add('active');
  // Update Weekly/Monthly digest links for this branch
  const urls=(DIGEST_URLS[activeBranch]||DIGEST_URLS['all']||{{}});
  const wl=document.getElementById('link-weekly'),ml=document.getElementById('link-monthly');
  if(wl&&urls.weekly)wl.href=urls.weekly;
  if(ml&&urls.monthly)ml.href=urls.monthly;
  render();
}}));
document.getElementById('score-slider').addEventListener('input',function(){{
  activeScore=parseInt(this.value);
  render();
}});
(function(){{
  const sel=document.getElementById('param-select');
  const sl=document.getElementById('param-slider');
  const lbl=document.getElementById('param-val');
  sel.addEventListener('change',function(){{
    activeParam=this.value;
    sl.disabled=!activeParam;
    if(!activeParam)lbl.textContent='—';
    render();
  }});
  sl.addEventListener('input',function(){{
    activeParamMin=parseInt(this.value);
    render();
  }});
}})();
document.getElementById('sort').addEventListener('change',e=>{{sortBy=e.target.value;render();}});
document.getElementById('search').addEventListener('input',e=>{{searchQ=e.target.value.trim();render();}});

// ── Researchers tab ──
let rSortBy='avg_score', rSearchQ='', rBranch='all';
function researcherBranches(r){{
  // Prefer stored branches; otherwise derive from aggregated field tags.
  if(r.branches&&r.branches.length)return r.branches;
  const fields=r.fields||[];
  return Object.keys(BRANCHES).filter(b=>BRANCHES[b].some(f=>fields.includes(f)));
}}
function renderResearchers(){{
  let list=researchers.slice();
  if(rBranch!=='all')list=list.filter(r=>researcherBranches(r).includes(rBranch));
  if(rSearchQ){{const q=rSearchQ.toLowerCase();list=list.filter(r=>(r.pi_full_name||r.pi||'').toLowerCase().includes(q)||(r.description||'').toLowerCase().includes(q)||(r.applicability||'').toLowerCase().includes(q)||(r.fields||[]).join(' ').toLowerCase().includes(q));}}
  if(rSortBy==='pi')list.sort((a,b)=>(a.pi_full_name||a.pi||'').localeCompare(b.pi_full_name||b.pi||''));
  else list.sort((a,b)=>(b[rSortBy]||0)-(a[rSortBy]||0));
  const n=list.length;
  document.getElementById('r-count').textContent=n+' researcher'+(n!==1?'s':'')+' shown';
  const grid=document.getElementById('r-grid');
  if(!list.length){{grid.innerHTML='<div class="empty">No researcher profiles match.</div>';return;}}
  grid.innerHTML=list.map(r=>{{
    const name=r.pi_full_name||r.pi||'Unknown';
    const tags=(r.fields||[]).map(f=>`<span class="tag">${{f}}</span>`).join('');
    const papers=(r.papers||[]).slice().sort((a,b)=>(b.score||0)-(a.score||0));
    const rows=papers.map(p=>{{
      const hasBd=p.score_breakdown&&Object.keys(p.score_breakdown).length>0;
      const ptags=(p.fields||[]).map(f=>`<span class="tag">${{f}}</span>`).join('');
      return `<div class="r-paper">
        <div class="r-paper-row">
          <span class="r-paper-title"><a href="${{p.url}}" target="_blank">${{p.title}}</a>${{p.date?' ('+p.date+')':''}} ${{modelBadge(p.eval_model,p.prev_score,p.prev_eval_model)}}</span>
          <span class="r-paper-score ${{scoreClass(p.score)}}">${{p.score}}/50</span>
        </div>
        ${{ptags?`<div class="tags">${{ptags}}</div>`:''}}
        ${{hasBd?renderBreakdown(p.score_breakdown):''}}
        ${{hasBd?`<button class="btn r-paper-toggle" onclick="toggleBd(this)">Score Breakdown ▾</button>`:''}}
      </div>`;
    }}).join('');
    return `<div class="r-card">
      <div class="r-card-header">
        <div>
          <div class="r-name">👤 ${{name}}</div>
          <div class="r-meta">${{r.paper_count||0}} graded paper${{(r.paper_count||0)!==1?'s':''}}${{r.pi_email?` · <a href="mailto:${{r.pi_email}}">${{r.pi_email}}</a>`:''}}</div>
        </div>
        <div class="score-badge ${{scoreClass(Math.round(r.avg_score||0))}}"><span class="score-num">${{(r.avg_score||0).toFixed(1)}}</span><span class="score-denom">avg /50</span></div>
      </div>
      ${{r.pi_affiliation?`<div class="r-affiliation">🏛️ ${{r.pi_affiliation}}</div>`:''}}
      ${{r.description?`<div class="r-desc">${{r.description}}</div>`:''}}
      ${{r.applicability?`<div class="r-applic"><span class="r-applic-label">Applicability</span>${{r.applicability}}</div>`:''}}
      ${{tags?`<div class="tags">${{tags}}</div>`:''}}
      <div class="actions">
        <button class="btn" onclick="toggleRPapers(this)">Graded Publications ▾</button>
      </div>
      <div class="r-papers">${{rows||'<div class="r-paper-row">No papers found.</div>'}}</div>
    </div>`;
  }}).join('');
}}
function toggleRPapers(btn){{
  const card=btn.closest('.r-card');
  const list=card.querySelector('.r-papers');
  if(!list)return;
  list.classList.toggle('open');
  btn.classList.toggle('active');
  btn.textContent=list.classList.contains('open')?'Graded Publications ▴':'Graded Publications ▾';
}}
document.getElementById('r-search').addEventListener('input',e=>{{rSearchQ=e.target.value.trim();renderResearchers();}});
document.getElementById('r-sort').addEventListener('change',e=>{{rSortBy=e.target.value;renderResearchers();}});
document.querySelectorAll('.r-branch-tab').forEach(tab=>tab.addEventListener('click',()=>{{
  document.querySelectorAll('.r-branch-tab').forEach(t=>t.classList.remove('active'));
  tab.classList.add('active');
  rBranch=tab.dataset.rbranch;
  renderResearchers();
}}));
document.querySelectorAll('.page-tab').forEach(tab=>tab.addEventListener('click',()=>{{
  document.querySelectorAll('.page-tab').forEach(t=>t.classList.remove('active'));
  tab.classList.add('active');
  const view=tab.dataset.view;
  document.getElementById('papers-view').style.display=view==='papers'?'':'none';
  document.getElementById('researchers-view').style.display=view==='researchers'?'':'none';
}}));
renderResearchers();
(function(){{
  const btn=document.getElementById('themeToggle');
  const saved=localStorage.getItem('huji-theme');
  if(saved==='dark'){{document.documentElement.setAttribute('data-theme','dark');btn.textContent='☀️ Light';}}
  btn.addEventListener('click',function(){{
    const isDark=document.documentElement.getAttribute('data-theme')==='dark';
    if(isDark){{document.documentElement.removeAttribute('data-theme');localStorage.setItem('huji-theme','light');btn.textContent='🌙 Dark';}}
    else{{document.documentElement.setAttribute('data-theme','dark');localStorage.setItem('huji-theme','dark');btn.textContent='☀️ Light';}}
  }});
}})();
render();
</script>
</body>
</html>
"""

def build_field_chips():
    return "\n    ".join(
        f'<button class="chip" data-filter="field" data-val="{f}" data-label="{f}">{f}</button>'
        for f in FIELD_TAGS
    )

def generate_html(papers, researchers=None):
    if researchers is None:
        # Weekly pipeline calls generate_html() without knowing about researcher
        # profiles — carry forward whatever researcher_pipeline.py last produced
        # instead of wiping the Researchers tab.
        if RESEARCHERS_JSON.exists():
            try:
                researchers = json.loads(RESEARCHERS_JSON.read_text(encoding="utf-8"))
            except Exception:
                researchers = []
        else:
            researchers = []

    enriched = sorted([{
        "id":              p["id"],
        "title":           p.get("title", ""),
        "authors":         p.get("authors", []),
        "journal":         p.get("journal", ""),
        "date":            p.get("date", ""),
        "url":             p.get("url", ""),
        "source":          p.get("source", ""),
        "score":           p.get("score", 0),
        "summary":         p.get("summary", ""),
        "opportunity":     p.get("opportunity", ""),
        "fields":          p.get("fields", []),
        "score_breakdown": p.get("score_breakdown", {}),
        "pi":              p.get("pi", ""),
        "pi_full_name":    p.get("pi_full_name", ""),
        "pi_email":        p.get("pi_email", ""),
        "pi_affiliation":  p.get("pi_affiliation", ""),
        "eval_model":      p.get("eval_model", ""),
        "prev_score":      p.get("prev_score", ""),
        "prev_eval_model": p.get("prev_eval_model", ""),
        "added_date":      p.get("added_date", ""),
    } for p in papers], key=lambda x: x["score"], reverse=True)

    # Build header links: latest weekly + monthly digests + spreadsheet + user guide
    header_links = ""
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "")

    # Build per-branch digest URL map and header links
    digest_urls = {"all": {"weekly": "", "monthly": ""}}
    for _b in BRANCHES:
        digest_urls[_b] = {"weekly": "", "monthly": ""}

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if repo:
        owner, rname = (repo.split("/") + [""])[:2]
        base = f"https://{owner}.github.io/{rname}"
        digests_dir = Path("digests")
        if digests_dir.is_dir():
            # Scan PDFs newest-first; assign to branch key by stem suffix
            for pdf in reversed(sorted(digests_dir.glob("HUJI_digest_*.pdf"))):
                parts = pdf.stem.split("_")   # ["HUJI","digest","YYYY","W##", ...]
                if len(parts) < 4:
                    continue
                is_weekly = parts[3].startswith("W")
                suffix = "_".join(parts[4:])  # "" for all-branches, "Healthcare" etc.
                if not suffix:
                    key = "all"
                else:
                    key = next((b for b in BRANCHES
                                if b.replace(" & ", "_").replace(" ", "_") == suffix), None)
                if key is None:
                    continue
                field = "weekly" if is_weekly else "monthly"
                if not digest_urls[key][field]:
                    digest_urls[key][field] = f"{base}/digests/{pdf.name}"

            # Header links use "all" digest (JS overrides per active branch)
            if digest_urls["all"]["weekly"]:
                header_links += (f'<a class="header-link" id="link-weekly" '
                                 f'href="{digest_urls["all"]["weekly"]}" target="_blank">📄 Weekly Report</a>')
            if digest_urls["all"]["monthly"]:
                header_links += (f'<a class="header-link" id="link-monthly" '
                                 f'href="{digest_urls["all"]["monthly"]}" target="_blank">📅 Monthly Report</a>')

        if sheet_id:
            sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"
            header_links += f'<a class="header-link" href="{sheet_url}" target="_blank">📊 All Papers Spreadsheet</a>'

        manual = Path("docs/HUJI_Research_Monitor_Guide.pdf")
        if manual.exists():
            manual_url = f"{base}/docs/{manual.name}"
            header_links += f'<a class="header-link" href="{manual_url}" target="_blank">📖 User Guide</a>'

    OUTPUT_HTML.write_text(HTML_TEMPLATE.format(
        field_chips=build_field_chips(),
        papers_json=json.dumps(enriched, ensure_ascii=False),
        researchers_json=json.dumps(researchers, ensure_ascii=False),
        branches_json=json.dumps(BRANCHES, ensure_ascii=False),
        digest_urls_json=json.dumps(digest_urls, ensure_ascii=False),
        updated=today_str(),
        header_links=header_links,
    ), encoding="utf-8")
    OUTPUT_JSON.write_text(json.dumps(enriched, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Generated {OUTPUT_HTML} and {OUTPUT_JSON} with {len(enriched)} papers "
          f"and {len(researchers)} researcher profiles.")

# ── Re-evaluation of Groq-scored papers ─────────────────────────────────────────

def _fetch_abstract_for_paper(paper):
    """Best-effort re-fetch of a paper's abstract from its source, by id.

    Sheet rows don't persist abstracts, so re-scoring needs to fetch them
    again. Returns the abstract string (may be empty if unavailable).
    """
    pid = paper.get("id", "")
    try:
        if pid.startswith("pubmed_"):
            pmid = pid.split("_", 1)[1]
            # NCBI rate-limits unauthenticated clients to ~3 req/sec; a reeval
            # pass hitting this per-paper in a tight loop trips 429s often
            # enough to silently drop papers from re-scoring without a retry.
            for attempt, delay in enumerate([0, 1, 3, 8]):
                if delay:
                    time.sleep(delay)
                r = requests.get(
                    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                    params={"db": "pubmed", "id": pmid, "rettype": "xml", "retmode": "xml"},
                    timeout=30,
                )
                if r.status_code != 429 or attempt == 3:
                    break
            r.raise_for_status()
            root = ET.fromstring(r.text)
            return " ".join((el.text or "") for el in root.findall(".//Abstract/AbstractText")).strip()
        if pid.startswith("epmc_"):
            eid = pid.split("_", 1)[1]
            r = requests.get(
                "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                params={"query": f"EXT_ID:{eid}", "resultType": "core", "format": "json"},
                timeout=30,
            )
            r.raise_for_status()
            items = r.json().get("resultList", {}).get("result", [])
            return (items[0].get("abstractText", "") if items else "").strip()
        if pid.startswith("ss_"):
            sid = pid.split("_", 1)[1]
            r = requests.get(
                f"https://api.semanticscholar.org/graph/v1/paper/{sid}",
                params={"fields": "abstract"}, timeout=30,
            )
            r.raise_for_status()
            return (r.json().get("abstract") or "").strip()
    except Exception as e:
        print(f"    abstract refetch failed for {pid}: {e}")
    return ""


def _is_gemma_model(model_id):
    return any(g in str(model_id) for g in GEMMA_ONLY_CANDIDATES)


def reeval_to_gemma_papers(papers):
    """Re-score any paper NOT currently scored by Gemma (empty/gemini/groq
    eval_model), using Gemma only — no fallback to gemini-3.1-flash-lite or
    Groq within this pass, since that would defeat the point.

    The paper's previous score/model are preserved in prev_score/
    prev_eval_model (only ever set once, on the first successful re-eval, so
    repeated runs don't overwrite the original baseline with an intermediate
    one) — this is what model_comparison_pilot.py and the dashboard can use
    to see the real effect of switching a paper from one model to another.

    Papers where the Gemma re-score fails (e.g. still down) are left exactly
    as they were, so this is safe to run daily until every paper converges
    on a Gemma score. Returns the number of papers actually re-scored.
    """
    targets = [p for p in papers if not _is_gemma_model(p.get("eval_model", ""))]
    print(f"  {len(targets)} non-Gemma-scored papers to re-evaluate on Gemma.")
    rescored = 0
    for i, p in enumerate(targets):
        print(f"  [{i+1}/{len(targets)}] re-eval: {p.get('title','')[:70]}")
        p["abstract"] = _fetch_abstract_for_paper(p)
        result = evaluate_paper(p, candidates=GEMMA_ONLY_CANDIDATES, allow_groq=False)
        p.pop("abstract", None)
        if not result or not _is_gemma_model(result.get("eval_model", "")):
            print("      still no Gemma result — leaving as-is")
            continue
        if "prev_score" not in p or not p.get("prev_eval_model"):
            p["prev_score"] = p.get("score", 0)
            p["prev_eval_model"] = p.get("eval_model", "")
        p.update(result)
        rescored += 1
        print(f"      re-scored {p['score']} via {result['eval_model']} "
              f"(was {p['prev_score']} via {p['prev_eval_model'] or '(none)'})")
    print(f"  Re-evaluated {rescored}/{len(targets)} papers onto Gemma.")
    return rescored


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("Loading existing papers from Google Sheet...")
    try:
        existing = load_from_sheet()
        print(f"  {len(existing)} existing papers loaded.")
    except Exception as e:
        print(f"  Could not read sheet (first run?): {e}")
        existing = []

    # Migrate papers scored on the old 1–10 scale to the new 1–50 scale
    # Only migrate papers without a score_breakdown (breakdown means already on 1–50 scale)
    for p in existing:
        s = p.get("score", 0)
        if 0 < s <= 10 and not p.get("score_breakdown"):
            p["score"] = s * 5
            print(f"  Migrated score {s}→{p['score']} for: {p.get('title','')[:60]}")

    # Recalculate composite score from breakdown where they don't match
    # (fixes papers incorrectly migrated in a previous run)
    for p in existing:
        bd = p.get("score_breakdown")
        if bd:
            cat_sum = sum(v.get("score", 0) for v in bd.values())
            if cat_sum > 0 and cat_sum != p.get("score", 0):
                print(f"  Recalc score {p['score']}→{cat_sum} for: {p.get('title','')[:60]}")
                p["score"] = cat_sum

    # Deduplicate existing papers by title (catches cross-source dupes already in the sheet)
    before = len(existing)
    existing = dedup_by_title(existing)
    if len(existing) < before:
        print(f"  Removed {before - len(existing)} duplicate(s) from existing papers.")

    # Verify existing PubMed papers: drop any where HUJI is not last/majority author
    existing = _verify_huji_pubmed(existing)

    if ARGS.backfill_metadata:
        backfill_metadata(existing)
        save_to_sheet(existing)
        generate_html(existing)
        print("Backfill complete.")
        return True

    if ARGS.reeval_to_gemma:
        rescored = reeval_to_gemma_papers(existing)
        if rescored:
            save_to_sheet(existing)
            generate_html(existing)
        else:
            print("  Nothing re-scored (no non-Gemma papers, or Gemma still unavailable).")
        print("Re-evaluation complete.")
        return True

    known_ids = existing_ids(existing)
    known_titles = {norm_title(p.get("title", "")) for p in existing}
    new_papers = []

    fetch_errors = 0
    for fetcher in [fetch_pubmed, fetch_europepmc, fetch_semantic_scholar]:
        try:
            batch = fetcher()
            fresh = [p for p in batch
                     if p["id"] not in known_ids
                     and norm_title(p.get("title", "")) not in known_titles]
            print(f"{fetcher.__name__}: {len(batch)} fetched, {len(fresh)} new")
            new_papers.extend(fresh)
            known_ids.update(p["id"] for p in fresh)
            known_titles.update(norm_title(p.get("title", "")) for p in fresh)
        except Exception as e:
            print(f"{fetcher.__name__} error: {e}")
            fetch_errors += 1
        time.sleep(0.5)

    if fetch_errors == 3:
        print("All fetchers failed — aborting to avoid overwriting sheet.")
        return

    # Deduplicate new papers by title (cross-source within this fetch)
    deduped = dedup_by_title(new_papers)
    print(f"\n{len(deduped)} unique new papers to evaluate.")

    # Enrich PI contact info for new papers
    if deduped:
        print(f"Enriching PI contact info for {len(deduped)} new papers...")
        for paper in deduped:
            enrich_pi_contact(paper)
            time.sleep(0.3)

    # Enrich existing papers that are missing PI contact data (cap at 25/run)
    to_enrich = [p for p in existing if not p.get("pi_email")][:25]
    if to_enrich:
        print(f"Enriching PI contact for {len(to_enrich)} existing papers (backfill)...")
        for p in to_enrich:
            enrich_pi_contact(p)
            time.sleep(0.3)

    # No age-based retention: all previously evaluated papers are kept
    # indefinitely. `retained` is just a stable snapshot so a checkpoint
    # mid-loop can save real progress even if the job is killed (e.g. a CI
    # timeout) before the loop finishes.
    retained = existing

    CHECKPOINT_EVERY = 15  # papers between incremental saves

    def checkpoint(evaluated_so_far, label):
        all_papers = retained + evaluated_so_far
        all_papers.sort(key=lambda p: p.get("date", ""), reverse=True)
        print(f"  [checkpoint: {label}] writing {len(all_papers)} papers "
              f"({len(retained)} retained + {len(evaluated_so_far)} new)...")
        save_to_sheet(all_papers)
        generate_html(all_papers)

    evaluated = []
    for i, paper in enumerate(deduped):
        print(f"  [{i+1}/{len(deduped)}] {paper['title'][:70]}")
        result = evaluate_paper(paper)
        if result:
            paper.update(result)
            paper["added_date"] = today_str()
            evaluated.append(paper)
            print(f"    score={result['score']} fields={result['fields']}")
        else:
            print(f"    evaluation failed — skipped")
        if evaluated and len(evaluated) % CHECKPOINT_EVERY == 0:
            try:
                checkpoint(evaluated, f"{i+1}/{len(deduped)}")
            except Exception as e:
                print(f"  [checkpoint failed, continuing]: {e}")
        time.sleep(0.5)

    # If there were papers to evaluate but ALL failed (e.g. quota exhausted),
    # abort rather than wiping the sheet. Exit non-zero so CI surfaces this
    # as a failed run instead of a silent green checkmark.
    if deduped and not evaluated:
        print("All evaluations failed — aborting to avoid overwriting sheet with empty data.")
        return False

    print(f"\nKept all {len(retained)} existing papers, added {len(evaluated)} new.")
    checkpoint(evaluated, "final")
    print("Done.")
    return True


if __name__ == "__main__":
    import sys
    sys.exit(0 if main() else 1)
