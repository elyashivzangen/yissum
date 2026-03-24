# Email Draft — TTO Colleagues

---

**Subject:** New tool: automated HUJI research monitor with weekly digest

**To:** TTO Team

---

Hi everyone,

I wanted to share a new tool I've set up that I think will save us a lot of manual work in tracking and prioritising HUJI research.

**What it does**

Every Monday morning, the system automatically searches PubMed, Europe PMC, and Semantic Scholar for new papers published by HUJI-affiliated researchers. It then uses Gemini AI to evaluate each paper across five commercial dimensions — novelty, commercial potential, market size, technology readiness, and IP strength — giving each paper a score out of 50. It also identifies the PI and finds their contact email where possible.

**What you get**

1. **An interactive web viewer** (`papers_reader.html`, committed to the repo every Monday) — open it in any browser, no login required. You can search by keyword, filter by field (Genomics, Drug Discovery, AgriTech, etc.), set a minimum score threshold, or filter by how well a paper scores on a specific dimension like IP Strength. Each paper card shows the AI summary, the commercial opportunity, and an expandable score breakdown with Gemini's reasoning.

2. **A weekly digest PDF** (in the `digests/` folder) — a curated 1–2 page document with the 8–12 most commercially promising papers of the week, written up with investor-facing headlines and "why now" context. Useful to forward directly to industry contacts or attach to a meeting agenda.

3. **A shared Google Sheet** — the live database behind everything. All papers, scores, PI names, and emails in one place. You can filter, sort, and add your own notes alongside the AI data.

**How to use it**

- Open `papers_reader.html` from the repository for the full interactive view.
- Check `digests/` for the latest weekly PDF.
- The Google Sheet is always up to date after Monday morning.
- To trigger a manual run (e.g. if you want a 30-day lookback), go to Actions → Papers Pipeline → Run workflow in the GitHub repo.

**What it doesn't replace**

The scores are AI-generated and meant as a first-pass filter, not a final assessment. A high score means "worth a closer look" — your domain expertise and relationships still drive the actual evaluation and outreach.

Happy to walk anyone through it or adjust the scoring criteria if there are dimensions you think matter more for our deal flow.

Best,
[Your name]
