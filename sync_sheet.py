#!/usr/bin/env python3
"""
Push the current papers_data.json to Google Sheet via Apps Script.
Run this whenever you want to sync local data to the sheet without
running the full pipeline:

    APPS_SCRIPT_URL=<url> GOOGLE_SHEET_ID=<id> python sync_sheet.py
"""
import json, os, requests
from pathlib import Path

APPS_SCRIPT_URL = os.environ["APPS_SCRIPT_URL"]

SHEET_COLUMNS = [
    "id", "title", "authors", "journal", "date", "url", "source",
    "score", "summary", "opportunity", "fields", "added_date", "score_breakdown", "pi",
]

papers = json.loads(Path("papers_data.json").read_text(encoding="utf-8"))

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
        p.get("added_date", ""),
        json.dumps(p.get("score_breakdown", {}), ensure_ascii=False),
        p.get("pi", ""),
    ])

r = requests.post(
    APPS_SCRIPT_URL,
    json={"action": "replace_all", "rows": rows},
    timeout=120,
)
r.raise_for_status()
print(f"Sheet updated with {len(papers)} papers: {r.text[:200]}")
