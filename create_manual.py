"""
Generate a PDF user guide for the HUJI Research Monitor.
Run: python create_manual.py
Output: docs/HUJI_Research_Monitor_Guide.pdf
"""
from pathlib import Path
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle
)

OUT = Path("docs/HUJI_Research_Monitor_Guide.pdf")
OUT.parent.mkdir(parents=True, exist_ok=True)

ACCENT   = colors.HexColor("#7c6ff7")
ACCENT2  = colors.HexColor("#a78bfa")
DARK     = colors.HexColor("#0f1225")
MUTED    = colors.HexColor("#6b7280")
GREEN    = colors.HexColor("#34d399")
YELLOW   = colors.HexColor("#fbbf24")
RED      = colors.HexColor("#f87171")
LIGHT_BG = colors.HexColor("#f3f4f6")

doc = SimpleDocTemplate(
    str(OUT),
    pagesize=A4,
    leftMargin=22*mm, rightMargin=22*mm,
    topMargin=20*mm, bottomMargin=20*mm,
)

styles = getSampleStyleSheet()

H1 = ParagraphStyle("H1", parent=styles["Heading1"],
    fontSize=22, textColor=ACCENT, spaceAfter=4, spaceBefore=0,
    fontName="Helvetica-Bold")
H2 = ParagraphStyle("H2", parent=styles["Heading2"],
    fontSize=13, textColor=ACCENT2, spaceAfter=3, spaceBefore=12,
    fontName="Helvetica-Bold", borderPad=2)
H3 = ParagraphStyle("H3", parent=styles["Heading3"],
    fontSize=10, textColor=DARK, spaceAfter=2, spaceBefore=8,
    fontName="Helvetica-Bold")
BODY = ParagraphStyle("Body", parent=styles["Normal"],
    fontSize=9.5, leading=15, spaceAfter=5, textColor=colors.HexColor("#1f2937"))
NOTE = ParagraphStyle("Note", parent=BODY,
    backColor=LIGHT_BG, borderPad=5, leftIndent=8, rightIndent=8,
    textColor=colors.HexColor("#374151"), fontSize=9)
BULLET = ParagraphStyle("Bullet", parent=BODY,
    bulletIndent=8, leftIndent=18, spaceAfter=3)

def h1(t): return Paragraph(t, H1)
def h2(t): return Paragraph(t, H2)
def h3(t): return Paragraph(t, H3)
def body(t): return Paragraph(t, BODY)
def note(t): return Paragraph(f"<i>ℹ {t}</i>", NOTE)
def bullet(t): return Paragraph(f"• {t}", BULLET)
def sp(n=4): return Spacer(1, n*mm)
def hr(): return HRFlowable(width="100%", thickness=0.5, color=MUTED, spaceAfter=4)

story = []

# ── Title page ────────────────────────────────────────────────────────────────
story += [
    sp(8),
    h1("HUJI Research Monitor"),
    Paragraph("<font size=13 color='#a78bfa'>User Guide & Pipeline Reference</font>", styles["Normal"]),
    sp(2),
    Paragraph("<font size=9 color='#6b7280'>Hebrew University of Jerusalem · Technology Transfer Office</font>", styles["Normal"]),
    sp(4), hr(), sp(2),
]

# ── 1. Overview ───────────────────────────────────────────────────────────────
story += [
    h2("1. Overview"),
    body("The HUJI Research Monitor automatically discovers new academic papers from PubMed, "
         "Europe PMC, and Semantic Scholar that are affiliated with the Hebrew University of Jerusalem "
         "or Hadassah Medical Center. Each paper is evaluated by an AI model (Gemini) across five "
         "commercial dimensions and the results are surfaced in four outputs:"),
    sp(1),
    bullet("<b>Interactive HTML viewer</b> (papers_reader.html) — browse, search, and filter all papers "
           "across three branch dashboards: Healthcare, Agriculture &amp; Food, and Exact &amp; Social Sciences."),
    bullet("<b>Weekly digest PDFs</b> — four curated PDFs each week: one combined and one per branch."),
    bullet("<b>Monthly digest PDFs</b> — same four PDFs with a 31-day window and broader selection."),
    bullet("<b>Google Sheet</b> — full database with all papers, scores, and PI contacts."),
    sp(2),
]

# ── 2. Pipeline schedule ──────────────────────────────────────────────────────
story += [
    h2("2. Automated Schedule"),
    body("Three GitHub Actions workflows run automatically:"),
    sp(1),
]
tdata = [
    ["Workflow", "Schedule", "What it does"],
    ["papers_pipeline.yml", "Every Monday 06:00 UTC", "Fetch new papers, score with AI, update sheet & HTML viewer"],
    ["weekly_digest.yml",   "Every Monday 09:00 UTC", "Generate 4 weekly PDFs: All + Healthcare + Agri&Food + Exact&Social"],
    ["monthly_digest.yml",  "1st of each month 10:00 UTC", "Generate 4 monthly PDFs (31-day window, up to 20 papers each)"],
]
t = Table(tdata, colWidths=[44*mm, 50*mm, 74*mm])
t.setStyle(TableStyle([
    ("BACKGROUND", (0,0), (-1,0), ACCENT),
    ("TEXTCOLOR",  (0,0), (-1,0), colors.white),
    ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
    ("FONTSIZE",   (0,0), (-1,-1), 8.5),
    ("ROWBACKGROUNDS", (0,1), (-1,-1), [LIGHT_BG, colors.white]),
    ("GRID",       (0,0), (-1,-1), 0.4, MUTED),
    ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
    ("TOPPADDING", (0,0), (-1,-1), 4),
    ("BOTTOMPADDING", (0,0), (-1,-1), 4),
    ("LEFTPADDING", (0,0), (-1,-1), 6),
]))
story += [t, sp(2)]

# ── 3. Affiliation filtering ──────────────────────────────────────────────────
story += [
    h2("3. Affiliation Filtering"),
    body("Not every paper that mentions HUJI is included. The pipeline keeps a paper only if "
         "<b>the last author (PI) is from HUJI/Hadassah</b>, OR <b>the majority (&gt;50%) of authors</b> "
         "are affiliated with HUJI or Hadassah Medical Center."),
    sp(1),
    note("Hadassah is treated identically to HUJI — it is the clinical/hospital arm of the university."),
    sp(2),
]

# ── 4. Scoring system ─────────────────────────────────────────────────────────
story += [
    h2("4. Scoring System"),
    body("Each paper is scored on a <b>1–50 scale</b> by summing five sub-scores (each 1–10):"),
    sp(1),
]
score_data = [
    ["Dimension", "What it measures"],
    ["Novelty (1–10)",             "How new and unexpected the scientific finding is"],
    ["Commercial Potential (1–10)", "Likelihood of licensing, spin-out, or product"],
    ["Market Size (1–10)",         "Total addressable market for the application"],
    ["Tech Readiness (1–10)",      "Maturity of the technology (TRL-like)"],
    ["IP Strength (1–10)",         "Patentability and freedom-to-operate outlook"],
]
st = Table(score_data, colWidths=[54*mm, 114*mm])
st.setStyle(TableStyle([
    ("BACKGROUND", (0,0), (-1,0), ACCENT),
    ("TEXTCOLOR",  (0,0), (-1,0), colors.white),
    ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
    ("FONTSIZE",   (0,0), (-1,-1), 8.5),
    ("ROWBACKGROUNDS", (0,1), (-1,-1), [LIGHT_BG, colors.white]),
    ("GRID",       (0,0), (-1,-1), 0.4, MUTED),
    ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
    ("TOPPADDING", (0,0), (-1,-1), 4),
    ("BOTTOMPADDING", (0,0), (-1,-1), 4),
    ("LEFTPADDING", (0,0), (-1,-1), 6),
]))
story += [st, sp(1)]

score_legend = [
    ["Score range", "Color", "Interpretation"],
    ["38–50", "Green",  "High commercial priority — investigate further"],
    ["28–37", "Yellow", "Moderate interest — monitor or request more info"],
    ["0–27",  "Red",    "Lower commercial fit for current focus"],
]
sl = Table(score_legend, colWidths=[30*mm, 24*mm, 114*mm])
sl.setStyle(TableStyle([
    ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#374151")),
    ("TEXTCOLOR",  (0,0), (-1,0), colors.white),
    ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
    ("FONTSIZE",   (0,0), (-1,-1), 8.5),
    ("ROWBACKGROUNDS", (0,1), (-1,-1), [LIGHT_BG, colors.white]),
    ("GRID",       (0,0), (-1,-1), 0.4, MUTED),
    ("TEXTCOLOR", (0,1), (1,1), GREEN),
    ("TEXTCOLOR", (0,2), (1,2), YELLOW),
    ("TEXTCOLOR", (0,3), (1,3), RED),
    ("FONTNAME",  (0,1), (1,3), "Helvetica-Bold"),
    ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
    ("TOPPADDING", (0,0), (-1,-1), 4),
    ("BOTTOMPADDING", (0,0), (-1,-1), 4),
    ("LEFTPADDING", (0,0), (-1,-1), 6),
]))
story += [sl, sp(2),
    note("Scores are AI-generated first-pass estimates. Use them as a triage filter, not a final verdict."),
    sp(2),
]

# ── 5. HTML Viewer ────────────────────────────────────────────────────────────
story += [
    h2("5. Using the HTML Viewer"),
    h3("Branch Dashboards"),
    body("Four tabs at the top switch between the three Yissum TTO branches plus a combined view:"),
    sp(1),
]
branch_data = [
    ["Tab", "Covers"],
    ["All Branches",           "Every paper regardless of field"],
    ["Healthcare",             "Drug Discovery, Medical Device, Diagnostics, Vaccines, Neuroscience, "
                               "Genomics, Imaging, Synthetic Biology, Proteomics, Immunology, Clinical"],
    ["Agriculture & Food",     "AgriTech, FoodTech"],
    ["Exact & Social Sciences","Materials, Clean Energy, Software/AI, Quantum, Other"],
]
bt = Table(branch_data, colWidths=[42*mm, 126*mm])
bt.setStyle(TableStyle([
    ("BACKGROUND", (0,0), (-1,0), ACCENT),
    ("TEXTCOLOR",  (0,0), (-1,0), colors.white),
    ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
    ("FONTSIZE",   (0,0), (-1,-1), 8.5),
    ("ROWBACKGROUNDS", (0,1), (-1,-1), [LIGHT_BG, colors.white]),
    ("GRID",       (0,0), (-1,-1), 0.4, MUTED),
    ("VALIGN",     (0,0), (-1,-1), "TOP"),
    ("TOPPADDING", (0,0), (-1,-1), 4),
    ("BOTTOMPADDING", (0,0), (-1,-1), 4),
    ("LEFTPADDING", (0,0), (-1,-1), 6),
]))
story += [bt, sp(1),
    note("Each paper is assigned to the branch with the most matching field tags. "
         "If two branches tie, the paper appears in both."),
    sp(1),
    body("Switching branch tabs also updates the Latest Weekly and Latest Monthly header links "
         "to point to that branch's specific digest PDF."),
    h3("Search"),
    body("Type any keyword in the search box to filter by title, summary, or commercial opportunity text."),
    h3("Period Filter"),
    body("Narrow results to the last week or last month using the period chips."),
    h3("Score Filter"),
    body("Drag the Score slider to set a minimum total score threshold."),
    h3("Parameter Filter"),
    body("Select a specific scoring dimension (e.g. Novelty) and drag its slider to filter by that sub-score."),
    h3("Field Filter"),
    body("Click a research field chip to show only papers in that domain (within the active branch)."),
    h3("Sort"),
    body("Sort by Score (highest first) or by Date (most recent first)."),
    h3("Score Breakdown"),
    body("Click 'Score Breakdown' on any card to expand the per-dimension scores and AI reasoning."),
    h3("PI Contact"),
    body("If an email was found for the lead researcher, click 'Email' to reveal it."),
    sp(2),
]

# ── 6. Digest PDFs ────────────────────────────────────────────────────────────
story += [
    h2("6. Digest PDFs"),
    body("Each weekly and monthly run generates <b>four PDFs</b> — one combined and one per branch:"),
    sp(1),
    bullet("<b>HUJI_digest_YYYY_W##.pdf</b> — all branches, top 12 papers, 7-day window."),
    bullet("<b>HUJI_digest_YYYY_W##_Healthcare.pdf</b> — Healthcare branch only."),
    bullet("<b>HUJI_digest_YYYY_W##_Agriculture_Food.pdf</b> — Agriculture &amp; Food branch only."),
    bullet("<b>HUJI_digest_YYYY_W##_Exact_Social_Sciences.pdf</b> — Exact &amp; Social Sciences branch only."),
    sp(1),
    body("Monthly PDFs follow the same naming pattern with <i>_M##</i> instead of <i>_W##</i>, "
         "cover the past 31 days, and Gemini selects up to 20 papers per digest."),
    body("Each PDF includes an AI-written executive summary, investor-facing headlines, "
         "and a 'Why Now' paragraph for each selected paper."),
    body("Digests are committed to the <b>digests/</b> folder. The HTML viewer header links "
         "update automatically to the branch-specific digest when you switch tabs."),
    sp(2),
]

# ── 7. Google Sheet ───────────────────────────────────────────────────────────
story += [
    h2("7. Google Sheet"),
    body("The Google Sheet is the master database. It contains every paper with all fields: "
         "title, authors, journal, date, score, sub-scores, summary, opportunity, PI name, and PI email. "
         "The pipeline reads the sheet on startup (to avoid re-processing) and appends new rows after each run."),
    note("The sheet is updated via a Google Apps Script web app — the sheet ID is stored as a GitHub secret."),
    sp(2),
]

# ── 8. Email Enrichment ───────────────────────────────────────────────────────
story += [
    h2("8. PI Email Enrichment"),
    body("For each paper the pipeline attempts to find the corresponding author's email via:"),
    bullet("PubMed efetch XML — extracts the corresponding author email if published in the XML."),
    bullet("CrossRef — queries by DOI for author contact metadata."),
    bullet("ORCID — looks up email from the author's ORCID profile (when public)."),
    body("Up to 25 previously unenriched papers are re-attempted each run. Not all journals publish "
         "author emails, so some gaps are unavoidable."),
    sp(2),
]

# ── 9. Configuration ──────────────────────────────────────────────────────────
story += [
    h2("9. Configuration & Secrets"),
    body("The following GitHub repository secrets are required:"),
    sp(1),
]
cfg_data = [
    ["Secret", "Purpose"],
    ["GEMINI_API_KEY",  "Google Gemini API key for paper scoring"],
    ["GOOGLE_SHEET_ID", "ID of the Google Sheet (from its URL)"],
]
ct = Table(cfg_data, colWidths=[54*mm, 114*mm])
ct.setStyle(TableStyle([
    ("BACKGROUND", (0,0), (-1,0), ACCENT),
    ("TEXTCOLOR",  (0,0), (-1,0), colors.white),
    ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
    ("FONTSIZE",   (0,0), (-1,-1), 8.5),
    ("ROWBACKGROUNDS", (0,1), (-1,-1), [LIGHT_BG, colors.white]),
    ("GRID",       (0,0), (-1,-1), 0.4, MUTED),
    ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
    ("TOPPADDING", (0,0), (-1,-1), 4),
    ("BOTTOMPADDING", (0,0), (-1,-1), 4),
    ("LEFTPADDING", (0,0), (-1,-1), 6),
]))
story += [ct, sp(2)]

# ── 10. Triggering manually ───────────────────────────────────────────────────
story += [
    h2("10. Manual Triggers"),
    body("All three workflows support manual dispatch from the GitHub Actions tab "
         "(Actions → select workflow → Run workflow). This is useful for testing or "
         "when you want to regenerate outputs outside the normal schedule."),
    sp(2), hr(), sp(2),
    Paragraph("<font size=8 color='#6b7280'>HUJI Research Monitor · Technology Transfer Office · "
              "Generated automatically</font>", styles["Normal"]),
]

doc.build(story)
print(f"Manual saved to {OUT}")
