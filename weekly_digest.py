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
import smtplib
import ssl
import datetime
from email.message import EmailMessage
import requests
from pathlib import Path
from google import genai

import yissum_report as yr

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

# ── Email delivery ──────────────────────────────────────────────────────────
# All optional: if SMTP_USER/SMTP_PASSWORD aren't set, digests are still
# generated and committed as before — email sending is just skipped.
RECIPIENTS_FILE = Path("digest_recipients.txt")
SMTP_HOST     = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
SMTP_PORT     = int(os.environ.get("SMTP_PORT") or "465")
SMTP_USER     = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
MAIL_FROM     = os.environ.get("MAIL_FROM", "") or SMTP_USER

# Three main Yissum TTO branches (mirrors papers_pipeline.py)
BRANCHES = {
    "Healthcare": [
        "Drug Discovery", "Medical Device", "Diagnostics", "Vaccines",
        "Neuroscience", "Genomics", "Imaging", "Synthetic Biology",
        "Proteomics", "Immunology", "Clinical",
    ],
    "Agriculture & Food": ["AgriTech", "FoodTech"],
    "Exact & Social Sciences": ["Materials", "Clean Energy", "Software/AI", "Quantum", "Other"],
}

client = genai.Client(api_key=GEMINI_API_KEY)
# Model ID candidates — tried in order until one works
DIGEST_MODEL_CANDIDATES = [
    "gemini-2.5-flash",          # confirmed working
    "gemini-2.0-flash",          # reliable fallback
]

# ── Load sheet ───────────────────────────────────────────────────────────────

_MONTHS = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,
           'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}

def _parse_date(d):
    """Parse date strings in multiple formats used by the sheet."""
    if not d:
        return None
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', d)
    if m:
        return datetime.date(int(m[1]), int(m[2]), int(m[3]))
    m = re.match(r'^(\d{4})\s+([A-Za-z]{3})(?:\s+(\d{1,2}))?', d)
    if m:
        return datetime.date(int(m[1]), _MONTHS.get(m[2], 1), int(m[3]) if m[3] else 1)
    m = re.match(r'^(\d{4})$', d)
    if m:
        return datetime.date(int(m[1]), 1, 1)
    return None


def _primary_branch(fields):
    """Return the branch name(s) with the most field-tag matches (exclusive for digest)."""
    best, best_n = None, 0
    for branch, branch_fields in BRANCHES.items():
        n = sum(1 for f in fields if f in branch_fields)
        if n > best_n:
            best_n, best = n, branch
    return best  # None if no matches


def load_top_papers(branch_name=None, top_n=TOP_N):
    url = (
        f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}"
        f"/export?format=csv&gid=0"
    )
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    print(f"  Sheet response: {r.status_code}, {len(r.text)} bytes")
    reader = csv.DictReader(io.StringIO(r.text))
    cutoff = datetime.date.today() - datetime.timedelta(days=DIGEST_WINDOW)
    print(f"  Cutoff date: {cutoff}  (DIGEST_WINDOW={DIGEST_WINDOW} days)")
    papers = []
    excluded = 0
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
        # Only include papers published within the digest window
        pub_str = p.get("date", "").strip()
        pub = _parse_date(pub_str)
        if pub is not None and pub < cutoff:
            excluded += 1
            continue
        # Filter by branch using exclusive primary-branch assignment
        if branch_name and _primary_branch(p.get("fields", [])) != branch_name:
            excluded += 1
            continue
        papers.append(p)
    print(f"  Rows: {len(papers)+excluded} total, {excluded} excluded, {len(papers)} kept.")
    if papers:
        print(f"  Sample pub date values: {[p.get('date','') for p in papers[:3]]}")
    papers.sort(key=lambda x: x["score"], reverse=True)
    return papers[:top_n]

# ── Gemini curation ──────────────────────────────────────────────────────────

CURATION_PROMPT = """You are a technology-transfer analyst at Hebrew University of Jerusalem.
Review these {n} research papers and select the most commercially promising ones
for a {period_label} digest sent to investors and industry partners.

Papers:
{paper_list}

Return a JSON object (no markdown) with exactly these keys:
- executive_summary: 3-4 sentence overview of the standout research themes
- selected: list of objects, each with:
    - index: 1-based integer matching the paper number above
    - headline: one punchy sentence on the commercial angle (max 20 words)
    - why_now: 1-2 sentences on timing, market gap, or near-term opportunity

Select up to {max_select} papers (or all if fewer are available).
Pick for commercial impact, not just high score. Aim for diversity of fields.
"""

def curate_with_gemini(papers, monthly=False):
    paper_list = "\n\n".join(
        f"[{i+1}] Score: {p['score']}/50 | PI: {p.get('pi','—')} | "
        f"Fields: {', '.join(p.get('fields', []))}\n"
        f"Title: {p['title']}\n"
        f"Summary: {p.get('summary','')}\n"
        f"Opportunity: {p.get('opportunity','')}"
        for i, p in enumerate(papers)
    )
    period_label = "monthly overview" if monthly else "weekly"
    max_select = 20 if monthly else 12
    prompt = CURATION_PROMPT.format(n=len(papers), paper_list=paper_list,
                                    period_label=period_label, max_select=max_select)
    last_err = None
    for model_id in DIGEST_MODEL_CANDIDATES:
        try:
            print(f"  Trying model: {model_id}")
            resp = client.models.generate_content(model=model_id, contents=prompt)
            print(f"  Success with: {model_id}")
            break
        except Exception as e:
            print(f"  {model_id} failed: {e}")
            last_err = e
    else:
        raise RuntimeError(f"All models failed. Last error: {last_err}")
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


def generate_pdf(papers_by_idx, curation, monthly=False, branch=None):
    today_dt = datetime.date.today()
    today = today_dt.strftime("%B %d, %Y")
    iso = today_dt.isocalendar()
    year = iso[0]
    DIGESTS_DIR.mkdir(exist_ok=True)

    branch_suffix = f"_{branch.replace(' & ', '_').replace(' ', '_')}" if branch else ""
    branch_prefix = f"{branch}  ·  " if branch else ""

    if monthly:
        month = today_dt.month
        output_pdf = DIGESTS_DIR / f"HUJI_digest_{year}_M{month:02d}{branch_suffix}.pdf"
        period_label = f"{branch_prefix}Monthly Digest  ·  {today_dt.strftime('%B %Y')}  ·  {today}"
        doc_title = f"HUJI Research Digest — {branch + ' — ' if branch else ''}{today_dt.strftime('%B %Y')}"
    else:
        week = iso[1]
        output_pdf = DIGESTS_DIR / f"HUJI_digest_{year}_W{week:02d}{branch_suffix}.pdf"
        period_label = f"{branch_prefix}Weekly Digest  ·  Week {week}  ·  {today}"
        doc_title = f"HUJI Research Digest — {branch + ' — ' if branch else ''}Week {week}"

    styles = build_styles()

    doc = SimpleDocTemplate(
        str(output_pdf),
        pagesize=A4,
        leftMargin=16*mm, rightMargin=16*mm,
        topMargin=16*mm, bottomMargin=16*mm,
        title=doc_title,
        author="HUJI Research Monitor",
    )

    story = []

    # ── Header ──
    story.append(Paragraph("HUJI Research Monitor", styles["title"]))
    story.append(Paragraph(period_label, styles["subtitle"]))
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
    return output_pdf


# ── Email delivery ───────────────────────────────────────────────────────────

def load_recipients():
    """One email address per line in digest_recipients.txt; '#' comments and
    blank lines are ignored. Missing file or no entries = no recipients."""
    if not RECIPIENTS_FILE.exists():
        return []
    lines = RECIPIENTS_FILE.read_text(encoding="utf-8").splitlines()
    return [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]


def send_digest_email(reports, monthly=False):
    """Email the generated digests to every address in digest_recipients.txt.

    ``reports`` is a list of dicts: {branch, html_path, pdf_path, html_str}.
    The styled Yissum HTML report is used as the inline e-mail body (the
    "All Branches" one, falling back to the first), and both the HTML and PDF
    files are attached. No-op (with a clear log line) if SMTP credentials
    aren't configured or there are no recipients — never blocks generation.
    """
    recipients = load_recipients()
    if not recipients:
        print("  No recipients configured in digest_recipients.txt — skipping email.")
        return
    if not (SMTP_USER and SMTP_PASSWORD):
        print("  SMTP_USER/SMTP_PASSWORD not set — skipping email "
              "(digests were still generated and committed as usual).")
        return

    period = "Monthly" if monthly else "Weekly"
    today = datetime.date.today().strftime("%B %d, %Y")

    # Prefer the combined "All Branches" report as the inline body.
    body = next((r for r in reports if r.get("branch") is None), reports[0])

    msg = EmailMessage()
    msg["Subject"] = f"Yissum Research Intelligence — {period} Report — {today}"
    msg["From"] = MAIL_FROM
    msg["To"] = ", ".join(recipients)
    msg.set_content(
        f"The {period.lower()} Yissum Research Intelligence Report for {today} "
        f"is attached (HTML + PDF). View this e-mail in an HTML-capable client "
        f"to read it inline, or open the dashboard: {yr.DASHBOARD_URL}"
    )
    msg.add_alternative(body["html_str"], subtype="html")
    for r in reports:
        for path, mt, st in ((r["pdf_path"], "application", "pdf"),
                             (r["html_path"], "text", "html")):
            msg.add_attachment(
                Path(path).read_bytes(),
                maintype=mt, subtype=st, filename=Path(path).name,
            )

    try:
        context = ssl.create_default_context()
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=30) as s:
                s.login(SMTP_USER, SMTP_PASSWORD)
                s.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
                s.starttls(context=context)
                s.login(SMTP_USER, SMTP_PASSWORD)
                s.send_message(msg)
        print(f"  Emailed {len(pdf_paths)} PDF(s) to {len(recipients)} recipient(s).")
    except Exception as e:
        print(f"  Email send failed (digests were still generated and committed as usual): {e}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main(monthly=False):
    # Generate one digest per branch + one combined "All" digest
    branch_cases = [
        (None, "All Branches"),
        ("Healthcare", "Healthcare"),
        ("Agriculture & Food", "Agriculture & Food"),
        ("Exact & Social Sciences", "Exact & Social Sciences"),
    ]

    top_n = 40 if monthly else 20  # monthly draws from a larger pool
    reports = []

    for branch, label in branch_cases:
        print(f"\n{'='*55}")
        print(f"Branch: {label}")

        print("Loading top papers from Google Sheet...")
        papers = load_top_papers(branch_name=branch, top_n=top_n)
        print(f"  {len(papers)} candidates (top {top_n} by score).")

        # Requests #2 + #4: HUJI-primary researchers only, and only
        # high-potential papers (score > 25) — else the 2 best as a fallback.
        papers, is_fallback = yr.select_report_papers(papers)
        if not papers:
            print(f"  No HUJI-primary papers for {label} — skipping.")
            continue
        print(f"  {len(papers)} HUJI-primary paper(s) selected "
              f"({'FALLBACK — no high-potential' if is_fallback else 'high-potential'}).")

        print("Asking Gemini to curate the digest...")
        curation = curate_with_gemini(papers, monthly=monthly)
        selected = curation.get("selected", [])
        print(f"  Gemini selected {len(selected)} papers.")
        print(f"  Executive summary: {curation.get('executive_summary','')[:120]}...")

        print("Generating HTML + PDF reports...")
        html_path, pdf_path = yr.generate_reports(
            papers, curation, monthly=monthly, branch=branch,
            is_fallback=is_fallback, out_dir=DIGESTS_DIR,
        )
        reports.append({
            "branch": branch, "html_path": html_path, "pdf_path": pdf_path,
            "html_str": Path(html_path).read_text(encoding="utf-8"),
        })

    print("\nAll digests done.")

    if reports:
        print("\nSending digest email...")
        send_digest_email(reports, monthly=monthly)


if __name__ == "__main__":
    import sys, traceback, argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--monthly", action="store_true", help="Generate monthly digest instead of weekly")
    args = parser.parse_args()
    if args.monthly:
        DIGEST_WINDOW = 31
    try:
        main(monthly=args.monthly)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
