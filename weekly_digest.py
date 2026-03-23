#!/usr/bin/env python3
"""
Weekly Digest Generator
- Reads top papers from the public Google Sheet
- Uses Gemini to curate the best 8-12 and write commercial headlines
- Generates weekly_digest.pdf committed to the repo
"""

import csv
import io
import json
import os
import re
import datetime
import requests
from pathlib import Path
from google import genai

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether,
)

# ── Config ──────────────────────────────────────────────────────────────────
GEMINI_API_KEY  = os.environ["GEMINI_API_KEY"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
DIGESTS_DIR     = Path("digests")
TOP_N           = 20   # top papers by score sent to Gemini for curation
DIGEST_WINDOW   = 7    # only include papers added in the last N days

client = genai.Client(api_key=GEMINI_API_KEY)
DIGEST_MODEL = "gemini-2.5-flash-preview-04-17"  # strongest available on free tier

# ── Load sheet ───────────────────────────────────────────────────────────────

def load_top_papers():
    url = (
        f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}"
        f"/export?format=csv&gid=0"
    )
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    reader = csv.DictReader(io.StringIO(r.text))
    cutoff = datetime.date.today() - datetime.timedelta(days=DIGEST_WINDOW)
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
        # Only include papers added within the digest window
        try:
            added = datetime.date.fromisoformat(p.get("added_date", ""))
            if added < cutoff:
                continue
        except Exception:
            pass  # if no added_date, include it anyway
        papers.append(p)
    papers.sort(key=lambda x: x["score"], reverse=True)
    return papers[:TOP_N]

# ── Gemini curation ──────────────────────────────────────────────────────────

CURATION_PROMPT = """You are a technology-transfer analyst at Hebrew University of Jerusalem.
Review these {n} research papers and select the 8-12 most commercially promising ones
for a weekly digest sent to investors and industry partners.

Papers:
{paper_list}

Return a JSON object (no markdown) with exactly these keys:
- executive_summary: 3-4 sentence overview of this week's standout research themes
- selected: list of objects, each with:
    - index: 1-based integer matching the paper number above
    - headline: one punchy sentence on the commercial angle (max 20 words)
    - why_now: 1-2 sentences on timing, market gap, or near-term opportunity

Pick for commercial impact, not just high score. Aim for diversity of fields.
"""

def curate_with_gemini(papers):
    paper_list = "\n\n".join(
        f"[{i+1}] Score: {p['score']}/50 | PI: {p.get('pi','—')} | "
        f"Fields: {', '.join(p.get('fields', []))}\n"
        f"Title: {p['title']}\n"
        f"Summary: {p.get('summary','')}\n"
        f"Opportunity: {p.get('opportunity','')}"
        for i, p in enumerate(papers)
    )
    prompt = CURATION_PROMPT.format(n=len(papers), paper_list=paper_list)
    resp = client.models.generate_content(model=DIGEST_MODEL, contents=prompt)
    text = re.sub(r"^```(?:json)?\s*", "", resp.text.strip())
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)

# ── PDF generation ───────────────────────────────────────────────────────────

# Colour palette
C_PURPLE  = colors.HexColor("#6c63ff")
C_PURPLE2 = colors.HexColor("#a78bfa")
C_DARK    = colors.HexColor("#0f1117")
C_CARD    = colors.HexColor("#1a1d2e")
C_BORDER  = colors.HexColor("#2d3148")
C_TEXT    = colors.HexColor("#e2e8f0")
C_MUTED   = colors.HexColor("#8892a4")
C_GREEN   = colors.HexColor("#22c55e")
C_YELLOW  = colors.HexColor("#eab308")
C_RED     = colors.HexColor("#ef4444")
C_WHITE   = colors.white

def score_color(s):
    if s >= 38: return C_GREEN
    if s >= 28: return C_YELLOW
    return C_RED

def build_styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "title", parent=base["Normal"],
            fontSize=22, leading=28, textColor=C_PURPLE2,
            fontName="Helvetica-Bold", spaceAfter=4,
        ),
        "subtitle": ParagraphStyle(
            "subtitle", parent=base["Normal"],
            fontSize=10, leading=14, textColor=C_MUTED,
            fontName="Helvetica", spaceAfter=0,
        ),
        "exec_label": ParagraphStyle(
            "exec_label", parent=base["Normal"],
            fontSize=8, leading=10, textColor=C_PURPLE,
            fontName="Helvetica-Bold", spaceAfter=4,
            spaceBefore=0,
        ),
        "exec_body": ParagraphStyle(
            "exec_body", parent=base["Normal"],
            fontSize=10, leading=15, textColor=C_TEXT,
            fontName="Helvetica", alignment=TA_JUSTIFY,
        ),
        "paper_num": ParagraphStyle(
            "paper_num", parent=base["Normal"],
            fontSize=8, leading=10, textColor=C_MUTED,
            fontName="Helvetica-Bold",
        ),
        "paper_title": ParagraphStyle(
            "paper_title", parent=base["Normal"],
            fontSize=12, leading=16, textColor=C_TEXT,
            fontName="Helvetica-Bold", spaceAfter=3,
        ),
        "pi_line": ParagraphStyle(
            "pi_line", parent=base["Normal"],
            fontSize=8, leading=11, textColor=C_PURPLE2,
            fontName="Helvetica-Bold",
        ),
        "headline": ParagraphStyle(
            "headline", parent=base["Normal"],
            fontSize=10, leading=14, textColor=C_PURPLE2,
            fontName="Helvetica-Oblique", spaceAfter=4,
        ),
        "label": ParagraphStyle(
            "label", parent=base["Normal"],
            fontSize=7, leading=9, textColor=C_MUTED,
            fontName="Helvetica-Bold", spaceAfter=1,
        ),
        "body": ParagraphStyle(
            "body", parent=base["Normal"],
            fontSize=9, leading=13, textColor=C_TEXT,
            fontName="Helvetica", alignment=TA_JUSTIFY, spaceAfter=4,
        ),
        "tag": ParagraphStyle(
            "tag", parent=base["Normal"],
            fontSize=7, leading=9, textColor=C_MUTED,
            fontName="Helvetica",
        ),
        "footer": ParagraphStyle(
            "footer", parent=base["Normal"],
            fontSize=8, leading=10, textColor=C_MUTED,
            fontName="Helvetica", alignment=TA_CENTER,
        ),
    }


def score_badge_table(score):
    """Returns a 1-cell Table that looks like a score badge."""
    c = score_color(score)
    data = [[Paragraph(
        f"<font color='#{c.hexval()[2:]}'>{score}/50</font>",
        ParagraphStyle("b", fontName="Helvetica-Bold", fontSize=10, leading=12,
                       textColor=c),
    )]]
    t = Table(data, colWidths=[18*mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#1a1d2e")),
        ("BOX",        (0, 0), (-1, -1), 0.5, C_BORDER),
        ("ROUNDEDCORNERS", [4]),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t


def paper_block(idx, paper, curation_item, styles):
    """Build a KeepTogether block for one paper."""
    elements = []

    # Header row: [number + title cell] [score badge]
    num_title = [
        Paragraph(f"#{idx}", styles["paper_num"]),
        Paragraph(paper["title"], styles["paper_title"]),
    ]
    header_inner = Table(
        [[num_title[0]], [num_title[1]]],
        colWidths=[None],
    )
    header_inner.setStyle(TableStyle([
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
        ("TOPPADDING",    (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))

    page_w = A4[0] - 32*mm   # usable width inside card
    header_row = Table(
        [[header_inner, score_badge_table(paper["score"])]],
        colWidths=[page_w - 24*mm, 22*mm],
    )
    header_row.setStyle(TableStyle([
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
        ("TOPPADDING",    (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))

    inner = [header_row]

    # PI
    pi = paper.get("pi", "").strip()
    if pi:
        inner.append(Spacer(1, 2*mm))
        inner.append(Paragraph(f"Main Researcher: {pi}", styles["pi_line"]))

    # Fields
    fields = paper.get("fields", [])
    if fields:
        inner.append(Spacer(1, 1*mm))
        inner.append(Paragraph("  ".join(f"[{f}]" for f in fields), styles["tag"]))

    # Gemini headline
    inner.append(Spacer(1, 3*mm))
    inner.append(Paragraph(f'"{curation_item["headline"]}"', styles["headline"]))

    # Why now
    inner.append(Paragraph("WHY NOW", styles["label"]))
    inner.append(Paragraph(curation_item["why_now"], styles["body"]))

    # Opportunity
    opp = paper.get("opportunity", "").strip()
    if opp:
        inner.append(Paragraph("COMMERCIAL ANGLE", styles["label"]))
        inner.append(Paragraph(opp, styles["body"]))

    # URL
    url = paper.get("url", "")
    if url:
        inner.append(Paragraph(
            f'<link href="{url}"><font color="#6c63ff">{url}</font></link>',
            ParagraphStyle("url", fontName="Helvetica", fontSize=8, leading=10,
                           textColor=C_PURPLE),
        ))

    # Wrap in card table
    card_content = [[inner_el] for inner_el in inner]
    # Use a single-cell table as the card background
    flat = []
    for row in card_content:
        flat.extend(row)

    card_table = Table([[flat]], colWidths=[page_w])
    card_table.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), C_CARD),
        ("BOX",           (0, 0), (-1, -1), 0.5, C_BORDER),
        ("LEFTPADDING",   (0, 0), (-1, -1), 5*mm),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 5*mm),
        ("TOPPADDING",    (0, 0), (-1, -1), 5*mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5*mm),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
    ]))

    elements.append(KeepTogether(card_table))
    elements.append(Spacer(1, 4*mm))
    return elements


def generate_pdf(papers_by_idx, curation):
    iso = datetime.date.today().isocalendar()
    today = datetime.date.today().strftime("%B %d, %Y")
    week = iso[1]
    year = iso[0]
    DIGESTS_DIR.mkdir(exist_ok=True)
    output_pdf = DIGESTS_DIR / f"HUJI_digest_{year}_W{week:02d}.pdf"
    styles = build_styles()

    doc = SimpleDocTemplate(
        str(output_pdf),
        pagesize=A4,
        leftMargin=16*mm, rightMargin=16*mm,
        topMargin=16*mm, bottomMargin=16*mm,
        title=f"HUJI Research Digest — Week {week}",
        author="HUJI Research Monitor",
    )

    story = []

    # ── Header ──
    story.append(Paragraph("HUJI Research Monitor", styles["title"]))
    story.append(Paragraph(
        f"Weekly Digest  ·  Week {week}  ·  {today}",
        styles["subtitle"],
    ))
    story.append(Spacer(1, 6*mm))
    story.append(HRFlowable(width="100%", thickness=1, color=C_BORDER))
    story.append(Spacer(1, 4*mm))

    # ── Executive summary ──
    exec_box_inner = [
        Paragraph("EXECUTIVE SUMMARY", styles["exec_label"]),
        Paragraph(curation["executive_summary"], styles["exec_body"]),
    ]
    page_w = A4[0] - 32*mm
    exec_table = Table([[[e] for e in exec_box_inner]], colWidths=[page_w])
    exec_table = Table([[exec_box_inner]], colWidths=[page_w])
    exec_table.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#1e1b4b")),
        ("BOX",           (0, 0), (-1, -1), 0.5, C_PURPLE),
        ("LEFTPADDING",   (0, 0), (-1, -1), 5*mm),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 5*mm),
        ("TOPPADDING",    (0, 0), (-1, -1), 4*mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4*mm),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(exec_table)
    story.append(Spacer(1, 6*mm))

    # ── Paper cards ──
    for rank, item in enumerate(curation["selected"], start=1):
        idx = item["index"] - 1   # 0-based
        if idx < 0 or idx >= len(papers_by_idx):
            continue
        paper = papers_by_idx[idx]
        story.extend(paper_block(rank, paper, item, styles))

    # ── Footer ──
    story.append(Spacer(1, 4*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=C_BORDER))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph(
        f"Generated {today} · HUJI Technology Transfer Office · Powered by Gemini AI",
        styles["footer"],
    ))

    doc.build(story)
    print(f"PDF written: {output_pdf}  ({output_pdf.stat().st_size // 1024} KB)")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("Loading top papers from Google Sheet...")
    papers = load_top_papers()
    print(f"  {len(papers)} candidates (top {TOP_N} by score).")

    if not papers:
        print("No papers found — aborting.")
        return

    print("Asking Gemini to curate the digest...")
    curation = curate_with_gemini(papers)
    selected = curation.get("selected", [])
    print(f"  Gemini selected {len(selected)} papers.")
    print(f"  Executive summary: {curation.get('executive_summary','')[:120]}...")

    print("Generating PDF...")
    generate_pdf(papers, curation)
    print("Done.")


if __name__ == "__main__":
    main()
