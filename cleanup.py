#!/usr/bin/env python3
"""
Bimonthly cleanup: remove low-scoring papers. The cutoff is per branch:
min(25, 75th-percentile score of that branch), so every branch keeps at least
its top 25%.
All other papers are kept indefinitely, regardless of age.
Removed papers are first saved to papers_archive.json (never shown in the monitor).

Reuses papers_pipeline's sheet I/O so the schema (all SHEET_COLUMNS, including
pi_affiliation/eval_model) and the Apps Script payload shape stay in sync — an
earlier standalone copy here posted the wrong payload and silently failed to
update the sheet.
"""

import papers_pipeline as pp


def main():
    print("Loading papers from sheet...")
    papers = pp.load_from_sheet()
    print(f"  {len(papers)} papers loaded.")

    # Archive EVERYTHING first, so nothing removed below is ever lost.
    pp.update_archive(papers)

    # Per-branch cutoff, measured over the whole archive: min(25, 75th percentile).
    pool = {p["id"]: p for p in pp.load_archive()}
    pool.update({p["id"]: p for p in papers if p.get("id")})
    cutoffs = pp.compute_cutoffs(list(pool.values()))
    print(f"  Cutoffs per branch: {cutoffs}")

    kept, removed = [], []
    for p in papers:
        if p.get("score", 0) < pp.paper_cutoff(p, cutoffs):
            removed.append(p)
        else:
            kept.append(p)

    print(f"  Removing {len(removed)} low-score papers (archived, not deleted).")
    print(f"  Keeping {len(kept)} papers.")

    if not removed:
        print("Nothing to clean up.")
        return

    pp.save_to_sheet(kept)
    pp.generate_html(kept)
    print("Done.")


if __name__ == "__main__":
    main()
