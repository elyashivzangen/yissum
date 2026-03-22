#!/usr/bin/env python3
"""
Paper Evaluation Pipeline
Fetches papers from PubMed, Europe PMC, and Semantic Scholar,
evaluates relevance with Gemini, and generates a standalone HTML reader.
"""

import json
import os
import re
import time
import datetime
import requests
import google.generativeai as genai
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────────────
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
DATA_FILE      = Path("papers_data.json")
OUTPUT_HTML    = Path("papers_reader.html")
MAX_RESULTS    = 30   # per source
DAYS_BACK      = 7    # look-back window
MIN_SCORE      = 6    # minimum relevance score (1-10) to include

SEARCH_QUERIES = [
    "bioinformatics machine learning drug discovery",
    "protein structure prediction AI",
    "genomics single cell sequencing analysis",
    "medical imaging deep learning diagnosis",
    "CRISPR gene editing therapeutic",
    "clinical trial biomarker oncology",
    "federated learning healthcare privacy",
    "digital pathology computational biology",
    "vaccine immunology computational",
    "synthetic biology metabolic engineering",
]

FIELD_TAGS = [
    "Drug Discovery", "Medical Device", "Diagnostics", "Vaccines",
    "AgriTech", "FoodTech", "Materials", "Clean Energy",
    "Software/AI", "Quantum", "Neuroscience", "Genomics",
    "Imaging", "Synthetic Biology", "Proteomics", "Immunology",
    "Clinical", "Other",
]

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

# ── Helpers ────────────────────────────────────────────────────────────────────

def today_str():
    return datetime.date.today().isoformat()

def days_ago(n):
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()

def load_existing():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text())
    return []

def save_data(papers):
    DATA_FILE.write_text(json.dumps(papers, indent=2, ensure_ascii=False))

def existing_ids(papers):
    return {p["id"] for p in papers}

# ── Fetchers ───────────────────────────────────────────────────────────────────

def fetch_pubmed(query, max_results=MAX_RESULTS):
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    since = days_ago(DAYS_BACK)
    # search
    r = requests.get(f"{base}/esearch.fcgi", params={
        "db": "pubmed", "term": query,
        "datetype": "pdat", "mindate": since, "maxdate": today_str(),
        "retmax": max_results, "retmode": "json",
    }, timeout=20)
    r.raise_for_status()
    ids = r.json().get("esearchresult", {}).get("idlist", [])
    if not ids:
        return []
    # fetch details
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
            "id": f"pubmed_{uid}",
            "title": item.get("title", ""),
            "abstract": "",  # summary endpoint doesn't include abstract
            "authors": authors,
            "journal": item.get("fulljournalname", ""),
            "date": item.get("pubdate", ""),
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
            "source": "PubMed",
        })
    return papers


def fetch_europepmc(query, max_results=MAX_RESULTS):
    since = days_ago(DAYS_BACK)
    r = requests.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search", params={
        "query": f"{query} FIRST_PDATE:[{since} TO {today_str()}]",
        "resultType": "core", "pageSize": max_results, "format": "json",
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
            "id": f"epmc_{item.get('id','')}",
            "title": item.get("title", ""),
            "abstract": item.get("abstractText", ""),
            "authors": authors,
            "journal": item.get("journalTitle", ""),
            "date": item.get("firstPublicationDate", ""),
            "url": f"https://europepmc.org/article/{item.get('source','')}/{item.get('id','')}",
            "source": "Europe PMC",
        })
    return papers


def fetch_semantic_scholar(query, max_results=MAX_RESULTS):
    r = requests.get("https://api.semanticscholar.org/graph/v1/paper/search", params={
        "query": query,
        "fields": "title,abstract,authors,year,venue,externalIds,publicationDate",
        "limit": max_results,
    }, timeout=20)
    r.raise_for_status()
    items = r.json().get("data", [])
    since = days_ago(DAYS_BACK)
    papers = []
    for item in items:
        pub_date = item.get("publicationDate") or f"{item.get('year','')}-01-01"
        if pub_date < since:
            continue
        pid = item.get("paperId", "")
        ext = item.get("externalIds") or {}
        url = (f"https://www.semanticscholar.org/paper/{pid}"
               if not ext.get("DOI") else f"https://doi.org/{ext['DOI']}")
        authors = [a.get("name", "") for a in (item.get("authors") or [])[:3]]
        papers.append({
            "id": f"ss_{pid}",
            "title": item.get("title", ""),
            "abstract": item.get("abstract", "") or "",
            "authors": authors,
            "journal": item.get("venue", ""),
            "date": pub_date,
            "url": url,
            "source": "Semantic Scholar",
        })
    return papers

# ── Gemini Evaluation ──────────────────────────────────────────────────────────

EVAL_PROMPT = """You are a life-science and deep-tech investment analyst.
Evaluate this academic paper for relevance to emerging commercial opportunities
in biotech, medtech, agritech, materials, clean energy, or AI/software tools
for science.

Title: {title}
Abstract: {abstract}

Return a JSON object (no markdown) with these exact keys:
- score: integer 1-10 (10 = highly relevant commercial opportunity)
- summary: 2-sentence plain-English summary of the paper
- opportunity: 1-sentence description of the commercial angle
- fields: list of 1-4 tags from this list only: {fields}
"""

def evaluate_paper(paper):
    abstract = paper.get("abstract", "").strip()
    if not abstract:
        abstract = "(no abstract available)"
    prompt = EVAL_PROMPT.format(
        title=paper["title"],
        abstract=abstract[:1200],
        fields=json.dumps(FIELD_TAGS),
    )
    try:
        resp = model.generate_content(prompt)
        text = resp.text.strip()
        # strip possible ```json ... ```
        text = re.sub(r"^```(?:json)?\s*", "", text)
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
<title>Research Paper Monitor</title>
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
  <h1>Research Paper Monitor</h1>
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
    # enrich papers dict with eval fields at top level for JS
    enriched = []
    for p in papers:
        flat = {
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
        }
        enriched.append(flat)
    # sort by score descending for initial render
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
    existing = load_existing()
    known_ids = existing_ids(existing)
    print(f"Loaded {len(existing)} existing papers.")

    new_papers = []
    for query in SEARCH_QUERIES:
        print(f"\nQuery: {query}")
        for fetcher in [fetch_pubmed, fetch_europepmc, fetch_semantic_scholar]:
            try:
                batch = fetcher(query)
                fresh = [p for p in batch if p["id"] not in known_ids]
                print(f"  {fetcher.__name__}: {len(batch)} fetched, {len(fresh)} new")
                new_papers.extend(fresh)
                known_ids.update(p["id"] for p in fresh)
                time.sleep(0.3)
            except Exception as e:
                print(f"  {fetcher.__name__} error: {e}")

    # de-dup within new batch by title similarity (simple exact dedup)
    seen_titles = set()
    deduped = []
    for p in new_papers:
        key = re.sub(r"\W+", " ", p["title"].lower()).strip()
        if key not in seen_titles:
            seen_titles.add(key)
            deduped.append(p)
    print(f"\n{len(deduped)} unique new papers to evaluate.")

    evaluated = []
    for i, paper in enumerate(deduped):
        print(f"  [{i+1}/{len(deduped)}] Evaluating: {paper['title'][:70]}")
        result = evaluate_paper(paper)
        if result and result["score"] >= MIN_SCORE:
            paper.update(result)
            evaluated.append(paper)
            print(f"    -> score {result['score']} | fields: {result['fields']}")
        else:
            score = result["score"] if result else "n/a"
            print(f"    -> score {score} (skipped)")
        time.sleep(0.5)  # be kind to Gemini rate limits

    all_papers = existing + evaluated
    # keep only last 200 papers sorted by date to avoid unbounded growth
    all_papers.sort(key=lambda p: p.get("date", ""), reverse=True)
    all_papers = all_papers[:200]

    save_data(all_papers)
    print(f"\nSaved {len(all_papers)} papers to {DATA_FILE}.")

    generate_html(all_papers)


if __name__ == "__main__":
    main()
