#!/usr/bin/env python3
"""
Bimonthly cleanup: remove low-scoring papers (score below LOW_SCORE_THRESHOLD).
All other papers are kept indefinitely, regardless of age.

Reuses papers_pipeline's sheet I/O so the schema (all SHEET_COLUMNS, including
pi_affiliation/eval_model) and the Apps Script payload shape stay in sync — an
earlier standalone copy here posted the wrong payload and silently failed to
update the sheet.
"""

import os

import papers_pipeline as pp

LOW_SCORE_THRESHOLD = int(os.environ.get("LOW_SCORE_THRESHOLD", "25"))   # remove if score is below this


def main():
    print("Loading papers from sheet...")
    papers = pp.load_from_sheet()
    print(f"  {len(papers)} papers loaded.")

    kept, removed = [], []
    for p in papers:
        if p.get("score", 0) < LOW_SCORE_THRESHOLD:
            removed.append(p)
        else:
            kept.append(p)

    print(f"  Removing {len(removed)} low-score papers (score<{LOW_SCORE_THRESHOLD}).")
    print(f"  Keeping {len(kept)} papers.")

    if not removed:
        print("Nothing to clean up.")
        return

    pp.save_to_sheet(kept)
    pp.generate_html(kept)
    print("Done.")


if __name__ == "__main__":
    main()
