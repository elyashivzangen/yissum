#!/usr/bin/env python3
"""
Hebrew University Paper Evaluation Pipeline
Fetches papers affiliated with Hebrew University of Jerusalem,
evaluates relevance with Gemini, stores results in Google Sheets,
and generates a standalone HTML reader.
"""

import json
import os
import re
import time
import datetime
import requests
import gspread
import google.generativeai as genai
from pathlib import Path
from google.oauth2.service_account import Credentials

# ── Config ─────────────────────────────────────────────────────────────────────
GEMINI_API_KEY       = os.environ["GEMINI_API_KEY"]
GOOGLE_SHEET_ID      = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_CREDS_JSON    = os.environ["GOOGLE_CREDS_JSON"]   # full JSON string of service account key
OUTPUT_HTML          = Path("papers_reader.html")
MAX_RESULTS          = 50    # per source
DAYS_BACK            = 7     # fetch window
MIN_SCORE            = 6     # discard below this
KEEP_FOREVER_SCORE   = 8     # never delete if score >= this
KEEP_DAYS_MID        = 60    # keep score 6-7 for this many days

HUJI_AFFILIATIONS = [
    "Hebrew University of Jerusalem",
    "Hebrew University",
    "Hadassah",
    "Einstein Institute",
    "Silberman Institute",
]

FIELD_TAGS = [
    "Drug Discovery", "Medical Device", "Diagnostics", "Vaccines",
    "AgriTech", "FoodTech", "Materials", "Clean Energy",
    "Software/AI", "Quantum", "Neuroscience", "Genomics",
    "Imaging", "Synthetic Biology", "Proteomics", "Immunology",
    "Clinical", "Other",
]

SHEET_COLUMNS = [
    "id", "title", "authors", "journal", "date", "url", "source",
    "score", "summary", "opportunity", "fields", "added_date",
]

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

# ── Google Sheets ──────────────────────────────────────────────────────────────

def get_sheet():
    creds_info = json.loads(GOOGLE_CREDS_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
    return sheet


def load_from_sheet(sheet):
    rows = sheet.get_all_records()
    papers = []
    for row in rows:
        p = dict(row)
        # parse list fields stored as JSON strings
        for f in ("authors", "fields"):
            if isinstance(p.get(f), str):
                try:
                    p[f] = json.loads(p[f])
                except Exception:
                    p[f] = []
        if isinstance(p.get("score"), str):
            try:
                p["score"] = int(p["score"])
            except Exception:
                p["score"] = 0
        papers.append(p)
    return papers


def ensure_header(sheet):
    first_row = sheet.row_values(1)
    if first_row != SHEET_COLUMNS:
        sheet.clear()
        sheet.append_row(SHEET_COLUMNS)


def save_to_sheet(sheet, papers):
    """Rewrite the sheet with the current papers list (after retention filtering)."""
    ensure_header(sheet)
    # clear data rows (keep header)
    sheet.resize(rows=1)
    if not papers:
        return
    rows = []
    for p in papers:
        rows.append([
            p.get("id", ""),
            p.get("title", ""),
            json.dumps(p.get("authors", [])),
            p.get("journal", ""),
            p.get("date", ""),
            p.get("url", ""),
            p.get("source", ""),
            p.get("score", 0),
            p.get("summary", ""),
            p.get("opportunity", ""),
            json.dumps(p.get("fields", [])),
            p.get("added_date", ""),
        ])
    sheet.append_rows(rows, value_input_option="RAW")


def append_papers_to_sheet(sheet, new_papers):
    """Append only new rows without rewriting existing ones."""
    ensure_header(sheet)
    rows = []
    for p in new_papers:
        rows.append([
            p.get("id", ""),
            p.get("title", ""),
            json.dumps(p.get("authors", [])),
            p.get("journal", ""),
            p.get("date", ""),
            p.get("url", ""),
            p.get("source", ""),
            p.get("score", 0),
            p.get("summary", ""),
            p.get("opportunity", ""),
            json.dumps(p.get("fields", [])),
            p.get("added_date", today_str()),
        ])
    if rows:
        sheet.append_rows(rows, value_input_option="RAW")

# ── Retention ──────────────────────────────────────────────────────────────────

def apply_retention(papers):
    """
    Keep forever:  score >= KEEP_FOREVER_SCORE
    Keep 60 days:  score 6-7
    Drop:          score < MIN_SCORE (shouldn't exist, but guard)
    """
    today = datetime.date.today()
    kept = []
    for p in papers:
        score = p.get("score", 0)
        if score >= KEEP_FOREVER_SCORE:
            kept.append(p)
            continue
        added = p.get("added_date", "")
        try:
            age = (today - datetime.date.fromisoformat(added)).days
        except Exception:
            age = 0
        if score >= MIN_SCORE and age <= KEEP_DAYS_MID:
            kept.append(p)
    return kept

# ── Helpers ────────────────────────────────────────────────────────────────────

def today_str():
    return datetime.date.today().isoformat()

def days_ago(n):
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()

def existing_ids(papers):
    return {p["id"] for p in papers}

# ── Fetchers ───────────────────────────────────────────────────────────────────

def fetch_pubmed(max_results=MAX_RESULTS):
    """Fetch recent papers with Hebrew University affiliation from PubMed."""
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
    r2 = requests.get(f"{base}/efetch.fcgi", params={
        "db": "pubmed", "id": ",".join(ids), "retmode": "xml", "rettype": "abstract",
    }, timeout=30)
    r2.raise_for_status()
    # parse with simple regex (avoid lxml dependency)
    papers = []
    for uid in ids:
        papers.append({
            "id": f"pubmed_{uid}",
            "title": "",
            "abstract": "",
            "authors": [],
            "journal": "",
            "date": "",
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
            "source": "PubMed",
        })
    # fetch summaries for metadata
    r3 = requests.get(f"{base}/esummary.fcgi", params={
        "db": "pubmed", "id": ",".join(ids), "retmode": "json",
    }, timeout=20)
    r3.raise_for_status()
    result = r3.json().get("result", {})
    enriched = []
    for p in papers:
        uid = p["id"].replace("pubmed_", "")
        item = result.get(uid, {})
        authors = [a.get("name", "") for a in item.get("authors", [])[:3]]
        enriched.append({
            **p,
            "title":   item.get("title", ""),
            "authors": authors,
            "journal": item.get("fulljournalname", ""),
            "date":    item.get("pubdate", ""),
        })
    return enriched


def fetch_europepmc(max_results=MAX_RESULTS):
    """Fetch HUJI-affiliated papers from Europe PMC."""
    since = days_ago(DAYS_BACK)
    query = (
        f'(AFF:"Hebrew University of Jerusalem" OR AFF:"Hadassah") '
        f'AND FIRST_PDATE:[{since} TO {today_str()}]'
    )
    r = requests.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search", params={
        "query": query,
        "resultType": "core",
        "pageSize": max_results,
        "format": "json",
    }, timeout=20)
    r.raise_for_status()
    items = r.json().get("resultList", {}).get("result", [])
    papers = []
    for item in items:
        authors = []
        for a in (item.get("authorList") or {}).get("author", [])[:3]:
            name = f"{a.get('firstName','')} {a.get('lastName','')}".strip()
            if name:
                authors.append(name)
        papers.append({
            "id":       f"epmc_{item.get('id','')}",
            "title":    item.get("title", ""),
            "abstract": item.get("abstractText", ""),
            "authors":  authors,
            "journal":  item.get("journalTitle", ""),
            "date":     item.get("firstPublicationDate", ""),
            "url":      f"https://europepmc.org/article/{item.get('source','')}/{item.get('id','')}",
            "source":   "Europe PMC",
        })
    return papers


def fetch_semantic_scholar(max_results=MAX_RESULTS):
    """
    Semantic Scholar has no affiliation filter via public API.
    Search for HUJI directly in the query and filter by affiliation string in author data.
    """
    r = requests.get("https://api.semanticscholar.org/graph/v1/paper/search", params={
        "query": "Hebrew University of Jerusalem",
        "fields": "title,abstract,authors,year,venue,externalIds,publicationDate,authors.affiliations",
        "limit": max_results,
    }, timeout=20)
    r.raise_for_status()
    since = days_ago(DAYS_BACK)
    items = r.json().get("data", [])
    papers = []
    for item in items:
        pub_date = item.get("publicationDate") or f"{item.get('year','')}-01-01"
        if pub_date < since:
            continue
        # verify at least one author has HUJI affiliation
        huji_affiliated = False
        for a in (item.get("authors") or []):
            for aff in (a.get("affiliations") or []):
                if any(h.lower() in aff.lower() for h in HUJI_AFFILIATIONS):
                    huji_affiliated = True
                    break
        if not huji_affiliated:
            continue
        pid = item.get("paperId", "")
        ext = item.get("externalIds") or {}
        url = (f"https://doi.org/{ext['DOI']}" if ext.get("DOI")
               else f"https://www.semanticscholar.org/paper/{pid}")
        authors = [a.get("name", "") for a in (item.get("authors") or [])[:3]]
        papers.append({
            "id":       f"ss_{pid}",
            "title":    item.get("title", ""),
            "abstract": item.get("abstract", "") or "",
            "authors":  authors,
            "journal":  item.get("venue", ""),
            "date":     pub_date,
            "url":      url,
            "source":   "Semantic Scholar",
        })
    return papers

# ── Gemini Evaluation ──────────────────────────────────────────────────────────

EVAL_PROMPT = """You are a life-science and deep-tech investment analyst.
Evaluate this academic paper (from Hebrew University of Jerusalem researchers)
for relevance to emerging commercial opportunities in biotech, medtech, agritech,
materials, clean energy, or AI/software tools for science.

Title: {title}
Abstract: {abstract}

Return a JSON object (no markdown) with these exact keys:
- score: integer 1-10 (10 = highly relevant commercial opportunity)
- summary: 2-sentence plain-English summary of the paper
- opportunity: 1-sentence description of the commercial angle
- fields: list of 1-4 tags from this list only: {fields}
"""

def evaluate_paper(paper):
    abstract = paper.get("abstract", "").strip() or "(no abstract available)"
    prompt = EVAL_PROMPT.format(
        title=paper["title"],
        abstract=abstract[:1200],
        fields=json.dumps(FIELD_TAGS),
    )
    try:
        resp = model.generate_content(prompt)
        text = re.sub(r"^```(?:json)?\s*", "", resp.text.strip())
        text = re.sub(r"\s*```$", "", text)
        data = json.loads(text)
        return {
            "score":       int(data.get("score", 0)),
            "summary":     data.get("summary", ""),
            "opportunity": data.get("opportunity", ""),
            "fields":      data.get("fields", []),
        }
    except Exception as e:
        print(f"  Gemini error for '{paper['title'][:60]}': {e}")
        return None

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
  .score{{min-width:36px;height:36px;border-radius:8px;display:flex;align-items:center;justify-content:center;
    font-weight:700;font-size:.95rem}}
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
  .btn:hover{{background:var(--accent);border-color:var(--accent);color:#fff}}
  .count{{font-size:.8rem;color:var(--muted);padding:0 24px 8px}}
  .empty{{text-align:center;padding:60px 24px;color:var(--muted)}}
  @media(max-width:600px){{.grid{{grid-template-columns:1fr;padding:0 12px 24px}}.controls{{padding:12px}}}}
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
    <label>Score</label>
    <button class="chip active" data-filter="score" data-val="all">All</button>
    <button class="chip" data-filter="score" data-val="8">8+</button>
    <button class="chip" data-filter="score" data-val="6">6+</button>
  </div>
  <div class="row" id="field-row">
    <label>Field</label>
    <button class="chip active" data-filter="field" data-val="all">All</button>
    {field_chips}
  </div>
</div>

<div class="count" id="count"></div>
<div class="grid" id="grid"></div>

<script>
const papers = {papers_json};
const updated = {updated_json};
document.getElementById('updated').textContent = 'Updated ' + updated;

let activeScore = 'all';
let activeField = 'all';
let sortBy = 'score';
let searchQ = '';

function scoreClass(s){{
  if(s>=8) return 'score-high';
  if(s>=6) return 'score-mid';
  return 'score-low';
}}

function render(){{
  let list = papers.slice();
  if(searchQ){{
    const q = searchQ.toLowerCase();
    list = list.filter(p=>
      (p.title||'').toLowerCase().includes(q)||
      (p.summary||'').toLowerCase().includes(q)||
      (p.opportunity||'').toLowerCase().includes(q)
    );
  }}
  if(activeScore!=='all') list = list.filter(p=>p.score>=parseInt(activeScore));
  if(activeField!=='all') list = list.filter(p=>(p.fields||[]).includes(activeField));
  if(sortBy==='score') list.sort((a,b)=>b.score-a.score);
  else list.sort((a,b)=>(b.date||'').localeCompare(a.date||''));

  document.getElementById('count').textContent = list.length + ' paper' + (list.length!==1?'s':'') + ' shown';

  const grid = document.getElementById('grid');
  if(!list.length){{
    grid.innerHTML='<div class="empty">No papers match the current filters.</div>';
    return;
  }}
  grid.innerHTML = list.map(p=>{{
    const sc = scoreClass(p.score);
    const authors = (p.authors||[]).join(', ');
    const tags = (p.fields||[]).map(f=>`<span class="tag">${{f}}</span>`).join('');
    return `<div class="card">
      <div class="card-header">
        <div class="title">${{p.title}}</div>
        <div class="score ${{sc}}">${{p.score}}</div>
      </div>
      <div class="meta">${{authors ? authors + ' · ' : ''}}${{p.journal||''}}${{p.date?' · '+p.date:''}}</div>
      ${{p.summary?`<div class="summary">${{p.summary}}</div>`:''}}
      ${{p.opportunity?`<div class="opportunity">${{p.opportunity}}</div>`:''}}
      ${{tags?`<div class="tags">${{tags}}</div>`:''}}
      <div class="actions">
        <a class="btn" href="${{p.url}}" target="_blank">Open Paper</a>
      </div>
    </div>`;
  }}).join('');
}}

document.querySelectorAll('.chip').forEach(btn=>{{
  btn.addEventListener('click',()=>{{
    const f=btn.dataset.filter, v=btn.dataset.val;
    document.querySelectorAll(`.chip[data-filter="${{f}}"]`).forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    if(f==='score') activeScore=v;
    if(f==='field') activeField=v;
    render();
  }});
}});
document.getElementById('sort').addEventListener('change',e=>{{sortBy=e.target.value;render();}});
document.getElementById('search').addEventListener('input',e=>{{searchQ=e.target.value.trim();render();}});

render();
</script>
</body>
</html>
"""

def build_field_chips():
    return "\n    ".join(
        f'<button class="chip" data-filter="field" data-val="{f}">{f}</button>'
        for f in FIELD_TAGS
    )

def generate_html(papers):
    enriched = [{
        "id":          p["id"],
        "title":       p.get("title", ""),
        "authors":     p.get("authors", []),
        "journal":     p.get("journal", ""),
        "date":        p.get("date", ""),
        "url":         p.get("url", ""),
        "score":       p.get("score", 0),
        "summary":     p.get("summary", ""),
        "opportunity": p.get("opportunity", ""),
        "fields":      p.get("fields", []),
    } for p in papers]
    enriched.sort(key=lambda x: x["score"], reverse=True)
    html = HTML_TEMPLATE.format(
        field_chips=build_field_chips(),
        papers_json=json.dumps(enriched, ensure_ascii=False),
        updated_json=json.dumps(today_str()),
    )
    OUTPUT_HTML.write_text(html, encoding="utf-8")
    print(f"Generated {OUTPUT_HTML} with {len(enriched)} papers.")

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("Connecting to Google Sheet...")
    sheet = get_sheet()
    existing = load_from_sheet(sheet)
    print(f"Loaded {len(existing)} existing papers from sheet.")

    known_ids = existing_ids(existing)

    # Fetch from all sources
    new_papers = []
    for fetcher in [fetch_pubmed, fetch_europepmc, fetch_semantic_scholar]:
        try:
            batch = fetcher()
            fresh = [p for p in batch if p["id"] not in known_ids]
            print(f"{fetcher.__name__}: {len(batch)} fetched, {len(fresh)} new")
            new_papers.extend(fresh)
            known_ids.update(p["id"] for p in fresh)
            time.sleep(0.5)
        except Exception as e:
            print(f"{fetcher.__name__} error: {e}")

    # Deduplicate by title
    seen_titles = set()
    deduped = []
    for p in new_papers:
        key = re.sub(r"\W+", " ", p.get("title", "").lower()).strip()
        if key and key not in seen_titles:
            seen_titles.add(key)
            deduped.append(p)
    print(f"\n{len(deduped)} unique new papers to evaluate.")

    # Evaluate with Gemini
    evaluated = []
    for i, paper in enumerate(deduped):
        print(f"  [{i+1}/{len(deduped)}] {paper['title'][:70]}")
        result = evaluate_paper(paper)
        if result and result["score"] >= MIN_SCORE:
            paper.update(result)
            paper["added_date"] = today_str()
            evaluated.append(paper)
            print(f"    -> score {result['score']} | {result['fields']}")
        else:
            score = result["score"] if result else "n/a"
            print(f"    -> score {score} (skipped)")
        time.sleep(0.5)

    # Apply retention to existing papers
    retained = apply_retention(existing)
    dropped = len(existing) - len(retained)
    if dropped:
        print(f"\nRetention: dropped {dropped} expired low-score papers.")

    all_papers = retained + evaluated
    all_papers.sort(key=lambda p: p.get("date", ""), reverse=True)

    # Update sheet: rewrite retained rows + append new
    print(f"\nUpdating sheet ({len(all_papers)} total papers)...")
    save_to_sheet(sheet, all_papers)

    generate_html(all_papers)
    print("Done.")


if __name__ == "__main__":
    main()
