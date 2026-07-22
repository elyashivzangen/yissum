#!/usr/bin/env python3
"""
Yissum Report — shared rendering for the HUJI Research Monitor digests.

Both weekly_digest.py and weekly_digest_enhanced.py import this module so the
weekly *and* monthly reports share one look (the light, Yissum-branded style)
and one set of business rules:

  * HUJI-primary researchers only          (is_huji_primary)
  * only high-potential papers (score > 25); if none, the 2 best as a fallback
    with an explicit "no high-potential research this period" notice
                                            (select_report_papers)
  * five commercialisation metrics per paper, HTS excluded — expandable in the
    HTML, shown statically in the PDF      (METRIC_LABELS / render_*)
  * venue (journal), PI e-mail, a dashboard link and a per-paper deep link
  * an executive summary that lists the researchers and research subjects of the
    high-potential papers

The two public entry points are:

  select_report_papers(candidates)  -> (papers, is_fallback)
  generate_reports(papers, curation, ...) -> (html_path, pdf_path)
"""

import base64
import datetime
import html
import json
from pathlib import Path
from urllib.parse import quote

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_JUSTIFY, TA_LEFT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable,
    KeepTogether, Image,
)

# ── Brand logo assets (Yissum + Hebrew University) ────────────────────────────
ASSETS_DIR      = Path(__file__).resolve().parent / "assets"
YISSUM_LOGO_SVG = ASSETS_DIR / "yissum_logo.svg"
YISSUM_LOGO_PNG = ASSETS_DIR / "yissum_logo.png"
HUJI_LOGO_PNG   = ASSETS_DIR / "huji_logo.png"


def _data_uri(path, mime):
    try:
        return f"data:{mime};base64," + base64.b64encode(Path(path).read_bytes()).decode()
    except Exception:
        return ""


YISSUM_LOGO_URI = _data_uri(YISSUM_LOGO_SVG, "image/svg+xml")
HUJI_LOGO_URI   = _data_uri(HUJI_LOGO_PNG, "image/png")

# ── Public config ─────────────────────────────────────────────────────────────

DASHBOARD_URL = "https://elyashivzangen.github.io/yissum/papers_reader.html"
HIGH_POTENTIAL_THRESHOLD = 25   # score is out of 50; "high potential" = strictly above this

# Five commercialisation dimensions (mirrors papers_pipeline.SCORE_PARAMS).
# HTS is intentionally NOT here — it is never shown in the digest.
METRIC_LABELS = [
    ("novelty",              "Novelty"),
    ("commercial_potential", "Commercial Potential"),
    ("market_size",          "Market Size"),
    ("trl",                  "Tech Readiness"),
    ("ip_strength",          "IP Strength"),
]

# ── Yissum brand palette ──────────────────────────────────────────────────────

BRAND_TEAL      = "#159a8a"   # primary Yissum green/teal
BRAND_TEAL_DARK = "#0f7d70"
BRAND_NAVY      = "#1b2a4a"   # Hebrew University dark
BRAND_RED       = "#c8102e"
BRAND_BLUE      = "#1155a6"
TEXT_DARK       = "#1f2d3d"
TEXT_BODY       = "#34506b"   # the muted blue body text in the reference design
MUTED           = "#8a97a5"
PAGE_BG         = "#eef1f4"
CARD_BG         = "#ffffff"
BORDER          = "#e2e8f0"
GREEN           = "#16a34a"
AMBER           = "#d97706"
RED             = "#dc2626"

# An evocative inline Yissum "N" monogram + wordmark (the real asset is not in
# the repo — this reproduces the brand colours and layout of the reference page).
LOGO_SVG = (
    '<svg width="230" height="42" viewBox="0 0 230 42" xmlns="http://www.w3.org/2000/svg" '
    'role="img" aria-label="Yissum — The Hebrew University Tech Transfer Company">'
    '<rect x="0" y="5" width="7" height="32" rx="1.5" fill="#159a8a"/>'
    '<polygon points="7,5 15,5 33,37 25,37" fill="#c8102e"/>'
    '<rect x="26" y="5" width="7" height="32" rx="1.5" fill="#1b2a4a"/>'
    '<text x="44" y="23" font-family="Arial,Helvetica,sans-serif" font-weight="700" '
    'font-size="21" letter-spacing="1" fill="#159a8a">YISSUM</text>'
    '<text x="45" y="35" font-family="Arial,Helvetica,sans-serif" font-size="7.5" '
    'letter-spacing=".3" fill="#6b7c8f">THE HEBREW UNIVERSITY TECH TRANSFER COMPANY</text>'
    '</svg>'
)


def _brand_html():
    """The Yissum + Hebrew University logo lockup for the report header.

    Falls back to the inline SVG wordmark if the asset files are missing.
    """
    if YISSUM_LOGO_URI and HUJI_LOGO_URI:
        return (
            f'<img class="logo-y" src="{YISSUM_LOGO_URI}" alt="Yissum">'
            f'<span class="divider"></span>'
            f'<img class="logo-h" src="{HUJI_LOGO_URI}" alt="The Hebrew University of Jerusalem">'
        )
    return LOGO_SVG


# ── Business rules ─────────────────────────────────────────────────────────────

def huji_first(aff):
    """True if the PI's *primary* affiliation is HUJI (not a co-located hospital).

    Python port of hujiFirst() in papers_reader.html: an affiliation with no
    competing-hospital mention counts as HUJI-primary; when a hospital (Hadassah
    / Shaare Zedek) is also present, HUJI must be named earlier in the string.
    An empty affiliation is treated as HUJI-primary (missing data isn't hidden).
    """
    if not aff:
        return True
    s = aff.lower()
    other_idx = min(
        (s.find(k) if s.find(k) >= 0 else 10**9)
        for k in ("hadassah", "shaare zedek", "sha'are zedek", "share zedek")
    )
    if other_idx == 10**9:
        return True
    huji_idx = min(
        (s.find(k) if s.find(k) >= 0 else 10**9)
        for k in ("hebrew university of jerusalem", "hebrew university", "hebrew u.")
    )
    if huji_idx == 10**9:
        return False
    return huji_idx < other_idx


def is_huji_primary(p):
    return huji_first(p.get("pi_affiliation", "") or "")


def _score(p):
    try:
        return int(p.get("score", 0) or 0)
    except Exception:
        return 0


def select_report_papers(candidates):
    """Apply requests #2 and #4 to a score-sorted candidate list.

    Returns (papers, is_fallback):
      * HUJI-primary papers scoring above HIGH_POTENTIAL_THRESHOLD, when any
        exist  -> (those papers, False)
      * otherwise the 2 best HUJI-primary papers -> (up to 2 papers, True),
        so the report can say "no high-potential applicable research now".
    """
    huji = [p for p in candidates if is_huji_primary(p)]
    huji.sort(key=_score, reverse=True)
    high = [p for p in huji if _score(p) > HIGH_POTENTIAL_THRESHOLD]
    if high:
        return high, False
    return huji[:2], True


# ── Small formatting helpers ───────────────────────────────────────────────────

def _e(s):
    return html.escape(str(s or ""))


def dashboard_paper_url(pid):
    """Deep link into the dashboard for one paper (see papers_reader.html)."""
    return f"{DASHBOARD_URL}?paper={quote(str(pid or ''))}" if pid else DASHBOARD_URL


def score_color(s):
    """Colour for a /50 total score."""
    if s >= 38:
        return GREEN
    if s >= 28:
        return AMBER
    return RED


def metric_color(s):
    """Colour for a /10 metric."""
    if s >= 8:
        return GREEN
    if s >= 5:
        return AMBER
    return RED


def _breakdown(p):
    """Return score_breakdown as a dict, tolerating a raw JSON string.

    weekly_digest.py's loader parses ``fields`` but leaves ``score_breakdown``
    as the raw CSV string, so coerce it here rather than assuming a dict.
    """
    bd = p.get("score_breakdown")
    if isinstance(bd, str):
        bd = bd.strip()
        if not bd:
            return {}
        try:
            bd = json.loads(bd)
        except Exception:
            return {}
    return bd if isinstance(bd, dict) else {}


def _pi_name(p):
    return (p.get("pi_full_name") or p.get("pi") or "").strip()


def _subject(p):
    """Short research-subject label: the field tags, else a trimmed title."""
    fields = p.get("fields") or []
    if isinstance(fields, str):
        try:
            fields = json.loads(fields)
        except Exception:
            fields = [fields]
    if fields:
        return ", ".join(str(f) for f in fields)
    title = (p.get("title") or "").strip()
    return (title[:70] + "…") if len(title) > 71 else title


def _selected_papers(papers, curation):
    """Yield (rank, paper, item) for each curated selection, in order."""
    for rank, item in enumerate(curation.get("selected", []), start=1):
        idx = item.get("index", 0) - 1
        if 0 <= idx < len(papers):
            yield rank, papers[idx], item, idx


def build_highlights(papers, curation):
    """Request #5: researchers + research subjects of the high-potential papers."""
    out = []
    seen = set()
    for _rank, p, _item, _idx in _selected_papers(papers, curation):
        pi = _pi_name(p) or "Researcher (name pending)"
        key = (pi, p.get("title", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append({"pi": pi, "subject": _subject(p), "title": (p.get("title") or "").strip()})
    return out


def report_meta(monthly, branch, variant=""):
    """Filenames, period label and titles for one report."""
    today_dt = datetime.date.today()
    today = today_dt.strftime("%B %d, %Y")
    iso = today_dt.isocalendar()
    year = iso[0]
    branch_suffix = f"_{branch.replace(' & ', '_').replace(' ', '_')}" if branch else ""
    branch_prefix = f"{branch} · " if branch else ""

    if monthly:
        month = today_dt.month
        base = f"HUJI_digest_{year}_M{month:02d}{branch_suffix}{variant}"
        period = today_dt.strftime("%B %Y")
        title = f"Yissum Research Intelligence Report — {branch + ' — ' if branch else ''}{period}"
        subtitle = f"{branch_prefix}Monthly · {period} · Generated {today}"
    else:
        week = iso[1]
        base = f"HUJI_digest_{year}_W{week:02d}{branch_suffix}{variant}"
        period = f"Week {week}, {year}"
        title = f"Yissum Research Intelligence Report — {branch + ' — ' if branch else ''}{period}"
        subtitle = f"{branch_prefix}Weekly · {period} · Generated {today}"

    return {
        "html_name": base + ".html",
        "pdf_name": base + ".pdf",
        "title": title,
        "subtitle": subtitle,
        "period": period,
        "today": today,
        "branch": branch or "All Branches",
        "monthly": monthly,
    }


FALLBACK_NOTICE = (
    "There is no high-potential applicable research this period — only "
    "lower-scoring papers were found. Presenting the two best below."
)


# ── HTML rendering ─────────────────────────────────────────────────────────────

_HTML_STYLE = """
:root{--teal:#159a8a;--teal-dark:#0f7d70;--navy:#1b2a4a;--red:#c8102e;
  --text:#1f2d3d;--body:#34506b;--muted:#8a97a5;--bg:#eef1f4;--card:#fff;--border:#e2e8f0}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
  line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:1000px;margin:0 auto;padding:24px 18px 60px}
.topbar{display:flex;align-items:center;justify-content:space-between;gap:16px;
  flex-wrap:wrap;padding:6px 4px 22px}
.brand{display:flex;align-items:center;gap:16px}
.brand img.logo-y{height:42px;width:auto;display:block}
.brand img.logo-h{height:38px;width:auto;display:block}
.brand .divider{width:1px;height:34px;background:#d5dde3}
.dash-btn{background:var(--teal);color:#fff;text-decoration:none;font-weight:700;
  font-size:.92rem;padding:11px 20px;border-radius:8px;white-space:nowrap;
  box-shadow:0 2px 8px rgba(21,154,138,.25);transition:background .15s}
.dash-btn:hover{background:var(--teal-dark)}
.sheet{background:var(--card);border:1px solid var(--border);border-radius:14px;
  box-shadow:0 6px 30px rgba(27,42,74,.07);padding:34px 38px}
h1{font-size:1.9rem;line-height:1.25;margin:0 0 6px;color:var(--text);font-weight:800}
.sub{color:var(--muted);font-size:.9rem;margin:0 0 20px}
.intro{color:var(--body);font-size:1.02rem;margin:0 0 22px}
.notice{background:#fff7ed;border:1px solid #fed7aa;border-left:4px solid #f59e0b;
  color:#9a3412;border-radius:10px;padding:14px 18px;margin:0 0 24px;font-size:.95rem;font-weight:600}
.highlights{background:#f0faf8;border:1px solid #cdeee8;border-left:4px solid var(--teal);
  border-radius:10px;padding:16px 20px;margin:0 0 28px}
.highlights h2{font-size:.78rem;letter-spacing:.06em;text-transform:uppercase;
  color:var(--teal-dark);margin:0 0 10px;font-weight:800}
.highlights ul{margin:0;padding-left:18px}
.highlights li{margin:5px 0;color:var(--body);font-size:.95rem}
.highlights li b{color:var(--text)}
.section-h{color:var(--teal-dark);font-size:.85rem;letter-spacing:.08em;
  text-transform:uppercase;font-weight:800;border-bottom:2px solid var(--teal);
  padding-bottom:8px;margin:30px 0 18px}
.card{border:1px solid var(--border);border-left:4px solid var(--teal);
  border-radius:10px;padding:18px 22px;margin:0 0 18px;background:var(--card)}
.card-top{display:flex;justify-content:space-between;gap:14px;align-items:flex-start}
.card-title{font-size:1.12rem;font-weight:700;color:var(--text);margin:0}
.badge{flex:none;font-weight:800;font-size:.9rem;padding:6px 12px;border-radius:8px;
  border:1px solid var(--border);white-space:nowrap}
.headline{color:var(--teal-dark);font-style:italic;font-weight:600;margin:10px 0 4px}
.pi-line{margin:12px 0 2px;font-size:.92rem;color:var(--body)}
.pi-line b{color:var(--navy)}
.pi-email a{color:var(--teal-dark);text-decoration:none;font-weight:600}
.pi-email a:hover{text-decoration:underline}
.venue{font-size:.85rem;color:var(--muted);margin:2px 0 0}
.venue b{color:var(--body);font-weight:600}
.lbl{font-size:.72rem;letter-spacing:.05em;text-transform:uppercase;color:var(--muted);
  font-weight:700;margin:12px 0 3px}
.txt{font-size:.95rem;color:var(--body);margin:0 0 4px}
.tags{margin:10px 0 0}
.tag{display:inline-block;font-size:.72rem;color:var(--teal-dark);background:#e9f6f3;
  border:1px solid #cdeee8;border-radius:999px;padding:2px 10px;margin:0 6px 6px 0}
.metrics{margin:14px 0 4px}
.metric{display:flex;align-items:center;gap:10px;width:100%;text-align:left;
  background:#f7f9fb;border:1px solid var(--border);border-radius:8px;
  padding:8px 12px;margin:6px 0;cursor:pointer;font:inherit;color:var(--text);transition:border-color .15s}
.metric:hover{border-color:var(--teal)}
.metric.static{cursor:default}
.m-name{font-weight:600;font-size:.9rem;flex:1}
.m-score{font-weight:800;font-size:.9rem}
.m-bar-bg{flex:none;width:90px;height:6px;border-radius:3px;background:#e5eaf0;overflow:hidden}
.m-bar{display:block;height:100%;border-radius:3px}
.m-caret{color:var(--muted);font-size:.8rem;flex:none}
.m-reason{display:none;font-size:.88rem;color:var(--body);padding:2px 12px 10px;margin:-2px 0 4px}
.m-reason.open{display:block}
.actions{margin-top:14px;display:flex;gap:10px;flex-wrap:wrap}
.act{text-decoration:none;font-weight:700;font-size:.85rem;padding:8px 16px;border-radius:8px;
  border:1px solid var(--teal);color:var(--teal-dark);background:#fff;transition:all .15s}
.act:hover{background:var(--teal);color:#fff}
.act.primary{background:var(--teal);color:#fff}
.act.primary:hover{background:var(--teal-dark)}
.footer{text-align:center;color:var(--muted);font-size:.8rem;margin-top:30px}
@media(max-width:560px){.sheet{padding:22px 18px}h1{font-size:1.5rem}}
"""

_HTML_SCRIPT = """
function tm(btn){var r=btn.nextElementSibling;if(!r)return;
  var open=r.classList.toggle('open');
  var c=btn.querySelector('.m-caret');if(c)c.textContent=open?'\\u25B4':'\\u25BE';}
"""


def _metric_html(paper):
    bd = _breakdown(paper)
    if not bd:
        return ""
    rows = []
    has_reason = False
    for key, label in METRIC_LABELS:
        v = bd.get(key) or {}
        try:
            s = int(v.get("score", 0) or 0)
        except Exception:
            s = 0
        reason = (v.get("reason") or "").strip()
        col = metric_color(s)
        interactive = bool(reason)
        has_reason = has_reason or interactive
        caret = '<span class="m-caret">▾</span>' if interactive else ""
        cls = "metric" if interactive else "metric static"
        onclick = ' onclick="tm(this)"' if interactive else ""
        rows.append(
            f'<button class="{cls}"{onclick}>'
            f'<span class="m-name">{_e(label)}</span>'
            f'<span class="m-bar-bg"><span class="m-bar" style="width:{s*10}%;background:{col}"></span></span>'
            f'<span class="m-score" style="color:{col}">{s}/10</span>{caret}</button>'
        )
        if interactive:
            rows.append(f'<div class="m-reason">{_e(reason)}</div>')
    hint = " Click a metric to see why." if has_reason else ""
    return (
        f'<div class="lbl">Commercialisation metrics{hint}</div>'
        f'<div class="metrics">{"".join(rows)}</div>'
    )


def _card_html(rank, paper, item, enrichment=None, pi_trend=None):
    score = _score(paper)
    col = score_color(score)
    parts = [
        '<div class="card">',
        '<div class="card-top">',
        f'<div class="card-title">{rank}. {_e(paper.get("title"))}</div>',
        f'<div class="badge" style="color:{col};border-color:{col}">{score}/50</div>',
        '</div>',
    ]
    headline = (item.get("headline") or "").strip()
    if headline:
        parts.append(f'<div class="headline">“{_e(headline)}”</div>')

    pi = _pi_name(paper)
    if pi:
        line = f'<div class="pi-line">Main researcher: <b>{_e(pi)}</b>'
        email = (paper.get("pi_email") or "").strip()
        if email:
            line += f' · <span class="pi-email"><a href="mailto:{_e(email)}">{_e(email)}</a></span>'
        if pi_trend:
            t = pi_trend
            arrow = {"trending up": "↑", "trending down": "↓", "stable": "→"}.get(t.get("trend") or "", "")
            line += (f' <span style="color:#8a97a5">· {t.get("count")} paper(s) in system,'
                     f' avg {t.get("avg")}{(" " + arrow + " " + t.get("trend")) if t.get("trend") else ""}</span>')
        line += "</div>"
        parts.append(line)
        aff = (paper.get("pi_affiliation") or "").strip()
        if aff:
            parts.append(f'<div class="venue">🏛️ {_e(aff)}</div>')

    venue = (paper.get("journal") or "").strip()
    date = (paper.get("date") or "").strip()
    if venue or date:
        pub = "Published in <b>" + _e(venue) + "</b>" if venue else "Published"
        if date:
            pub += f" · {_e(date)}"
        parts.append(f'<div class="venue">{pub}</div>')

    why = (item.get("why_now") or "").strip()
    if why:
        parts.append('<div class="lbl">Why now</div>')
        parts.append(f'<div class="txt">{_e(why)}</div>')

    opp = (paper.get("opportunity") or "").strip()
    if opp:
        parts.append('<div class="lbl">Commercial angle</div>')
        parts.append(f'<div class="txt">{_e(opp)}</div>')

    if enrichment:
        deal = (enrichment.get("comparable_deal") or "").strip()
        if deal and deal != "No direct comparable found":
            parts.append('<div class="lbl">Comparable deal</div>')
            parts.append(f'<div class="txt">{_e(deal)}</div>')
        comp = (enrichment.get("competitor_scan") or "").strip()
        if comp and comp != "No direct competitors identified":
            parts.append('<div class="lbl">Competitor scan</div>')
            parts.append(f'<div class="txt">{_e(comp)}</div>')

    parts.append(_metric_html(paper))

    fields = paper.get("fields") or []
    if isinstance(fields, str):
        try:
            fields = json.loads(fields)
        except Exception:
            fields = []
    if fields:
        parts.append('<div class="tags">' + "".join(f'<span class="tag">{_e(f)}</span>' for f in fields) + '</div>')

    url = (paper.get("url") or "").strip()
    parts.append('<div class="actions">')
    parts.append(f'<a class="act primary" href="{_e(dashboard_paper_url(paper.get("id")))}" target="_blank">View in Dashboard</a>')
    if url:
        parts.append(f'<a class="act" href="{_e(url)}" target="_blank">Open Paper</a>')
    parts.append('</div>')
    parts.append('</div>')
    return "".join(parts)


def render_html(papers, curation, meta, is_fallback, pi_trends=None, enrichments=None):
    highlights = build_highlights(papers, curation)
    n = len(highlights)
    label = "high-potential paper" if not is_fallback else "paper"

    hi_html = ""
    if highlights:
        lis = "".join(
            f'<li><b>{_e(h["pi"])}</b> — {_e(h["subject"])}'
            + (f' · <span style="color:#8a97a5">{_e(h["title"])}</span>' if h["title"] else "")
            + '</li>'
            for h in highlights
        )
        heading = ("Researchers & research subjects — high-potential papers"
                   if not is_fallback else "Researchers & research subjects — best available")
        hi_html = f'<div class="highlights"><h2>{_e(heading)}</h2><ul>{lis}</ul></div>'

    cards = []
    for rank, paper, item, idx in _selected_papers(papers, curation):
        enr = (enrichments or {}).get(idx)
        trend = (pi_trends or {}).get((paper.get("pi") or "").strip())
        cards.append(_card_html(rank, paper, item, enrichment=enr, pi_trend=trend))

    notice = f'<div class="notice">⚠️ {_e(FALLBACK_NOTICE)}</div>' if is_fallback else ""
    section_title = _e(meta["branch"]).upper() + " — RESEARCH HIGHLIGHTS"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(meta['title'])}</title>
<style>{_HTML_STYLE}</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div class="brand">{_brand_html()}</div>
    <a class="dash-btn" href="{_e(DASHBOARD_URL)}" target="_blank">Open Dashboard →</a>
  </div>
  <div class="sheet">
    <h1>{_e(meta['title'])}</h1>
    <p class="sub">{n} {label}(s) · {_e(meta['subtitle'])}</p>
    {notice}
    <p class="intro">{_e(curation.get('executive_summary',''))}</p>
    {hi_html}
    <div class="section-h">{section_title}</div>
    {''.join(cards)}
    <div class="footer">Generated {_e(meta['today'])} · Yissum — Hebrew University Technology Transfer Company · Powered by Gemini AI</div>
  </div>
</div>
<script>{_HTML_SCRIPT}</script>
</body>
</html>
"""


# ── PDF rendering (same light Yissum style) ────────────────────────────────────

def _hex(c):
    return colors.HexColor(c)


def _pdf_styles():
    base = getSampleStyleSheet()
    mk = lambda name, **kw: ParagraphStyle(name, parent=base["Normal"], **kw)
    return {
        "title": mk("t", fontSize=19, leading=23, textColor=_hex(TEXT_DARK),
                    fontName="Helvetica-Bold", spaceAfter=3),
        "sub": mk("s", fontSize=8.5, leading=12, textColor=_hex(MUTED), fontName="Helvetica"),
        "intro": mk("i", fontSize=10, leading=15, textColor=_hex(TEXT_BODY),
                    fontName="Helvetica", alignment=TA_JUSTIFY),
        "notice": mk("n", fontSize=9.5, leading=13, textColor=_hex("#9a3412"),
                     fontName="Helvetica-Bold"),
        "hi_h": mk("hh", fontSize=8, leading=11, textColor=_hex(BRAND_TEAL_DARK),
                   fontName="Helvetica-Bold", spaceAfter=4),
        "hi": mk("hi", fontSize=9, leading=13, textColor=_hex(TEXT_BODY), fontName="Helvetica"),
        "section": mk("sec", fontSize=9, leading=12, textColor=_hex(BRAND_TEAL_DARK),
                      fontName="Helvetica-Bold", spaceAfter=2),
        "card_title": mk("ct", fontSize=11.5, leading=15, textColor=_hex(TEXT_DARK),
                         fontName="Helvetica-Bold"),
        "headline": mk("hl", fontSize=9.5, leading=13, textColor=_hex(BRAND_TEAL_DARK),
                       fontName="Helvetica-Oblique", spaceBefore=3, spaceAfter=2),
        "pi": mk("pi", fontSize=9, leading=12, textColor=_hex(BRAND_NAVY), fontName="Helvetica-Bold"),
        "venue": mk("v", fontSize=8, leading=11, textColor=_hex(MUTED), fontName="Helvetica"),
        "lbl": mk("l", fontSize=7, leading=9, textColor=_hex(MUTED), fontName="Helvetica-Bold",
                  spaceBefore=4, spaceAfter=1),
        "body": mk("b", fontSize=8.5, leading=12, textColor=_hex(TEXT_BODY),
                   fontName="Helvetica", alignment=TA_JUSTIFY),
        "metric": mk("m", fontSize=8, leading=11, textColor=_hex(TEXT_DARK), fontName="Helvetica"),
        "reason": mk("r", fontSize=7.5, leading=10, textColor=_hex(TEXT_BODY), fontName="Helvetica"),
        "link": mk("lk", fontSize=8, leading=11, textColor=_hex(BRAND_TEAL_DARK),
                   fontName="Helvetica-Bold", spaceBefore=4),
        "footer": mk("f", fontSize=7.5, leading=10, textColor=_hex(MUTED), fontName="Helvetica"),
    }


def _pdf_metric_rows(paper, st, page_w):
    bd = _breakdown(paper)
    if not bd:
        return None
    data = []
    for key, label in METRIC_LABELS:
        v = bd.get(key) or {}
        try:
            s = int(v.get("score", 0) or 0)
        except Exception:
            s = 0
        reason = _e(v.get("reason", ""))
        col = metric_color(s)
        cell = [Paragraph(f'<b>{_e(label)}</b>', st["metric"]),
                Paragraph(f'<font color="{col}"><b>{s}/10</b></font>', st["metric"])]
        row = Table([[cell[0], cell[1]]], colWidths=[page_w - 60, 40])
        row.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        block = [row]
        if reason:
            block.append(Paragraph(reason, st["reason"]))
        data.append(block)
    return data


def _pdf_card(rank, paper, item, st, page_w, enrichment=None):
    inner = []
    score = _score(paper)
    col = score_color(score)
    head = Table(
        [[Paragraph(f'{rank}. {_e(paper.get("title"))}', st["card_title"]),
          Paragraph(f'<font color="{col}"><b>{score}/50</b></font>', st["card_title"])]],
        colWidths=[page_w - 60, 44],
    )
    head.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT"),
    ]))
    inner.append(head)

    headline = (item.get("headline") or "").strip()
    if headline:
        inner.append(Paragraph(f'"{_e(headline)}"', st["headline"]))

    pi = _pi_name(paper)
    if pi:
        line = f'Main researcher: {_e(pi)}'
        email = (paper.get("pi_email") or "").strip()
        if email:
            line += f'  ·  <font color="{BRAND_TEAL_DARK}">{_e(email)}</font>'
        inner.append(Paragraph(line, st["pi"]))
        aff = (paper.get("pi_affiliation") or "").strip()
        if aff:
            inner.append(Paragraph(_e(aff), st["venue"]))

    venue = (paper.get("journal") or "").strip()
    date = (paper.get("date") or "").strip()
    if venue or date:
        pub = ("Published in " + _e(venue)) if venue else "Published"
        if date:
            pub += f"  ·  {_e(date)}"
        inner.append(Paragraph(pub, st["venue"]))

    why = (item.get("why_now") or "").strip()
    if why:
        inner.append(Paragraph("WHY NOW", st["lbl"]))
        inner.append(Paragraph(_e(why), st["body"]))
    opp = (paper.get("opportunity") or "").strip()
    if opp:
        inner.append(Paragraph("COMMERCIAL ANGLE", st["lbl"]))
        inner.append(Paragraph(_e(opp), st["body"]))
    if enrichment:
        deal = (enrichment.get("comparable_deal") or "").strip()
        if deal and deal != "No direct comparable found":
            inner.append(Paragraph("COMPARABLE DEAL", st["lbl"]))
            inner.append(Paragraph(_e(deal), st["body"]))
        comp = (enrichment.get("competitor_scan") or "").strip()
        if comp and comp != "No direct competitors identified":
            inner.append(Paragraph("COMPETITOR SCAN", st["lbl"]))
            inner.append(Paragraph(_e(comp), st["body"]))

    metric_rows = _pdf_metric_rows(paper, st, page_w)
    if metric_rows:
        inner.append(Paragraph("COMMERCIALISATION METRICS", st["lbl"]))
        for block in metric_rows:
            inner.extend(block)

    url = (paper.get("url") or "").strip()
    dash = dashboard_paper_url(paper.get("id"))
    links = f'<link href="{_e(dash)}">View in Dashboard</link>'
    if url:
        links += f'    <link href="{_e(url)}">Open Paper</link>'
    inner.append(Paragraph(links, st["link"]))

    card = Table([[inner]], colWidths=[page_w])
    card.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _hex(CARD_BG)),
        ("BOX", (0, 0), (-1, -1), 0.6, _hex(BORDER)),
        ("LINEBEFORE", (0, 0), (0, -1), 2.2, _hex(BRAND_TEAL)),
        ("LEFTPADDING", (0, 0), (-1, -1), 5 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 4 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4 * mm),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return [KeepTogether(card), Spacer(1, 3 * mm)]


def render_pdf(path, papers, curation, meta, is_fallback, pi_trends=None, enrichments=None):
    st = _pdf_styles()
    page_w = A4[0] - 32 * mm
    doc = SimpleDocTemplate(
        str(path), pagesize=A4,
        leftMargin=16 * mm, rightMargin=16 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
        title=meta["title"], author="Yissum — HUJI Research Monitor",
    )
    story = []

    # Header band (white) with the Yissum + Hebrew University logos and a link.
    brand_items, brand_widths = [], []
    if YISSUM_LOGO_PNG.exists():
        brand_items.append(Image(str(YISSUM_LOGO_PNG), width=10 * mm * (436 / 120), height=10 * mm))
        brand_widths.append(10 * mm * (436 / 120) + 6 * mm)
    if HUJI_LOGO_PNG.exists():
        brand_items.append(Image(str(HUJI_LOGO_PNG), width=9 * mm * (843 / 293), height=9 * mm))
        brand_widths.append(9 * mm * (843 / 293) + 6 * mm)
    if brand_items:
        brand_cell = Table([brand_items], colWidths=brand_widths)
        brand_cell.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
    else:
        brand_cell = Paragraph('<b><font color="#159a8a">YISSUM</font></b>',
                               ParagraphStyle("hz", fontSize=14, leading=16))
    header = Table(
        [[brand_cell,
          Paragraph(f'<link href="{_e(DASHBOARD_URL)}"><font color="#0f7d70"><b>Open Dashboard →</b></font></link>',
                    ParagraphStyle("hd", fontSize=9.5, leading=13, alignment=2))]],
        colWidths=[page_w - 46 * mm, 46 * mm],
    )
    header.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 2 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, -1), 1.4, _hex(BRAND_TEAL)),
    ]))
    story.append(header)
    story.append(Spacer(1, 5 * mm))

    story.append(Paragraph(_e(meta["title"]), st["title"]))
    highlights = build_highlights(papers, curation)
    label = "high-potential paper" if not is_fallback else "paper"
    story.append(Paragraph(f'{len(highlights)} {label}(s) · {_e(meta["subtitle"])}', st["sub"]))
    story.append(Spacer(1, 4 * mm))

    if is_fallback:
        notice = Table([[Paragraph("⚠️ " + _e(FALLBACK_NOTICE), st["notice"])]], colWidths=[page_w])
        notice.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), _hex("#fff7ed")),
            ("BOX", (0, 0), (-1, -1), 0.6, _hex("#fed7aa")),
            ("LINEBEFORE", (0, 0), (0, -1), 2.2, _hex("#f59e0b")),
            ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
            ("TOPPADDING", (0, 0), (-1, -1), 3 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
        ]))
        story.append(notice)
        story.append(Spacer(1, 4 * mm))

    story.append(Paragraph(_e(curation.get("executive_summary", "")), st["intro"]))
    story.append(Spacer(1, 4 * mm))

    if highlights:
        hi_inner = [Paragraph(
            ("RESEARCHERS & RESEARCH SUBJECTS — HIGH-POTENTIAL PAPERS"
             if not is_fallback else "RESEARCHERS & RESEARCH SUBJECTS — BEST AVAILABLE"), st["hi_h"])]
        for h in highlights:
            subj = f'<b>{_e(h["pi"])}</b> — {_e(h["subject"])}'
            hi_inner.append(Paragraph("• " + subj, st["hi"]))
        hi = Table([[hi_inner]], colWidths=[page_w])
        hi.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), _hex("#f0faf8")),
            ("BOX", (0, 0), (-1, -1), 0.6, _hex("#cdeee8")),
            ("LINEBEFORE", (0, 0), (0, -1), 2.2, _hex(BRAND_TEAL)),
            ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
            ("TOPPADDING", (0, 0), (-1, -1), 3 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
        ]))
        story.append(hi)
        story.append(Spacer(1, 5 * mm))

    story.append(Paragraph(_e(meta["branch"]).upper() + " — RESEARCH HIGHLIGHTS", st["section"]))
    story.append(HRFlowable(width="100%", thickness=1.4, color=_hex(BRAND_TEAL)))
    story.append(Spacer(1, 4 * mm))

    for rank, paper, item, idx in _selected_papers(papers, curation):
        enr = (enrichments or {}).get(idx)
        story.extend(_pdf_card(rank, paper, item, st, page_w, enrichment=enr))

    story.append(Spacer(1, 3 * mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=_hex(BORDER)))
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph(
        f"Generated {_e(meta['today'])} · Yissum — Hebrew University Technology "
        f"Transfer Company · Powered by Gemini AI", st["footer"]))

    doc.build(story)
    return path


# ── Combined entry point ───────────────────────────────────────────────────────

def generate_reports(papers, curation, *, monthly, branch, is_fallback, out_dir,
                     variant="", pi_trends=None, enrichments=None):
    """Write both the HTML and the PDF report; return (html_path, pdf_path)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(exist_ok=True)
    meta = report_meta(monthly, branch, variant=variant)

    html_path = out_dir / meta["html_name"]
    html_path.write_text(
        render_html(papers, curation, meta, is_fallback,
                    pi_trends=pi_trends, enrichments=enrichments),
        encoding="utf-8",
    )

    pdf_path = out_dir / meta["pdf_name"]
    render_pdf(pdf_path, papers, curation, meta, is_fallback,
               pi_trends=pi_trends, enrichments=enrichments)

    print(f"  HTML written: {html_path}  ({html_path.stat().st_size // 1024} KB)")
    print(f"  PDF written:  {pdf_path}  ({pdf_path.stat().st_size // 1024} KB)")
    return html_path, pdf_path
