import requests, hashlib, json, re, pathlib, datetime
from bs4 import BeautifulSoup
import pdfplumber

DATA = pathlib.Path("data")
DATA.mkdir(exist_ok=True)

SITES = {
    "pfizer_gmg": "https://www.pfizer.com/about/programs-policies/grants/competitive-grants",
    "bayer_g4t":  "https://www.grants4targets.com",
}

def doc_links(url: str):
    """Yield absolute URLs of all PDF/Word docs on the landing page."""
    soup = BeautifulSoup(requests.get(url, timeout=15).text, "html.parser")
    for a in soup.find_all("a", href=True):
        if a["href"].lower().endswith((".pdf", ".doc", ".docx")):
            yield a["href"] if a["href"].startswith("http") else requests.compat.urljoin(url, a["href"])

def extract_meta(path):
    with pdfplumber.open(path) as pdf:
        txt = pdf.pages[0].extract_text()[:1500]
    date_pat = re.compile(r"(?:Issued|Posted):\s*(\d{1,2}\s*\w+\s*\d{4})", re.I)
    ddl_pat  = re.compile(r"(?:Deadline|Due):\s*(\d{1,2}\s*\w+\s*\d{4})", re.I)
    return {
        "posted":   date_pat.search(txt).group(1) if date_pat.search(txt) else "n/a",
        "deadline": ddl_pat.search(txt).group(1)  if ddl_pat.search(txt) else "n/a",
        "snippet":  " ".join(txt.splitlines()[:5])
    }

def main():
    out = []
    seen = {p.stem for p in DATA.glob("*.pdf")}

    for tag, url in SITES.items():
        for link in doc_links(url):
            h = hashlib.sha1(link.encode()).hexdigest()
            if h in seen:
                continue
            pdf = DATA / f"{h}.pdf"
            pdf.write_bytes(requests.get(link, timeout=15).content)
            meta = extract_meta(pdf) | {"portal": tag, "source": link}
            out.append(meta)

    # Merge with old JSON (if any) so GPT keeps history
    store = pathlib.Path("latest_rfps.json")
    existing = json.loads(store.read_text()) if store.exists() else []
    store.write_text(json.dumps(out + existing, indent=2))

if __name__ == "__main__":
    main()
