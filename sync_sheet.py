#!/usr/bin/env python3
"""
Push the current papers_data.json to Google Sheet via Apps Script.
Run this whenever you want to sync local data to the sheet without
running the full pipeline:

    APPS_SCRIPT_URL=<url> GOOGLE_SHEET_ID=<id> GEMINI_API_KEY=<key> python sync_sheet.py

Reuses papers_pipeline.save_to_sheet so the sheet schema stays in sync with the
pipeline (a previous standalone copy here had a stale column list and would
have dropped pi_full_name/pi_email/pi_affiliation/eval_model on write).
"""
import json
from pathlib import Path

import papers_pipeline as pp


def main():
    papers = json.loads(Path("papers_data.json").read_text(encoding="utf-8"))
    pp.save_to_sheet(papers)
    print(f"Sheet updated with {len(papers)} papers.")


if __name__ == "__main__":
    main()
