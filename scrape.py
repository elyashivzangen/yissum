#!/usr/bin/env python3
"""
scrape.py  –  Weekly pharma-RFP harvester
----------------------------------------
* Visits each URL in SITES
* Collects links ending in .pdf / .doc / .docx
* Downloads new files into data/<sha1>.pdf
* Extracts “Issued / Deadline” dates from first 1 500 chars
* Appends / updates latest_rfps.json  (for GPT ingestion)
"""

from __future__ import annotations
import hashlib, json, re, pathlib, logging, datetime
import requests, pdfplumber
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException
from urllib3.util.retry import Retry


# ------------------------------------------------------------------
# 0.  Settings
# ------------------------------------------------------------------
SITES = {
    "pfizer_gmg": "https://www.pfizer.com/about/programs-policies/grants/competitive-grants",
    "bayer_g4t":  "https://collaboratetocurehubjapan.bayer.co.jp/en/home/researchgrant/grants4targets",
    "bayer_g4t2": "https://www.bayer.com/en/innovation/open-innovation-and-collaboration",
}

DATA_DIR  = pathlib.Path("data")
DATA_DIR.mkdir(exist_ok=True)

JSON_PATH = pathlib.Path("latest_rfps.json")

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


# ------------------------------------------------------------------
# 1.  Retry-capable requests.Session
# ------------------------------------------------------------------
def build_session(retries: int = 3, backoff: int = 2, timeout: int = 30) -> requests.Session:
    sess = requests.Session()
    retry_cfg = Retry(
        total=retries,
        connect=retries,
        read=retries,
        backoff_factor=backoff,
        status_forcelist=[502, 503, 504],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_cfg)
    sess.mount("https://", adapter)
    sess.mount("http://",  adapter)
    sess.request_timeout = timeout                    # custom attr
    return sess

session = build_session()


# ------------------------------------------------------------------
# 2.  Helpers
# ------------------------------------------------------------------
def doc_links(url: str):
    """
    Yield absolute URLs for every PDF / Word doc linked from `url`.
    Any timeout / HTTP error is logged and swallowed, so the scraper
    continues with the next site.
    """
    try:
        resp = session.get(url, timeout=session.request_timeout)
        resp.raise_for_status()
    except RequestException as e:
        logging.warning(f"SKIP {url} ↯ {e}")
        return                                          # nothing yielded

    soup = BeautifulSoup(resp.text, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().endswith((".pdf", ".doc", ".docx")):
            yield href if href.startswith("http") else \
                  requests.compat.urljoin(url, href)


date_pat = re.compile(r"(Issued|Posted):\s*([\dA-Za-z ,]+)", re.I)
ddl_pat  = re.compile(r"(Deadline|Due):\s*([\dA-Za-z ,]+)",  re.I)

def parse_pdf(path: pathlib.Path) -> dict[str, str]:
    try:
        with pdfplumber.open(path) as pdf:
            txt = pdf.pages[0].extract_text()[:1500]
    except Exception as e:
        logging.warning(f"⚠️  PDF parse failed {path.name}: {e}")
        return {"posted": "n/a", "deadline": "n/a", "snippet": ""}

    posted   = date_pat.search(txt)
    deadline = ddl_pat.search(txt)
    return {
        "posted":   posted.group(2).strip()   if posted   else "n/a",
        "deadline": deadline.group(2).strip() if deadline else "n/a",
        "snippet":  " ".join(txt.splitlines()[:5]),
    }


# ------------------------------------------------------------------
# 3.  Main driver
# ------------------------------------------------------------------
def main() -> None:
    logging.info("🌀  Scraper start %s", datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z")

    out: list[dict] = []
    seen_hashes = {p.stem for p in DATA_DIR.glob("*.pdf")}

    for tag, url in SITES.items():
        logging.info("🌐  %s → %s", tag, url)
        for link in doc_links(url):
            h = hashlib.sha1(link.encode()).hexdigest()
            if h in seen_hashes:
                continue                                # already downloaded

            logging.info("⬇️  Download %s", link)
            try:
                pdf_bytes = session.get(link, timeout=session.request_timeout).content
            except RequestException as e:
                logging.warning("⚠️  Download failed %s: %s", link, e)
                continue

            file_path = DATA_DIR / f"{h}.pdf"
            file_path.write_bytes(pdf_bytes)
            meta = parse_pdf(file_path) | {"portal": tag, "source": link}
            out.append(meta)

    # merge with existing JSON (keeps history)
    if JSON_PATH.exists():
        existing = json.loads(JSON_PATH.read_text())
        out.extend(existing)

    JSON_PATH.write_text(json.dumps(out, indent=2))
    logging.info("✅  Wrote %d total RFP entries to %s", len(out), JSON_PATH)


if __name__ == "__main__":
    main()
