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
from google import genai
from google.genai import types
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────────────
GEMINI_API_KEY   = os.environ["GEMINI_API_KEY"]
GOOGLE_SHEET_ID  = os.environ["GOOGLE_SHEET_ID"]
APPS_SCRIPT_URL  = os.environ["APPS_SCRIPT_URL"]   # deployed Apps Script web app URL
OUTPUT_HTML      = Path("papers_reader.html")
OUTPUT_JSON      = Path("papers_data.json")
MAX_RESULTS      = 50    # per source
KEEP_DAYS        = 90    # keep all papers for this many days

def _parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--period", choices=["week", "month"], default="week",
                   help="Fetch window: week=7 days, month=30 days")
    return p.parse_args()

ARGS     = _parse_args()
DAYS_BACK = 7 if ARGS.period == "week" else 30

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

SHEET_COLUMNS = [
    "id", "title", "authors", "journal", "date", "url", "source",
    "score", "summary", "opportunity", "fields", "added_date", "score_breakdown", "pi",
    "pi_full_name", "pi_email",
]

SCORE_PARAMS = [
    ("novelty",              "Scientific novelty and innovation — how groundbreaking is this research compared to prior art?"),
    ("commercial_potential", "Commercial opportunity strength — how clearly does this translate to a product, service, or licensable technology?"),
    ("market_size",          "Market size and addressable demand — how large and valuable is the target market?"),
    ("trl",                  "Technology readiness — how close is this to real-world application or commercialization (lab-stage vs. near-market)?"),
    ("ip_strength",          "IP and defensibility — how patentable or otherwise defensible is the underlying innovation?"),
]

client = genai.Client(api_key=GEMINI_API_KEY)

# ── Google Sheets (no service account) ────────────────────────────────────────

def load_from_sheet():
    """Read all papers from the public Google Sheet via CSV export."""
    url = (
        f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}"
        f"/export?format=csv&gid=0"
    )
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    reader = csv.DictReader(io.StringIO(r.text))
    papers = []
    for row in reader:
        p = dict(row)
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
        ])
    r = requests.post(
        APPS_SCRIPT_URL,
        json={"action": "replace_all", "rows": rows},
        timeout=120,
    )
    r.raise_for_status()
    print(f"Sheet updated: {r.text[:200]}")

# ── Retention ──────────────────────────────────────────────────────────────────

def apply_retention(papers):
    today = datetime.date.today()
    kept = []
    for p in papers:
        try:
            age = (today - datetime.date.fromisoformat(p.get("added_date", ""))).days
        except Exception:
            age = 0
        if age <= KEEP_DAYS:
            kept.append(p)
    return kept

# ── Helpers ────────────────────────────────────────────────────────────────────

def today_str():
    return datetime.date.today().isoformat()

def days_ago(n):
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()

def existing_ids(papers):
    return {p["id"] for p in papers}

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
    except Exception:
        pass
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
        results = r.json().get("result", [])
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
            for entry in er.json().get("email", []):
                addr = entry.get("email")
                if addr:
                    return addr, orcid_id
    except Exception:
        pass
    return None, None


def enrich_pi_contact(paper):
    """Resolve full name and email for the PI. Updates paper dict in-place.

    Strategy (in order):
    1. PubMed full-XML efetch  — extracts email from affiliation strings
    2. CrossRef                — corresponding-author email via DOI
    3. ORCID public API        — searches by name + institution, reads public email
    """
    pid = paper.get("id", "")

    if pid.startswith("pubmed_"):
        pmid = pid.replace("pubmed_", "")
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

    r2 = requests.get(f"{base}/esummary.fcgi", params={
        "db": "pubmed", "id": ",".join(ids), "retmode": "json",
    }, timeout=20)
    r2.raise_for_status()
    result = r2.json().get("result", {})

    papers = []
    for uid in ids:
        item = result.get(uid, {})
        all_authors = [a.get("name", "") for a in item.get("authors", [])]
        # PubMed query already filters by HUJI affiliation; last author is the PI
        pi = all_authors[-1] if all_authors else ""
        papers.append({
            "id":       f"pubmed_{uid}",
            "title":    item.get("title", ""),
            "abstract": "",
            "authors":  all_authors[:3],
            "journal":  item.get("fulljournalname", ""),
            "date":     item.get("pubdate", ""),
            "url":      f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
            "source":   "PubMed",
            "pi":       pi,
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
        # Find last HUJI-affiliated author as PI
        pi = ""
        for a in reversed(all_author_objs):
            affs = [x.get("affiliation", "") for x in
                    (a.get("authorAffiliationDetailsList") or {}).get("authorAffiliation", [])]
            if any(h.lower() in af.lower() for h in HUJI_AFFILIATIONS for af in affs):
                pi = f"{a.get('firstName','')} {a.get('lastName','')}".strip()
                break
        if not pi and all_authors:
            pi = all_authors[-1]
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
        huji = any(
            any(h.lower() in (aff or "").lower() for h in HUJI_AFFILIATIONS)
            for a in (item.get("authors") or [])
            for aff in (a.get("affiliations") or [])
        )
        if not huji:
            continue
        pid = item.get("paperId", "")
        ext = item.get("externalIds") or {}
        url = (f"https://doi.org/{ext['DOI']}" if ext.get("DOI")
               else f"https://www.semanticscholar.org/paper/{pid}")
        all_author_objs = item.get("authors") or []
        all_authors = [a.get("name", "") for a in all_author_objs]
        # Find last HUJI-affiliated author as PI
        pi = ""
        for a in reversed(all_author_objs):
            if any(h.lower() in (aff or "").lower()
                   for h in HUJI_AFFILIATIONS
                   for aff in (a.get("affiliations") or [])):
                pi = a.get("name", "")
                break
        if not pi and all_authors:
            pi = all_authors[-1]
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

Return a JSON object (no markdown) with exactly these keys:
- score: integer 1-10 (10 = excellent on this dimension)
- reason: 1-2 sentence explanation for the score on this specific dimension
"""

def _call_gemini(prompt):
    resp = client.models.generate_content(
        model="gemma-3-27b-it",
        contents=prompt,
    )
    text = re.sub(r"^```(?:json)?\s*", "", resp.text.strip())
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)

def evaluate_paper(paper):
    abstract = paper.get("abstract", "").strip() or "(no abstract available)"
    truncated = abstract[:1200]

    # 1. Meta: summary, opportunity, fields
    try:
        meta = _call_gemini(META_PROMPT.format(
            title=paper["title"], abstract=truncated, fields=json.dumps(FIELD_TAGS),
        ))
    except Exception as e:
        print(f"  Gemini error (meta): {e}")
        return None
    time.sleep(0.4)

    # 2. Per-parameter scoring (separate call each)
    breakdown = {}
    scores = []
    for param_name, param_desc in SCORE_PARAMS:
        try:
            data = _call_gemini(PARAM_PROMPT.format(
                param_name=param_name, param_desc=param_desc,
                title=paper["title"], abstract=truncated,
            ))
            s = max(1, min(10, int(data.get("score", 5))))
            breakdown[param_name] = {"score": s, "reason": data.get("reason", "")}
            scores.append(s)
        except Exception as e:
            print(f"  Gemini error ({param_name}): {e}")
        time.sleep(0.4)

    if not scores:
        return None

    composite = sum(scores)  # total out of 50
    return {
        "score":           composite,
        "summary":         meta.get("summary", ""),
        "opportunity":     meta.get("opportunity", ""),
        "fields":          meta.get("fields", []),
        "score_breakdown": breakdown,
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
    --bg:#0f1117; --card:#1a1d2e; --accent:#6c63ff;
    --accent2:#a78bfa; --text:#e2e8f0; --muted:#8892a4;
    --border:#2d3148; --tag-bg:#23263a; --green:#22c55e;
    --yellow:#eab308; --red:#ef4444;
  }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh}}
  header{{background:var(--card);border-bottom:1px solid var(--border);padding:16px 24px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}}
  header h1{{font-size:1.25rem;font-weight:700;color:var(--accent2)}}
  header span{{font-size:.8rem;color:var(--muted)}}
  .controls{{padding:16px 24px;display:flex;flex-direction:column;gap:12px}}
  .row{{display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
  .row label{{font-size:.75rem;color:var(--muted);min-width:52px;text-transform:uppercase;letter-spacing:.05em}}
  .chip{{padding:4px 12px;border-radius:999px;border:1px solid var(--border);background:var(--tag-bg);
    color:var(--muted);font-size:.75rem;cursor:pointer;transition:all .15s}}
  .chip:hover,.chip.active{{background:var(--accent);border-color:var(--accent);color:#fff}}
  .search-row{{display:flex;gap:8px}}
  input[type=text]{{flex:1;background:var(--card);border:1px solid var(--border);border-radius:8px;
    padding:8px 14px;color:var(--text);font-size:.875rem;outline:none}}
  input[type=text]:focus{{border-color:var(--accent)}}
  .sort-select{{background:var(--card);border:1px solid var(--border);border-radius:8px;
    padding:8px 14px;color:var(--text);font-size:.875rem;cursor:pointer;outline:none}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px;padding:0 24px 32px}}
  .card{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:18px;
    display:flex;flex-direction:column;gap:10px;transition:border-color .2s}}
  .card:hover{{border-color:var(--accent)}}
  .card-header{{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}}
  .score{{min-width:52px;height:36px;border-radius:8px;display:flex;align-items:center;justify-content:center;
    font-weight:700;font-size:.82rem;padding:0 6px;white-space:nowrap}}
  .score-high{{background:#14532d;color:var(--green)}}
  .score-mid{{background:#713f12;color:var(--yellow)}}
  .score-low{{background:#450a0a;color:var(--red)}}
  .title{{font-size:.9rem;font-weight:600;line-height:1.4;color:var(--text)}}
  .meta{{font-size:.75rem;color:var(--muted)}}
  .summary{{font-size:.8rem;color:#b0bac8;line-height:1.5}}
  .opportunity{{font-size:.8rem;background:#1e1b4b;border-left:3px solid var(--accent);
    padding:6px 10px;border-radius:0 6px 6px 0;color:var(--accent2);line-height:1.4}}
  .tags{{display:flex;flex-wrap:wrap;gap:6px}}
  .tag{{padding:2px 10px;border-radius:999px;background:var(--tag-bg);border:1px solid var(--border);
    font-size:.7rem;color:var(--muted)}}
  .actions{{display:flex;gap:8px;margin-top:4px}}
  .btn{{padding:5px 14px;border-radius:6px;border:1px solid var(--border);background:transparent;
    color:var(--muted);font-size:.75rem;cursor:pointer;transition:all .15s;text-decoration:none;display:inline-block}}
  .btn:hover,.btn.active{{background:var(--accent);border-color:var(--accent);color:#fff}}
  .count{{font-size:.8rem;color:var(--muted);padding:0 24px 8px}}
  .empty{{text-align:center;padding:60px 24px;color:var(--muted)}}
  .breakdown{{display:none;flex-direction:column;gap:8px;border-top:1px solid var(--border);padding-top:10px;margin-top:2px}}
  .breakdown.open{{display:flex}}
  .bd-row{{display:flex;flex-direction:column;gap:3px}}
  .bd-label{{display:flex;justify-content:space-between;align-items:center}}
  .bd-name{{font-size:.72rem;font-weight:600;color:var(--text);text-transform:capitalize}}
  .bd-score{{font-size:.72rem;font-weight:700}}
  .bd-bar-bg{{height:5px;background:var(--border);border-radius:3px;overflow:hidden}}
  .bd-bar{{height:100%;border-radius:3px;transition:width .3s}}
  .bd-reason{{font-size:.7rem;color:var(--muted);line-height:1.4}}
  .pi{{display:flex;align-items:center;flex-wrap:wrap;gap:6px;margin:4px 0 2px}}
  .pi-label{{font-size:.65rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:700;flex-shrink:0}}
  .pi-name{{font-size:.85rem;font-weight:700;color:var(--accent2)}}
  .pi-email-btn{{padding:2px 8px;font-size:.68rem;margin-left:2px}}
  .pi-email{{font-size:.75rem;padding:0 0 4px 0}}
  .pi-email a{{color:var(--accent2);text-decoration:none}}
  .pi-email a:hover{{text-decoration:underline}}
  .slider{{-webkit-appearance:none;appearance:none;height:4px;border-radius:2px;background:var(--border);outline:none;cursor:pointer;width:140px;vertical-align:middle}}
  .slider::-webkit-slider-thumb{{-webkit-appearance:none;appearance:none;width:16px;height:16px;border-radius:50%;background:var(--accent);cursor:pointer}}
  .slider::-moz-range-thumb{{width:16px;height:16px;border-radius:50%;background:var(--accent);cursor:pointer;border:none}}
  .slider:disabled{{opacity:.3;cursor:not-allowed}}
  .slider-val{{font-size:.75rem;color:var(--accent2);font-weight:700;min-width:42px;display:inline-block}}
  @media(max-width:600px){{.grid{{grid-template-columns:1fr;padding:0 12px 24px}}.controls{{padding:12px}}.slider{{width:100px}}}}
</style>
</head>
<body>
<header>
  <h1>HUJI Research Monitor</h1>
  <span id="updated"></span>
</header>
<div class="controls">
  <div class="row search-row">
    <input type="text" id="search" placeholder="Search titles, summaries, opportunities..."/>
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
<script>
const papers = {papers_json};
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
let activeScore=0, activeField='all', activePeriod='all', sortBy='score', searchQ='', activeParam='', activeParamMin=1;
const PARAM_LABELS = {{
  novelty:'Novelty', commercial_potential:'Commercial Potential',
  market_size:'Market Size', trl:'Tech Readiness', ip_strength:'IP Strength'
}};
function scoreClass(s){{return s>=38?'score-high':s>=28?'score-mid':'score-low';}}
function barColor(s){{return s>=8?'#22c55e':s>=5?'#eab308':'#ef4444';}}
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
function applyFilters(list,{{skipPeriod,skipField}}){{
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
      <div class="card-header"><div class="title">${{p.title}}</div><div class="score ${{scoreClass(p.score)}}">${{p.score}}/50</div></div>
      ${{(p.pi||p.pi_full_name)?`<div class="pi"><span class="pi-label">Main Researcher</span><span class="pi-name">👤 ${{p.pi_full_name||p.pi}}</span>${{p.pi_email?`<button class="btn pi-email-btn" onclick="toggleEmail(this)">Email ▾</button>`:''}} </div>${{p.pi_email?`<div class="pi-email" style="display:none"><a href="mailto:${{p.pi_email}}">${{p.pi_email}}</a></div>`:''}}`:''}}
      <div class="meta">${{authors?authors+' · ':''}}${{p.journal||''}}${{p.date?' · '+p.date:''}}</div>
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
  const card=btn.closest('.card');
  const bd=card.querySelector('.breakdown');
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

def generate_html(papers):
    enriched = sorted([{
        "id":              p["id"],
        "title":           p.get("title", ""),
        "authors":         p.get("authors", []),
        "journal":         p.get("journal", ""),
        "date":            p.get("date", ""),
        "url":             p.get("url", ""),
        "score":           p.get("score", 0),
        "summary":         p.get("summary", ""),
        "opportunity":     p.get("opportunity", ""),
        "fields":          p.get("fields", []),
        "score_breakdown": p.get("score_breakdown", {}),
        "pi":              p.get("pi", ""),
        "pi_full_name":    p.get("pi_full_name", ""),
        "pi_email":        p.get("pi_email", ""),
        "added_date":      p.get("added_date", ""),
    } for p in papers], key=lambda x: x["score"], reverse=True)

    OUTPUT_HTML.write_text(HTML_TEMPLATE.format(
        field_chips=build_field_chips(),
        papers_json=json.dumps(enriched, ensure_ascii=False),
        updated=today_str(),
    ), encoding="utf-8")
    OUTPUT_JSON.write_text(json.dumps(enriched, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Generated {OUTPUT_HTML} and {OUTPUT_JSON} with {len(enriched)} papers.")

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

    known_ids = existing_ids(existing)
    new_papers = []

    fetch_errors = 0
    for fetcher in [fetch_pubmed, fetch_europepmc, fetch_semantic_scholar]:
        try:
            batch = fetcher()
            fresh = [p for p in batch if p["id"] not in known_ids]
            print(f"{fetcher.__name__}: {len(batch)} fetched, {len(fresh)} new")
            new_papers.extend(fresh)
            known_ids.update(p["id"] for p in fresh)
        except Exception as e:
            print(f"{fetcher.__name__} error: {e}")
            fetch_errors += 1
        time.sleep(0.5)

    if fetch_errors == 3:
        print("All fetchers failed — aborting to avoid overwriting sheet.")
        return

    # Deduplicate by title
    seen, deduped = set(), []
    for p in new_papers:
        key = re.sub(r"\W+", " ", p.get("title", "").lower()).strip()
        if key and key not in seen:
            seen.add(key)
            deduped.append(p)
    print(f"\n{len(deduped)} unique new papers to evaluate.")

    # Enrich PI contact info for new papers
    if deduped:
        print(f"Enriching PI contact info for {len(deduped)} new papers...")
        for paper in deduped:
            enrich_pi_contact(paper)
            time.sleep(0.3)

    # Enrich existing papers that are missing PI contact data
    to_enrich = [p for p in existing if not p.get("pi_email")]
    if to_enrich:
        print(f"Enriching PI contact for {len(to_enrich)} existing papers (backfill)...")
        for p in to_enrich:
            enrich_pi_contact(p)
            time.sleep(0.3)

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
        time.sleep(0.5)

    # If there were papers to evaluate but ALL failed (e.g. quota exhausted),
    # abort rather than wiping the sheet.
    if deduped and not evaluated:
        print("All evaluations failed — aborting to avoid overwriting sheet with empty data.")
        return

    retained = apply_retention(existing)
    print(f"\nRetention: kept {len(retained)}/{len(existing)} existing, added {len(evaluated)} new.")

    all_papers = retained + evaluated
    all_papers.sort(key=lambda p: p.get("date", ""), reverse=True)

    print(f"Writing {len(all_papers)} papers to Google Sheet...")
    save_to_sheet(all_papers)

    generate_html(all_papers)
    print("Done.")


if __name__ == "__main__":
    main()
