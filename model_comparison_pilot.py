#!/usr/bin/env python3
"""
One-off pilot: force each model in the fallback chain (gemma-4-31b-it,
gemma-4-26b-a4b-it, gemini-3.1-flash-lite, groq llama-3.1-8b-instant) to
independently score the SAME sample of papers, to quantify the real
difference in applicability scoring across models — not just which one
happens to answer first in production.

Usage:
    GEMINI_API_KEY=... GOOGLE_SHEET_ID=... [GROQ_API_KEY=...] \
      python model_comparison_pilot.py [--sample-size 5]

Cost note: each paper costs 6 calls (1 meta + 5 dimensions) per model, so a
sample of N papers costs N x 4 x 6 calls. Keep --sample-size small; the
default (5) is 120 calls.

Output: model_comparison_pilot.json (full per-model results) plus a summary
table printed to stdout (per-paper score spread, per-model averages and
failure counts).
"""
import argparse
import json
import time
from pathlib import Path

import papers_pipeline as pp

MODELS_TO_COMPARE = [
    "gemma-4-31b-it",
    "gemma-4-26b-a4b-it",
    "gemini-3.1-flash-lite",
    f"groq:{pp.GROQ_MODEL}",
]

OUTPUT_JSON = Path("model_comparison_pilot.json")


def _parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--sample-size", type=int, default=5,
                    help="Number of papers to compare across all 4 models (default 5). "
                         "Cost is sample_size x 4 models x 6 calls.")
    return p.parse_args()


def main():
    args = _parse_args()
    print("Loading papers from Google Sheet...")
    papers = pp.load_from_sheet()
    print(f"  {len(papers)} papers loaded.")

    sample = sorted(papers, key=lambda p: p.get("score", 0), reverse=True)[:args.sample_size]
    print(f"\nComparing {len(MODELS_TO_COMPARE)} models on {len(sample)} papers "
          f"({len(sample) * len(MODELS_TO_COMPARE) * 6} total calls)...")

    results = []
    for i, paper in enumerate(sample):
        print(f"\n[{i+1}/{len(sample)}] {paper.get('title', '')[:70]}")
        abstract = pp._fetch_abstract_for_paper(paper)
        row = {
            "id": paper.get("id", ""),
            "title": paper.get("title", ""),
            "production_score": paper.get("score", 0),
            "production_eval_model": paper.get("eval_model", ""),
            "models": {},
        }
        for model_id in MODELS_TO_COMPARE:
            print(f"    forcing {model_id}...")
            probe = {"title": paper.get("title", ""), "abstract": abstract}
            try:
                result = pp.evaluate_paper(probe, force_model=model_id)
            except Exception as e:
                result = None
                print(f"      error: {e}")
            if result:
                row["models"][model_id] = {
                    "score": result["score"],
                    "score_breakdown": result["score_breakdown"],
                }
                print(f"      score={result['score']}")
            else:
                row["models"][model_id] = None
                print("      failed (no score)")
            time.sleep(0.5)
        results.append(row)

    OUTPUT_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {OUTPUT_JSON}")

    # Summary table
    print("\n=== Per-paper score spread across models ===")
    for row in results:
        scores = {m: (r["score"] if r else None) for m, r in row["models"].items()}
        present = [s for s in scores.values() if s is not None]
        spread = (max(present) - min(present)) if len(present) > 1 else 0
        print(f"- {row['title'][:60]}")
        for m, s in scores.items():
            print(f"    {m:28s} {s if s is not None else 'FAILED'}")
        print(f"    spread: {spread}")

    print("\n=== Per-model summary ===")
    for model_id in MODELS_TO_COMPARE:
        scores = [row["models"][model_id]["score"] for row in results if row["models"][model_id]]
        failures = sum(1 for row in results if not row["models"][model_id])
        avg = round(sum(scores) / len(scores), 1) if scores else None
        print(f"- {model_id:28s} avg={avg} succeeded={len(scores)}/{len(results)} failed={failures}")


if __name__ == "__main__":
    main()
