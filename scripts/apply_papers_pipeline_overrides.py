#!/usr/bin/env python3
"""Apply operational overrides to papers_pipeline.py before a GitHub Actions run.

This keeps the main pipeline usable while allowing the workflow to:
- use the requested Gemma model,
- run a multi-month backfill,
- keep all historical papers in the HTML,
- use a more realistic high-fit threshold.

The script is intentionally idempotent so repeated runs are safe.
"""

from pathlib import Path

PIPELINE = Path("papers_pipeline.py")
text = PIPELINE.read_text(encoding="utf-8")


def replace_once(old: str, new: str, label: str) -> None:
    """Replace old with new, but tolerate an already-patched file."""
    global text
    if old in text:
        text = text.replace(old, new, 1)
        print(f"patched: {label}")
    elif new in text:
        print(f"already patched: {label}")
    else:
        raise RuntimeError(f"Could not patch {label}; expected text not found")


replace_once(
    'MAX_RESULTS      = 50    # per source',
    'MAX_RESULTS      = int(os.environ.get("MAX_RESULTS", "1000"))  # per source',
    'MAX_RESULTS env override',
)

replace_once(
    'KEEP_DAYS        = 90    # keep all papers for this many days',
    'KEEP_DAYS        = int(os.environ.get("KEEP_DAYS", "3650"))  # keep all papers for this many days',
    'KEEP_DAYS env override',
)

replace_once(
    'DAYS_BACK = 7 if ARGS.period == "week" else 30',
    'DAYS_BACK = int(os.environ.get("DAYS_BACK", "7" if ARGS.period == "week" else "30"))',
    'DAYS_BACK env override',
)

replace_once(
    'model="gemma-3-27b-it",',
    'model=os.environ.get("GEMINI_MODEL", "gemma-4-31b-it"),',
    'Gemma 4 31B model',
)

replace_once(
    '"limit": max_results,',
    '"limit": min(max_results, 100),',
    'Semantic Scholar limit cap',
)

replace_once(
    'Return a JSON object (no markdown) with exactly these keys:\n- score: integer 1-10 (10 = excellent on this dimension)',
    'Use the full 1-10 scale. Scores of 8-10 are appropriate for clearly protectable, clinically or industrially relevant, platform-enabling, or near-product work; do not reserve 8-10 only for marketed products.\n\nReturn a JSON object (no markdown) with exactly these keys:\n- score: integer 1-10 (10 = excellent on this dimension)',
    'score calibration prompt',
)

replace_once(
    "function scoreClass(s){{return s>=38?'score-high':s>=28?'score-mid':'score-low';}}",
    "function scoreClass(s){{return s>=32?'score-high':s>=24?'score-mid':'score-low';}}",
    'high-fit visual threshold',
)

replace_once(
    'retained = apply_retention(existing)',
    'retained = existing',
    'disable HTML retention cutoff',
)

PIPELINE.write_text(text, encoding="utf-8")
print("papers_pipeline.py overrides applied")
