#!/usr/bin/env python3
"""
Bimonthly cleanup: remove low-scoring papers (score below LOW_SCORE_THRESHOLD).
All other papers are kept indefinitely, regardless of age.
"""

import csv
import io
import json
import os
import requests
from pathlib import Path

GOOGLE_SHEET_ID       = os.environ["GOOGLE_SHEET_ID"]
APPS_SCRIPT_URL       = os.environ["APPS_SCRIPT_URL"]
OUTPUT_HTML           = Path("papers_reader.html")
OUTPUT_JSON           = Path("papers_data.json")

LOW_SCORE_THRESHOLD   = int(os.environ.get("LOW_SCORE_THRESHOLD", "25"))   # only remove if score is below this


# ── Load sheet ────────────────────────────────────────────────────────────────

def load_from_sheet():
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
        try:
            p["score"] = int(p.get("score", 0))
        except Exception:
            p["score"] = 0
        try:
            p["fields"] = json.loads(p.get("fields", "[]"))
        except Exception:
            p["fields"] = []
        try:
            p["score_breakdown"] = json.loads(p.get("score_breakdown", "{}"))
        except Exception:
            p["score_breakdown"] = {}
        try:
            p["authors"] = json.loads(p.get("authors", "[]"))
        except Exception:
            p["authors"] = []
        papers.append(p)
    return papers


def save_to_sheet(papers):
    payload = {"papers": papers}
    r = requests.post(APPS_SCRIPT_URL, json=payload, timeout=30)
    r.raise_for_status()
    print(f"Sheet updated: {r.text[:200]}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Loading papers from sheet...")
    papers = load_from_sheet()
    print(f"  {len(papers)} papers loaded.")

    kept, removed = [], []
    for p in papers:
        score = p.get("score", 0)
        if score < LOW_SCORE_THRESHOLD:
            removed.append(p)
        else:
            kept.append(p)

    print(f"  Removing {len(removed)} low-score papers (score<{LOW_SCORE_THRESHOLD}).")
    print(f"  Keeping {len(kept)} papers.")

    if not removed:
        print("Nothing to clean up.")
        return

    save_to_sheet(kept)

    # Regenerate HTML + JSON with remaining papers
    from papers_pipeline import generate_html
    generate_html(kept)
    print("Done.")


if __name__ == "__main__":
    main()
