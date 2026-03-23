#!/usr/bin/env python3
"""
Hebrew University Paper Evaluation Pipeline
- Reads existing papers from public Google Sheet (CSV export, no auth)
- Fetches new HUJI-affiliated papers from PubMed + Europe PMC + Semantic Scholar
- Evaluates with Gemini (score, summary, commercial opportunity, field tags)
- Writes updated papers back via Apps Script web app (no service account needed)
- Generates standalone papers_reader.html committed to the repo
"""

import csv
import io
import json
import os
import re
import time
import datetime
import requests
from google import genai
from google.genai import types
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────────────
GEMINI_API_KEY   = os.environ.get("GEMINI_API_KEY",   "AIzaSyA_tvstQJ_bVWR0lnTTrfEWDfVy158Llek")
GOOGLE_SHEET_ID  = os.environ.get("GOOGLE_SHEET_ID",  "1oyewjr_pojhyBJHXw_yLUrWVvVoFPbd352dAXPqpGyM")
APPS_SCRIPT_URL  = os.environ.get("APPS_SCRIPT_URL",  "https://script.google.com/macros/s/AKfycbzVIm6dwN_2KuQuYxI1Y7LjfEMYFzicdSEn1aMPOXr6n8MHB0LI9jVNkB0QFxRJmJYw/exec")
OUTPUT_HTML      = Path("papers_reader.html")
MAX_RESULTS      = 50    # per source
DAYS_BACK        = 7     # fetch window (days)
MIN_SCORE        = 6     # discard below this
KEEP_FOREVER     = 8     # keep indefinitely if score >= this
KEEP_DAYS_MID    = 60    # keep score 6-7 for this many days

HUJI_AFFILIATIONS = [
    "Hebrew University of Jerusalem",
    "Hebrew University",
    "Hadassah",
    "Einstein Institute of Mathematics",
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
        score = p.get("score", 0)
        if score >= KEEP_FOREVER:
            kept.append(p)
            continue
        try:
            age = (today - datetime.date.fromisoformat(p.get("added_date", ""))).days
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
        authors = [a.get("name", "") for a in item.get("authors", [])[:3]]
        papers.append({
            "id":       f"pubmed_{uid}",
            "title":    item.get("title", ""),
            "abstract": "",
            "authors":  authors,
            "journal":  item.get("fulljournalname", ""),
            "date":     item.get("pubdate", ""),
            "url":      f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
            "source":   "PubMed",
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
Evaluate this paper from Hebrew University of Jerusalem researchers
for relevance to emerging commercial opportunities in biotech, medtech,
agritech, materials, clean energy, or AI/software tools for science.

Title: {title}
Abstract: {abstract}

Return a JSON object (no markdown) with these exact keys:
- score: integer 1-10 (10 = highly relevant commercial opportunity)
- summary: 2-sentence plain-English summary
- opportunity: 1-sentence commercial angle
- fields: list of 1-4 tags from: {fields}
"""

def evaluate_paper(paper):
    abstract = paper.get("abstract", "").strip() or "(no abstract available)"
    prompt = EVAL_PROMPT.format(
        title=paper["title"],
        abstract=abstract[:1200],
        fields=json.dumps(FIELD_TAGS),
    )
    try:
        resp = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
        )
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
        print(f"  Gemini error: {e}")
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
  <div class="row">
    <label>Field</label>
    <button class="chip active" data-filter="field" data-val="all">All</button>
    {field_chips}
  </div>
</div>
<div class="count" id="count"></div>
<div class="grid" id="grid"></div>
<script>
const papers = {papers_json};
document.getElementById('updated').textContent = 'Updated {updated}';
let activeScore='all', activeField='all', sortBy='score', searchQ='';
function scoreClass(s){{return s>=8?'score-high':s>=6?'score-mid':'score-low';}}
function render(){{
  let list=papers.slice();
  if(searchQ){{const q=searchQ.toLowerCase();list=list.filter(p=>(p.title||'').toLowerCase().includes(q)||(p.summary||'').toLowerCase().includes(q)||(p.opportunity||'').toLowerCase().includes(q));}}
  if(activeScore!=='all')list=list.filter(p=>p.score>=parseInt(activeScore));
  if(activeField!=='all')list=list.filter(p=>(p.fields||[]).includes(activeField));
  list.sort(sortBy==='score'?(a,b)=>b.score-a.score:(a,b)=>(b.date||'').localeCompare(a.date||''));
  document.getElementById('count').textContent=list.length+' paper'+(list.length!==1?'s':'')+' shown';
  const grid=document.getElementById('grid');
  if(!list.length){{grid.innerHTML='<div class="empty">No papers match.</div>';return;}}
  grid.innerHTML=list.map(p=>{{
    const tags=(p.fields||[]).map(f=>`<span class="tag">${{f}}</span>`).join('');
    const authors=(p.authors||[]).join(', ');
    return `<div class="card">
      <div class="card-header"><div class="title">${{p.title}}</div><div class="score ${{scoreClass(p.score)}}">${{p.score}}</div></div>
      <div class="meta">${{authors?authors+' · ':''}}${{p.journal||''}}${{p.date?' · '+p.date:''}}</div>
      ${{p.summary?`<div class="summary">${{p.summary}}</div>`:''}}
      ${{p.opportunity?`<div class="opportunity">${{p.opportunity}}</div>`:''}}
      ${{tags?`<div class="tags">${{tags}}</div>`:''}}
      <div class="actions"><a class="btn" href="${{p.url}}" target="_blank">Open Paper</a></div>
    </div>`;
  }}).join('');
}}
document.querySelectorAll('.chip').forEach(b=>b.addEventListener('click',()=>{{
  const f=b.dataset.filter,v=b.dataset.val;
  document.querySelectorAll(`.chip[data-filter="${{f}}"]`).forEach(x=>x.classList.remove('active'));
  b.classList.add('active');
  if(f==='score')activeScore=v; else activeField=v;
  render();
}}));
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
    enriched = sorted([{
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
    } for p in papers], key=lambda x: x["score"], reverse=True)

    OUTPUT_HTML.write_text(HTML_TEMPLATE.format(
        field_chips=build_field_chips(),
        papers_json=json.dumps(enriched, ensure_ascii=False),
        updated=today_str(),
    ), encoding="utf-8")
    print(f"Generated {OUTPUT_HTML} with {len(enriched)} papers.")

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("Loading existing papers from Google Sheet...")
    try:
        existing = load_from_sheet()
        print(f"  {len(existing)} existing papers loaded.")
    except Exception as e:
        print(f"  Could not read sheet (first run?): {e}")
        existing = []

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

    evaluated = []
    for i, paper in enumerate(deduped):
        print(f"  [{i+1}/{len(deduped)}] {paper['title'][:70]}")
        result = evaluate_paper(paper)
        if result and result["score"] >= MIN_SCORE:
            paper.update(result)
            paper["added_date"] = today_str()
            evaluated.append(paper)
            print(f"    score={result['score']} fields={result['fields']}")
        else:
            print(f"    score={result['score'] if result else 'n/a'} — skipped")
        time.sleep(0.5)

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
