import requests, hashlib, json, re, pathlib, datetime, logging
from bs4 import BeautifulSoup
import pdfplumber
from requests.exceptions import RequestException

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# ------------- no change -------------
SITES = {
    "pfizer_gmg": "https://www.pfizer.com/about/programs-policies/grants/competitive-grants",
    "bayer_g4t":  "https://collaboratetocurehubjapan.bayer.co.jp/en/home/researchgrant/grants4targets",
        "bayer_g4t2":  "https://www.bayer.com/en/innovation/open-innovation-and-collaboration",

}
# --------------------------------------

def doc_links(url: str):
    """Yield absolute URLs of all PDF/Word docs on the landing page.
       Any network error just logs and returns nothing."""
    try:
        html = requests.get(url, timeout=30)          # longer timeout
        html.raise_for_status()
    except RequestException as e:
        logging.warning(f"⚠️  Skipping {url}: {e}")
        return []                                     # nothing yielded, but run continues

    soup = BeautifulSoup(html.text, "html.parser")
    for a in soup.find_all("a", href=True):
        if a["href"].lower().endswith((".pdf", ".doc", ".docx")):
            yield a["href"] if a["href"].startswith("http") else \
                  requests.compat.urljoin(url, a["href"])
