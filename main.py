"""Collect public notices (editais) and news from UFC-related websites.

Sources:
    - PROGEP/UFC editais: https://progep.ufc.br/pt/concursos-e-processo-seletivo/editais/
    - PROGRAD/UFC editais: https://prograd.ufc.br/pt/editais-e-resultados/
    - PRPPG/UFC editais: https://prppg.ufc.br/pt/editais/
    - DETI/UFC news: https://deti.ufc.br/pt/category/noticias/
    - PPGETI/UFC news: https://ppgeti.ufc.br/pt/category/noticias/
    - LESC: https://www.lesc-lab.com.br/
    - GTEL open calls: https://gtel.ufc.br/inscricoes-abertas/
    - FCPC Central de Concursos: https://centraldeconcursos.fcpc.ufc.br/

The WordPress sites (PROGEP, PROGRAD, DETI, PPGETI, LESC) are read through their RSS
feeds. PRPPG and GTEL keep their notices on a static page, which is parsed as HTML. The
FCPC site is a JavaScript app, so its public JSON API is queried directly.
"""

import argparse
import json
import ssl
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import date
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from xml.etree import ElementTree

import certifi
import requests
from bs4 import BeautifulSoup

PROGEP_EDITAIS_URL = "https://progep.ufc.br/pt/concursos-e-processo-seletivo/editais/"
PROGRAD_EDITAIS_FEED_URL = "https://prograd.ufc.br/pt/category/editais-e-resultados/feed/"
PRPPG_EDITAIS_URL = "https://prppg.ufc.br/pt/editais/"
DETI_NEWS_FEED_URL = "https://deti.ufc.br/pt/category/noticias/feed/"
PPGETI_NEWS_FEED_URL = "https://ppgeti.ufc.br/pt/category/noticias/feed/"
GTEL_OPEN_CALLS_URL = "https://gtel.ufc.br/inscricoes-abertas/"
LESC_FEED_URL = "https://www.lesc-lab.com.br/feed/"
FCPC_API_URL = "https://api.fcpc.adsr.com.br/candidate-area/selective-process/"
FCPC_PROCESS_PAGE_URL = "https://centraldeconcursos.fcpc.ufc.br/processo-seletivo/informacoes/{process_id}"

# The *.ufc.br servers send the wrong intermediate certificate, so their chain cannot be
# verified with a standard CA bundle. Browsers download the right one from the URL in the
# certificate's "Authority Information Access" field; we do the same and append it to
# certifi's bundle instead of disabling verification.
UFC_INTERMEDIATE_CERT_URL = "http://secure.globalsign.com/cacert/rnpicpedugr46ovtlsca2025.crt"

REQUEST_TIMEOUT_SECONDS = 30
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) web-scrap/0.1"


@dataclass
class Notice:
    source: str
    title: str
    link: str
    published: str  # ISO 8601 date, or "" when unknown
    summary: str


def build_ca_bundle_with_ufc_intermediate() -> str:
    """Return the path of a CA bundle made of certifi's roots plus the UFC intermediate."""
    response = requests.get(UFC_INTERMEDIATE_CERT_URL, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    intermediate_cert_pem = ssl.DER_cert_to_PEM_cert(response.content)

    with open(certifi.where(), encoding="ascii") as certifi_bundle:
        root_certs_pem = certifi_bundle.read()

    ca_bundle_file = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)
    with ca_bundle_file:
        ca_bundle_file.write(root_certs_pem + "\n" + intermediate_cert_pem)
    return ca_bundle_file.name


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    session.verify = build_ca_bundle_with_ufc_intermediate()
    return session


def html_to_text(html: str) -> str:
    text = BeautifulSoup(html, "html.parser").get_text(" ")
    return " ".join(text.split())


def fetch_rss_notices(session: requests.Session, feed_url: str, source: str) -> list[Notice]:
    response = session.get(feed_url, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    channel = ElementTree.fromstring(response.content).find("channel")

    notices = []
    for item in channel.iter("item"):
        publication_date = item.findtext("pubDate", "")
        notices.append(
            Notice(
                source=source,
                title=html_to_text(item.findtext("title", "")),
                link=item.findtext("link", "").strip(),
                published=parsedate_to_datetime(publication_date).date().isoformat() if publication_date else "",
                summary=html_to_text(item.findtext("description", "")),
            )
        )
    return notices


def collect_progep(session: requests.Session) -> list[Notice]:
    """Follow every PROGEP category listed on the editais page and read its feed."""
    response = session.get(PROGEP_EDITAIS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    page = BeautifulSoup(response.text, "html.parser")

    notices = []
    for category_link in page.select("main article ul li a[href*='/category/']"):
        category_url = category_link["href"]
        if urlparse(category_url).hostname != "progep.ufc.br":
            continue
        category_name = category_link.get_text(strip=True)
        feed_url = category_url.rstrip("/") + "/feed/"
        notices += fetch_rss_notices(session, feed_url, source=f"PROGEP - {category_name}")
    return notices


def collect_prograd(session: requests.Session) -> list[Notice]:
    return fetch_rss_notices(session, PROGRAD_EDITAIS_FEED_URL, source="PROGRAD")


def collect_prppg(session: requests.Session) -> list[Notice]:
    """Read the PRPPG editais table: one row per edital with its title, status and result link."""
    response = session.get(PRPPG_EDITAIS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    page = BeautifulSoup(response.text, "html.parser")

    notices = []
    for table_row in page.select("main article table tbody tr"):
        cells = table_row.find_all("td")
        title_link = cells[0].find("a") if cells else None
        if title_link is None:
            continue
        status = cells[1].get_text(strip=True) if len(cells) > 1 else ""
        result_links = ", ".join(
            f"{result_link.get_text(strip=True)}: {result_link['href']}"
            for result_link in (cells[2].select("a[href]") if len(cells) > 2 else [])
        )
        notices.append(
            Notice(
                source="PRPPG",
                title=title_link.get_text(" ", strip=True),
                link=title_link["href"],
                published="",
                summary=" | ".join(part for part in (status, result_links) if part),
            )
        )
    return notices


def collect_deti(session: requests.Session) -> list[Notice]:
    return fetch_rss_notices(session, DETI_NEWS_FEED_URL, source="DETI")


def collect_ppgeti(session: requests.Session) -> list[Notice]:
    return fetch_rss_notices(session, PPGETI_NEWS_FEED_URL, source="PPGETI")


def collect_gtel(session: requests.Session) -> list[Notice]:
    """Read the GTEL open-calls page: one notice per linked call, or the page text when there is none."""
    response = session.get(GTEL_OPEN_CALLS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    page = BeautifulSoup(response.text, "html.parser")
    page_content = page.select_one("#page-content .hentry")
    if page_content is None:
        raise ValueError(f"page content not found in {GTEL_OPEN_CALLS_URL}")

    notices = [
        Notice(source="GTEL", title=call_link.get_text(" ", strip=True), link=call_link["href"], published="", summary="")
        for call_link in page_content.select("a[href]")
        if call_link.get_text(strip=True)
    ]
    page_text = html_to_text(str(page_content))
    if not notices and page_text:
        notices.append(Notice(source="GTEL", title=page_text, link=GTEL_OPEN_CALLS_URL, published="", summary=""))
    return notices


def collect_lesc(session: requests.Session) -> list[Notice]:
    return fetch_rss_notices(session, LESC_FEED_URL, source="LESC")


def collect_fcpc(session: requests.Session) -> list[Notice]:
    response = session.get(
        FCPC_API_URL, params={"remove_pagination": "true"}, timeout=REQUEST_TIMEOUT_SECONDS
    )
    response.raise_for_status()

    notices = []
    for selective_process in response.json():
        situations = ", ".join(
            situation["name"] for situation in selective_process.get("selective_process_situation", [])
        )
        registrations_start = (selective_process.get("registrations_start") or {}).get("br", "?")
        registrations_end = (selective_process.get("registrations_end") or {}).get("br", "?")
        notices.append(
            Notice(
                source="FCPC",
                title=selective_process["name"],
                link=FCPC_PROCESS_PAGE_URL.format(process_id=selective_process["id"]),
                published=(selective_process.get("created_at") or {}).get("us", "")[:10],
                summary=(
                    f"{selective_process.get('type', '')} | {situations} | "
                    f"Inscrições: {registrations_start} a {registrations_end} | "
                    f"Vagas: {selective_process.get('number_of_vacancies', '?')}"
                ),
            )
        )
    notices.sort(key=lambda notice: notice.published, reverse=True)
    return notices


def is_from_year(notice: Notice, year: int) -> bool:
    """Tell whether a notice belongs to the given year.

    Dated notices are compared by their publication date. Undated ones (PRPPG, GTEL) are
    kept when the year appears in their title, link or summary, e.g. in the
    ".../wp-content/uploads/2026/05/edital.pdf" path of the edital file.
    """
    if notice.published:
        return notice.published.startswith(str(year))
    return str(year) in f"{notice.title} {notice.link} {notice.summary}"


COLLECTORS = {
    "progep": collect_progep,
    "prograd": collect_prograd,
    "prppg": collect_prppg,
    "deti": collect_deti,
    "ppgeti": collect_ppgeti,
    "lesc": collect_lesc,
    "gtel": collect_gtel,
    "fcpc": collect_fcpc,
}


def print_notices(notices: list[Notice], max_summary_length: int = 200) -> None:
    current_source = None
    for notice in notices:
        if notice.source != current_source:
            current_source = notice.source
            print(f"\n=== {current_source} ===")
        summary = notice.summary
        if len(summary) > max_summary_length:
            summary = summary[:max_summary_length].rstrip() + "..."
        print(f"\n[{notice.published or '----------'}] {notice.title}\n  {notice.link}\n  {summary}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--sources", nargs="+", choices=COLLECTORS, default=list(COLLECTORS), help="sites to collect (default: all)"
    )
    parser.add_argument(
        "--year", type=int, default=date.today().year, help="keep only notices from this year (default: current year)"
    )
    parser.add_argument("--all-years", action="store_true", help="keep notices from every year")
    parser.add_argument("--json", metavar="PATH", help="also save the results to a JSON file")
    arguments = parser.parse_args()

    session = create_session()
    all_notices: list[Notice] = []
    has_failed_source = False
    for source_name in arguments.sources:
        try:
            source_notices = COLLECTORS[source_name](session)
        except (requests.RequestException, ElementTree.ParseError, KeyError, ValueError) as error:
            print(f"[error] {source_name}: {error}", file=sys.stderr)
            has_failed_source = True
            continue
        if not arguments.all_years:
            source_notices = [notice for notice in source_notices if is_from_year(notice, arguments.year)]
        all_notices += source_notices

    print_notices(all_notices)

    if arguments.json:
        with open(arguments.json, "w", encoding="utf-8") as json_file:
            json.dump([asdict(notice) for notice in all_notices], json_file, ensure_ascii=False, indent=2)
        print(f"\nSaved {len(all_notices)} notices to {arguments.json}")

    sys.exit(1 if has_failed_source else 0)


if __name__ == "__main__":
    main()
