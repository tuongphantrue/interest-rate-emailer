"""
interest_rate_emailer.py

Fetches both the policy rate and the average commercial bank deposit rate
for six major economies and emails a summary. Designed to run on GitHub
Actions (see .github/workflows/send-interest-rate.yml) or locally via
cron. No local computer needs to stay on.

Why two rates per bank: a central bank's policy rate (e.g. SBV's 4.5%
refinancing rate) is what it charges commercial banks - it is NOT what
those banks pay savers or charge borrowers. Commercial deposit/lending
rates are set by each bank individually and are usually higher. Showing
both side by side avoids the "why does my bank offer 7-8% when the central
bank rate is 4.5%?" confusion.

Economies covered (each rendered as its own row, each rate degrades
independently if it fails to fetch on a given run):

1. United States - Fed Funds target rate via FRED API (free, needs a key); deposit rate via TradingEconomics
2. Euro Area - ECB main refinancing rate via ECB Statistical Data Warehouse API (no key); deposit rate via TradingEconomics
3. United Kingdom - BOE Bank Rate via BOE's public statistics page (no key); deposit rate via TradingEconomics
4. Japan - BOJ policy rate via TradingEconomics (BOJ's own site publishes decisions as PDFs with no parsable number)
5. China - PBOC Loan Prime Rate via TradingEconomics (PBOC's own English news index mixes unrelated headlines)
6. Vietnam - SBV refinancing rate via TradingEconomics (SBV's own portal is a noisy multi-widget page)

Both the policy rate and the deposit rate for Japan/China/Vietnam are read
from TradingEconomics, since it reports every country in the same
consistent plain-English sentence format - far more reliable than each
central bank's own differently-structured site.

Usage:
  python interest_rate_emailer.py generate   # fetch rates, build email body -> email_body.txt / email_body.html
  python interest_rate_emailer.py send       # send both bodies (plain text + styled HTML) via SMTP

Required environment variables (set as GitHub Actions secrets, or export locally):
  GMAIL_ADDRESS          - sender gmail address
  GMAIL_APP_PASSWORD     - Gmail App Password (not your normal password)
  INTEREST_RATE_RECIPIENT - recipient email address
  FRED_API_KEY           - free key from https://fred.stlouisfed.org/docs/api/api_key.html

Optional environment variables:
  SEND_ONLY_ON_CHANGE    - "true" to only email when a rate actually changed
                            since the last run (compares against last_rates.json)
"""

import os
import re
import sys
import json
import html
import time
import smtplib
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
from bs4 import BeautifulSoup

# --- Config -----------------------------------------------------------------

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def now_vn():
    """Current time in Vietnam (UTC+7), regardless of the runner's local timezone."""
    return datetime.now(VN_TZ)


FRED_API_KEY = os.environ.get("FRED_API_KEY", "")

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
INTEREST_RATE_RECIPIENT = os.environ.get("INTEREST_RATE_RECIPIENT")

SEND_ONLY_ON_CHANGE = os.environ.get("SEND_ONLY_ON_CHANGE", "false").lower() == "true"

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587

EMAIL_BODY_FILE = "email_body.txt"
EMAIL_HTML_FILE = "email_body.html"
STATE_FILE = "last_rates.json"

# Sent with every scrape request. Several central bank sites block the bare
# default "python-requests/x.y" User-Agent, so we look like an ordinary browser.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json",
}

SOURCES = [
    ("US Federal Reserve", "https://fred.stlouisfed.org/series/DFEDTARU"),
    ("European Central Bank", "https://data.ecb.europa.eu/data/datasets/FM"),
    ("Bank of England", "https://www.bankofengland.co.uk/monetary-policy/the-interest-rate-bank-rate"),
    ("Bank of Japan", "https://tradingeconomics.com/japan/interest-rate"),
    ("People's Bank of China", "https://tradingeconomics.com/china/interest-rate"),
    ("State Bank of Vietnam", "https://tradingeconomics.com/vietnam/interest-rate"),
]

# Deposit rate = what commercial banks actually pay savers, as opposed to
# the policy rate above (what the central bank charges commercial banks).
# All six are read from TradingEconomics for the same consistent-format
# reason the Japan/China/Vietnam policy rates are.
DEPOSIT_SLUGS = {
    "US Federal Reserve": "united-states",
    "European Central Bank": "euro-area",
    "Bank of England": "united-kingdom",
    "Bank of Japan": "japan",
    "People's Bank of China": "china",
    "State Bank of Vietnam": "vietnam",
}

DEPOSIT_SOURCES = [
    (name, f"https://tradingeconomics.com/{slug}/deposit-interest-rate")
    for name, slug in DEPOSIT_SLUGS.items()
]

# --- Scrape helpers ----------------------------------------------------------


def fix_encoding(resp):
    """requests defaults to ISO-8859-1 when a server doesn't send a charset
    header, which mangles UTF-8 pages into mojibake. Re-detect if needed.
    """
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding
    return resp


# --- Fetch --------------------------------------------------------------------


def fetch_fed_rate():
    """US Fed Funds target rate (upper bound) via FRED series DFEDTARU."""
    if not FRED_API_KEY:
        raise RuntimeError("FRED_API_KEY not set")
    url = (
        "https://api.stlouisfed.org/fred/series/observations"
        f"?series_id=DFEDTARU&api_key={FRED_API_KEY}&file_type=json&sort_order=desc&limit=1"
    )
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    obs = resp.json()["observations"][0]
    return {"rate": f"{obs['value']}%", "as_of": obs["date"]}


def fetch_ecb_rate():
    """ECB main refinancing rate via the ECB Statistical Data Warehouse API."""
    url = (
        "https://data-api.ecb.europa.eu/service/data/FM/"
        "D.U2.EUR.4F.KR.MRR_FR.LEV?lastNObservations=1&format=jsondata"
    )
    resp = requests.get(url, headers={**HEADERS, "Accept": "application/json"}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    all_series = data["dataSets"][0]["series"]
    # Don't assume the series key - ECB sometimes returns a different
    # dimension combination than "0:0:0:0:0:0" depending on what's live.
    series_key = next(iter(all_series))
    series = all_series[series_key]["observations"]
    latest_key = sorted(series.keys(), key=int)[-1]
    value = series[latest_key][0]
    date = data["structure"]["dimensions"]["observation"][0]["values"][int(latest_key)]["id"]
    return {"rate": f"{value}%", "as_of": date}


def fetch_boe_rate():
    """Bank of England Bank Rate, scraped from their public rate table."""
    url = "https://www.bankofengland.co.uk/boeapps/database/Bank-Rate.asp"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table")
    if not table:
        raise RuntimeError("Rate table not found - page markup may have changed")
    first_row = table.find_all("tr")[1]
    cells = [c.get_text(strip=True) for c in first_row.find_all("td")]
    date, value = cells[0], cells[1]
    return {"rate": f"{value}%", "as_of": date}


def fetch_te_rate(country_slug):
    """Fetches a country's benchmark rate from TradingEconomics, which
    reports it as one consistent plain-English sentence across every
    country page (unlike BOJ/PBOC/SBV's own sites, which are either
    multi-table PDF listings, mixed news feeds, or noisy nav-heavy portals).

    Looks for: "The benchmark interest rate in <Country> was last recorded
    at X percent." plus the reference month from the indicators table.
    """
    url = f"https://tradingeconomics.com/{country_slug}/interest-rate"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    fix_encoding(resp)
    soup = BeautifulSoup(resp.text, "html.parser")
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    rate_match = re.search(r"was last recorded at ([\d.]+)\s*percent", text, re.I)
    if not rate_match:
        raise RuntimeError("Rate sentence not found - page markup may have changed")

    as_of_match = re.search(
        r"Interest Rate\s+[\d.]+\s+[\d.]+\s+percent\s+([A-Za-z]{3,9}\.?\s+\d{4})", text
    )
    as_of = as_of_match.group(1) if as_of_match else now_vn().strftime("%Y-%m-%d")

    return {"rate": f"{rate_match.group(1)}%", "as_of": as_of}


def fetch_te_deposit_rate(country_slug):
    """Fetches a country's average commercial bank deposit rate from
    TradingEconomics - a different figure from the central bank's own
    policy rate above. This is what commercial banks actually pay savers,
    which is why it's often higher than the policy rate (e.g. Vietnam's
    SBV refinancing rate sits at 4.5% while banks advertise 6-8% deposit
    rates - the policy rate isn't what banks pay you).

    TradingEconomics phrases this a few different ways depending on the
    country ("increased to X percent in <month/year>", "remained unchanged
    at X percent in <year>", etc) so the regex matches any of them, then
    falls back to the indicators table row for the date if the headline
    sentence didn't include one.
    """
    url = f"https://tradingeconomics.com/{country_slug}/deposit-interest-rate"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    fix_encoding(resp)
    soup = BeautifulSoup(resp.text, "html.parser")
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    rate_match = re.search(
        r"Deposit (?:Interest Rate|Facility Rate).{0,40}?"
        r"(?:remained unchanged at|increased to|decreased to|rose to|fell to|stood at|was)\s+"
        r"([\d.]+)\s*percent(?:\s+in\s+([A-Za-z]+\.?\s*\d{4}|\d{4}))?",
        text, re.I,
    )
    if not rate_match:
        raise RuntimeError("Rate sentence not found - page markup may have changed")

    as_of = rate_match.group(2)
    if not as_of:
        table_match = re.search(
            r"Deposit (?:Interest Rate|Facility Rate)\s+[\d.]+\s+[\d.]+\s+percent\s+([A-Za-z]{3,9}\.?\s+\d{4})",
            text,
        )
        as_of = table_match.group(1) if table_match else now_vn().strftime("%Y-%m-%d")

    return {"rate": f"{rate_match.group(1)}%", "as_of": as_of}


def fetch_boj_rate():
    """Bank of Japan policy rate, via TradingEconomics (BOJ's own decisions
    are published as PDFs with no numeric rate in the surrounding page text,
    so the official site can't be scraped for a clean number - see README)."""
    return fetch_te_rate("japan")


def fetch_pboc_rate():
    """PBOC Loan Prime Rate, via TradingEconomics (PBOC's own English news
    index mixes unrelated headlines with rate releases and isn't reliably
    scrapable for just the rate - see README)."""
    return fetch_te_rate("china")


def fetch_sbv_rate():
    """State Bank of Vietnam refinancing rate, via TradingEconomics (SBV's
    own portal is a noisy multi-widget page that doesn't reliably surface
    just the rate figure - see README)."""
    return fetch_te_rate("vietnam")


FETCHERS = [
    ("US Federal Reserve", fetch_fed_rate),
    ("European Central Bank", fetch_ecb_rate),
    ("Bank of England", fetch_boe_rate),
    ("Bank of Japan", fetch_boj_rate),
    ("People's Bank of China", fetch_pboc_rate),
    ("State Bank of Vietnam", fetch_sbv_rate),
]

# --- Commercial bank fetchers -------------------------------------------------
#
# All ten banks below are read from 24hmoney.vn's per-bank rate page rather
# Vietcombank and Techcombank read from each bank's own official site
# (restored from earlier in this project - both patterns were already
# built and verified against real content). The other 8 banks below still
# read from 24hmoney.vn for now - see the status note further down for
# why, and what each would need to move to its own official site too.

COMMERCIAL_BANK_SOURCES = [
    ("Vietcombank", "https://www.vietcombank.com.vn/en/Personal/Cong-cu-Tien-ich/KHCN---Lai-suat"),
    ("Techcombank", "https://techcombank.com/en/tools-utilities/interest-rates"),
    ("BIDV", "https://bidv.com.vn/vn/ca-nhan/cong-cu-tien-ich/lai-suat"),
    ("VietinBank", "https://www.vietinbank.vn/lai-suat-khcn"),
    ("Sacombank", "https://www.sacombank.com.vn/content/dam/sacombank/files/cong-cu/lai-suat/tien-gui/khcn/SACOMBANK_LAISUATNIEMYETTAIQUAY_KHCN_VIE.pdf"),
    ("ACB", "https://acb.com.vn/en/interest-rate"),
    ("HDBank", "https://hdbank.com.vn/vi/personal/cong-cu/interest-rate"),
    ("TPBank", "https://tpb.vn/cn-tiet-kiem-tiet-kiem-thuong-linh-lai-dinh-ky"),
    ("MB Bank", "https://www.mbbank.com.vn/Fee"),
    ("VPBank", "https://www.vpbank.com.vn/ca-nhan/tiet-kiem"),
]


def diagnostic_snippet(soup, around_pattern=r"12"):
    """Grabs a short snippet of the rendered page's visible text, centered
    on the first occurrence of around_pattern if found, otherwise from the
    start. Included in error messages below so a failure shows what the
    page actually contained instead of just "not found" - turning the next
    debugging round into "read this snippet" instead of another screenshot.
    """
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    match = re.search(around_pattern, text)
    start = max(0, (match.start() - 80 if match else 0))
    return text[start:start + 300]


def render_js_page(url, wait_selector=None, goto_timeout_ms=20000, selector_timeout_ms=25000,
                    settle_ms=3000, attempts=3):
    """Loads a page with a headless Chromium browser and returns the fully
    rendered HTML, for official bank pages whose content is populated by
    client-side JS. Uses "domcontentloaded" rather than "networkidle"/"load"
    (both of which wait for every page resource - analytics, trackers,
    etc. - which caused real timeouts in practice), then waits specifically
    for the content selector we need. Retries with HTTP/2 disabled after
    the first attempt, since some banking-site WAFs respond to automated
    traffic with a raw connection error under HTTP/2 specifically.
    """
    from playwright.sync_api import sync_playwright

    last_error = None
    for attempt in range(attempts):
        args = ["--no-sandbox"]
        if attempt > 0:
            args.append("--disable-http2")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=args)
                try:
                    page = browser.new_page(
                        user_agent=HEADERS["User-Agent"],
                        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                    )
                    page.goto(url, wait_until="domcontentloaded", timeout=goto_timeout_ms)
                    if wait_selector:
                        page.wait_for_selector(wait_selector, timeout=selector_timeout_ms)
                    page.wait_for_timeout(settle_ms)
                    return page.content()
                finally:
                    browser.close()
        except Exception as e:
            last_error = e
            if attempt < attempts - 1:
                time.sleep(5 * (attempt + 1))
    raise last_error


def fetch_vietcombank_official_rates():
    """Vietcombank's own official rate table, for every term listed.

    Vietcombank's rate page is populated by client-side JS (confirmed
    empty in the raw HTML), so this renders it with a headless browser
    first. The page shows one VND rate per term rather than a separate
    counter/online split - Vietnamese financial press has independently
    confirmed Vietcombank deliberately keeps its online rate equivalent to
    its counter rate ("gửi tiết kiệm trực tuyến ở mức tương đương"), so
    both columns use this same figure rather than one being a guess.
    """
    rendered_html = render_js_page(
        "https://www.vietcombank.com.vn/en/Personal/Cong-cu-Tien-ich/KHCN---Lai-suat",
        wait_selector="table",
    )
    soup = BeautifulSoup(rendered_html, "html.parser")
    table = soup.find("table")
    if not table:
        raise RuntimeError(
            f"Rate table not found after page render. Page text sample: {diagnostic_snippet(soup)!r}"
        )

    terms = []
    for row in table.find_all("tr"):
        cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
        if len(cells) < 2 or not re.search(r"\d", cells[0]):
            continue
        term_label, vnd_rate = cells[0], cells[1]
        if not vnd_rate.endswith("%"):
            vnd_rate += "%"
        terms.append({"term": term_label, "counter": vnd_rate, "online": vnd_rate})

    if not terms:
        raise RuntimeError(f"No term rows parsed. Page text sample: {diagnostic_snippet(soup)!r}")
    return {"as_of": now_vn().strftime("%Y-%m-%d"), "terms": terms}


def fetch_techcombank_official_rates():
    """Techcombank's own official rate-sheet PDF, for every term listed in
    both the "Phat Loc Savings at Counter" and "Phat Loc Online Savings"
    sections (Normal Customer / under-3-billion-VND tier - representative
    for a typical individual saver, not Techcombank's highest available
    rate at larger balances or Private/Priority tiers).

    Techcombank's interest-rates hub page is a landing page, not a table -
    the actual numbers live in a PDF whose filename has a date stamp that
    changes whenever Techcombank updates it, so the link is discovered
    fresh each run rather than hardcoded. Finding that link still needs a
    headless browser (the download link itself is JS-populated); reading
    the PDF itself just needs pypdf, no browser.
    """
    import io
    from urllib.parse import urljoin
    from pypdf import PdfReader

    hub_url = "https://techcombank.com/en/tools-utilities/interest-rates"
    rendered_html = render_js_page(hub_url, wait_selector="a[href*='lai-suat-tien-gui-tiet-kiem']")
    soup = BeautifulSoup(rendered_html, "html.parser")

    pdf_link = soup.find("a", href=re.compile(r"lai-suat-tien-gui-tiet-kiem.*\.pdf", re.I))
    if not pdf_link:
        raise RuntimeError(
            f"Rate-sheet PDF link not found on hub page. Page text sample: {diagnostic_snippet(soup, r'pdf')!r}"
        )
    pdf_url = urljoin(hub_url, pdf_link["href"])

    pdf_resp = requests.get(pdf_url, headers=HEADERS, timeout=20)
    pdf_resp.raise_for_status()
    reader = PdfReader(io.BytesIO(pdf_resp.content))
    pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
    pdf_text = re.sub(r"[ \t]+", " ", pdf_text)

    def extract_section_terms(section_pattern):
        section_match = re.search(section_pattern, pdf_text, re.I | re.S)
        if not section_match:
            return {}
        section = section_match.group(0)
        rows = {}
        # Each term row is "<n>M" followed by 12 decimal figures (4
        # customer tiers x 3 balance tiers); the last one is Normal
        # Customer / smallest balance.
        for term_match in re.finditer(r"\b(\d{1,2}M)\b\s*((?:\d\.\d{2}\s*){12})", section):
            term_label = term_match.group(1)
            values = term_match.group(2).split()
            rows[term_label] = f"{values[-1]}%"
        return rows

    counter_rates = extract_section_terms(r"PHAT LOC SAVINGS(?: AT COUNTER)?.*?(?=TIỀN GỬI|FLEXIBLE SAVINGS|$)")
    online_rates = extract_section_terms(r"PHAT LOC ONLINE SAVINGS.*?(?=FLEXIBLE SAVINGS|$)")
    if not counter_rates and not online_rates:
        raise RuntimeError(
            f"No term rows parsed from PDF {pdf_url}. Text sample: {pdf_text[:300]!r}"
        )

    term_order = list(counter_rates.keys())
    for t in online_rates:
        if t not in term_order:
            term_order.append(t)
    terms = [
        {"term": t, "counter": counter_rates.get(t, "-"), "online": online_rates.get(t, "-")}
        for t in term_order
    ]

    as_of_match = re.search(r"Effective from ([A-Za-z]+ \d{1,2}\s*,?\s*\d{4})", pdf_text)
    as_of = as_of_match.group(1) if as_of_match else now_vn().strftime("%Y-%m-%d")
    return {"as_of": as_of, "terms": terms}


def _parse_rate_table(table):
    """Parses a 24hmoney.vn rate table into {term_label: rate_string}."""
    rates = {}
    for row in table.find_all("tr"):
        cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
        if len(cells) < 2 or not re.search(r"\d", cells[0]):
            continue  # skip header row ("Kỳ hạn" / "Lãi suất")
        term, rate = cells[0], cells[1]
        if not rate.endswith("%"):
            rate += "%"
        rates[term] = rate
    return rates


def fetch_bank_all_rates(bank_slug):
    """Fetches every term/rate combination for a bank from 24hmoney.vn's
    per-bank rate page - confirmed server-rendered (no JavaScript needed)
    and using one consistent table format across every bank tracked here.
    Used for the 8 banks that haven't been individually moved to their own
    official site yet (see BANK_SLUGS / status note).

    Returns both the "tại Quầy" (at counter - walk-in) and "Trực tuyến"
    (online, usually noticeably higher - that gap is each bank's incentive
    to get you using the app instead of a branch) rates for every term the
    bank lists, rather than a single headline figure.

    Known limitation (confirmed in practice, not theoretical): this
    aggregator's comparison table can lag behind a bank's real published
    rate during active rate-hike/cut periods, even though 24hmoney's own
    news coverage is current - the table and the newsroom don't update in
    lockstep. Treat this as a same-week ballpark, not a live number.
    """
    url = f"https://24hmoney.vn/lai-suat-gui-ngan-hang/{bank_slug}"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    fix_encoding(resp)
    soup = BeautifulSoup(resp.text, "html.parser")

    online_heading = soup.find(
        lambda tag: tag.name in ("h2", "h3") and re.search(r"[Tt]rực tuyến|[Oo]nline", tag.get_text())
    )
    counter_heading = soup.find(
        lambda tag: tag.name in ("h2", "h3") and "Quầy" in tag.get_text()
    )
    counter_table = counter_heading.find_next("table") if counter_heading else None
    online_table = online_heading.find_next("table") if online_heading else None

    if not counter_table and not online_table:
        fallback = soup.find("table")
        if not fallback:
            raise RuntimeError(f"No rate table found. Page text sample: {diagnostic_snippet(soup)!r}")
        counter_table = fallback

    counter_rates = _parse_rate_table(counter_table) if counter_table else {}
    online_rates = _parse_rate_table(online_table) if online_table else {}
    if not counter_rates and not online_rates:
        raise RuntimeError(f"Tables found but no term rows parsed. Page text sample: {diagnostic_snippet(soup)!r}")

    as_of_match = re.search(r"th[aá]ng\s+(\d{2}/\d{4})", soup.get_text())
    as_of = as_of_match.group(1) if as_of_match else now_vn().strftime("%Y-%m-%d")

    term_order = list(counter_rates.keys())
    for term in online_rates:
        if term not in term_order:
            term_order.append(term)

    terms = [
        {"term": term, "counter": counter_rates.get(term, "-"), "online": online_rates.get(term, "-")}
        for term in term_order
    ]
    return {"as_of": as_of, "terms": terms}


def fetch_bidv_official_rates():
    """BIDV's own official rate table, for every term listed.

    BIDV's rate page is a real interactive form, not just a JS-rendered
    table: the raw HTML contains template placeholders
    ("{{vm.convertFormatNumber(item.VND)}}%") rather than numbers, and a
    region dropdown ("Chọn khu vực": Hà Nội / Hồ Chí Minh) has to be set
    and "Tìm kiếm" (Search) clicked before the table populates. This is a
    step up in fragility from Vietcombank's fetcher above (which only
    needs the page to render, not a form filled in) - if BIDV changes this
    form's structure, this is the fetcher most likely to need attention.

    Uses Hà Nội as the region (BIDV's page defaults to showing rates by
    region; Hà Nội is used here as a single representative choice since
    rates don't typically vary much by region for standard products).
    """
    from playwright.sync_api import sync_playwright

    url = "https://bidv.com.vn/vn/ca-nhan/cong-cu-tien-ich/lai-suat"
    last_error = None
    for attempt in range(3):
        args = ["--no-sandbox"]
        if attempt > 0:
            args.append("--disable-http2")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=args)
                try:
                    page = browser.new_page(
                        user_agent=HEADERS["User-Agent"],
                        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                    )
                    page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    page.get_by_text("Hà Nội", exact=False).first.click(timeout=10000)
                    page.get_by_text("Tìm kiếm", exact=False).first.click(timeout=10000)
                    page.wait_for_timeout(3000)
                    rendered_html = page.content()
                finally:
                    browser.close()
            break
        except Exception as e:
            last_error = e
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
            else:
                raise last_error

    soup = BeautifulSoup(rendered_html, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        raise RuntimeError(f"No rate table found after form submit. Page text sample: {diagnostic_snippet(soup)!r}")

    terms = []
    for table in tables:
        for row in table.find_all("tr"):
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 4 or not re.search(r"\d", cells[0]):
                continue
            if "{{" in " ".join(cells):
                continue  # unrendered template row - form submit didn't take effect
            term_label = cells[0]
            vnd_rate = cells[3] if len(cells) > 3 else cells[-1]
            if not re.search(r"\d", vnd_rate):
                continue
            if not vnd_rate.endswith("%"):
                vnd_rate += "%"
            terms.append({"term": term_label, "counter": vnd_rate, "online": vnd_rate})

    if not terms:
        raise RuntimeError(
            f"Table(s) found but still showed template placeholders (form submit likely didn't "
            f"take effect). Page text sample: {diagnostic_snippet(soup)!r}"
        )
    return {"as_of": now_vn().strftime("%Y-%m-%d"), "terms": terms}


def fetch_vietinbank_official_rates():
    """VietinBank's own official rate table, for every term listed.

    Lower confidence than the fetchers above: VietinBank's rate page
    (vietinbank.vn/lai-suat-khcn) returned a 405 error on a direct fetch
    attempt during development - a strong signal of active bot-protection
    (a WAF rejecting non-browser-like requests), which a simple GET can't
    get past. This could not be inspected for its real DOM structure the
    way BIDV's page was, so there was no confirmed button/dropdown text to
    target - guessing at Vietnamese UI labels without having seen the page
    would be pure speculation, so this doesn't attempt any interaction.

    Instead, this just renders the page with a real (headless) browser -
    which presents a genuine browser fingerprint and may get past
    protection that blocked the plain HTTP fetch - and reads whatever
    table is present without clicking anything, on the chance the page
    shows a default region/view on load. If VietinBank's page turns out
    to strictly require an interaction to show any data, this will fail
    with a diagnostic error rather than silently returning nothing -
    check that error message against the page's actual current layout if
    it does.
    """
    rendered_html = render_js_page(
        "https://www.vietinbank.vn/lai-suat-khcn",
        wait_selector="table",
    )
    soup = BeautifulSoup(rendered_html, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        raise RuntimeError(
            f"No rate table found after page render (page may require an interaction this "
            f"fetcher doesn't perform - see docstring). Page text sample: {diagnostic_snippet(soup)!r}"
        )

    terms = []
    for table in tables:
        for row in table.find_all("tr"):
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 2 or not re.search(r"\d", cells[0]):
                continue
            if "{{" in " ".join(cells):
                continue
            term_label = cells[0]
            rate = next((c for c in cells[1:] if re.search(r"\d", c)), None)
            if not rate:
                continue
            if not rate.endswith("%"):
                rate += "%"
            terms.append({"term": term_label, "counter": rate, "online": rate})

    if not terms:
        raise RuntimeError(
            f"Table(s) found but no usable rate rows parsed. Page text sample: {diagnostic_snippet(soup)!r}"
        )
    return {"as_of": now_vn().strftime("%Y-%m-%d"), "terms": terms}


def fetch_sacombank_official_rates():
    """Sacombank's own official rate-sheet PDF, for every term listed in
    both the counter and online sections.

    Unlike Techcombank's equivalent PDF, this one has a stable filename
    with no date stamp, so it can be fetched directly with no discovery
    step (no headless browser needed at all, for either finding the link
    or reading the data).

    Sacombank's table is considerably more granular than Techcombank's:
    each term has separate rates for THREE balance tiers AND up to four
    interest-payment methods (at maturity / quarterly / monthly / paid
    upfront), and - unusually - the number of columns varies by row (short
    terms don't have all four payment methods). Rather than assume a fixed
    column position like the Techcombank fetcher does, this takes the
    FIRST percentage figure after each term label, which is consistently
    the smallest balance tier's "paid at maturity" rate regardless of how
    many additional columns that particular row happens to have.
    """
    url = "https://www.sacombank.com.vn/content/dam/sacombank/files/cong-cu/lai-suat/tien-gui/khcn/SACOMBANK_LAISUATNIEMYETTAIQUAY_KHCN_VIE.pdf"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()

    import io
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(resp.content))
    pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
    pdf_text = re.sub(r"[ \t]+", " ", pdf_text)

    def extract_section_terms(section_pattern):
        section_match = re.search(section_pattern, pdf_text, re.I | re.S)
        if not section_match:
            return {}
        section = section_match.group(0)
        rows = {}
        for term_match in re.finditer(
            r"((?:Dưới \d+|Từ \d+ đến dưới \d+|\d+)\s*tháng)\s*\n?\s*(\d+\.\d+)%",
            section,
        ):
            term_label, rate = term_match.group(1).strip(), term_match.group(2)
            rows.setdefault(term_label, f"{rate}%")
        return rows

    counter_rates = extract_section_terms(
        r"TIẾT KIỆM CÓ KỲ HẠN TRUYỀN THỐNG.*?(?=II\.|TIỀN GỬI CÓ KỲ HẠN TRỰC TUYẾN)"
    )
    online_rates = extract_section_terms(r"TIỀN GỬI CÓ KỲ HẠN TRỰC TUYẾN.*?(?=III\.|B\.|$)")
    if not counter_rates and not online_rates:
        raise RuntimeError(f"No term rows parsed from PDF. Text sample: {pdf_text[:300]!r}")

    term_order = list(counter_rates.keys())
    for t in online_rates:
        if t not in term_order:
            term_order.append(t)
    terms = [
        {"term": t, "counter": counter_rates.get(t, "-"), "online": online_rates.get(t, "-")}
        for t in term_order
    ]

    as_of_match = re.search(r"Hiệu lực từ.*?ngày ([\d.]+)", pdf_text)
    as_of = as_of_match.group(1) if as_of_match else now_vn().strftime("%Y-%m-%d")
    return {"as_of": as_of, "terms": terms}


def fetch_acb_official_rates():
    """ACB's own official rate table, for every term listed.

    Medium confidence, same caveat as the VietinBank fetcher above: ACB's
    page is a Next.js app where navigation content renders but the actual
    rate table appeared cut off in a direct fetch (consistent with the
    table being populated by a client-side data call after initial page
    load, the same pattern Vietcombank's page has - but unlike
    Vietcombank, this wasn't confirmed against a fully-rendered version of
    the page, since only the pre-render HTML could be inspected during
    development). A dedicated PDF for individual-customer rates (as
    opposed to the business-customer one, which was found and isn't the
    right one) could not be located either. No specific interaction is
    attempted since none was confirmed necessary or identified. If this
    reports "unavailable", check the diagnostic snippet in the error
    against the page's current layout.
    """
    rendered_html = render_js_page(
        "https://acb.com.vn/en/interest-rate",
        wait_selector="table",
    )
    soup = BeautifulSoup(rendered_html, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        raise RuntimeError(
            f"No rate table found after page render. Page text sample: {diagnostic_snippet(soup)!r}"
        )

    terms = []
    for table in tables:
        for row in table.find_all("tr"):
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 2 or not re.search(r"\d", cells[0]):
                continue
            if "{{" in " ".join(cells):
                continue
            term_label = cells[0]
            rate = next((c for c in cells[1:] if re.search(r"\d", c)), None)
            if not rate:
                continue
            if not rate.endswith("%"):
                rate += "%"
            terms.append({"term": term_label, "counter": rate, "online": rate})

    if not terms:
        raise RuntimeError(
            f"Table(s) found but no usable rate rows parsed. Page text sample: {diagnostic_snippet(soup)!r}"
        )
    return {"as_of": now_vn().strftime("%Y-%m-%d"), "terms": terms}


def fetch_hdbank_official_rates():
    """HDBank's own official rate-sheet PDF, for every term listed in both
    the counter and online sections.

    Like Techcombank's PDF, HDBank's filename has a date/timestamp that
    changes whenever they update it, so the current link is discovered
    from their rate-info hub page each run rather than hardcoded (that
    page still needs a headless browser; reading the PDF itself just
    needs pypdf). Like Sacombank's PDF, HDBank's table has multiple named
    rate tiers per term (not a fixed, uniform column count), so this uses
    the same robust "first percentage after the term label" extraction
    rather than assuming a fixed column position.
    """
    import io
    from urllib.parse import urljoin
    from pypdf import PdfReader

    hub_url = "https://hdbank.com.vn/vi/personal/cong-cu/interest-rate"
    rendered_html = render_js_page(hub_url, wait_selector="a[href*='.pdf']")
    soup = BeautifulSoup(rendered_html, "html.parser")

    pdf_link = soup.find("a", href=re.compile(r"BIEULAISUATTIENGUIKHACHHANGCANHAN.*\.pdf", re.I))
    if not pdf_link:
        pdf_link = soup.find("a", href=re.compile(r"\.pdf", re.I))
    if not pdf_link:
        raise RuntimeError(
            f"Rate-sheet PDF link not found on hub page. Page text sample: {diagnostic_snippet(soup, r'pdf')!r}"
        )
    pdf_url = urljoin(hub_url, pdf_link["href"])

    pdf_resp = requests.get(pdf_url, headers=HEADERS, timeout=20)
    pdf_resp.raise_for_status()
    reader = PdfReader(io.BytesIO(pdf_resp.content))
    pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
    pdf_text = re.sub(r"[ \t]+", " ", pdf_text)

    def extract_section_terms(section_pattern):
        section_match = re.search(section_pattern, pdf_text, re.I | re.S)
        if not section_match:
            return {}
        section = section_match.group(0)
        rows = {}
        for term_match in re.finditer(
            r"((?:Không kỳ hạn|Dưới \d+|Từ \d+ đến dưới \d+|\d+)\s*tháng)\s*\n?\s*(\d+\.\d+)%",
            section,
        ):
            term_label, rate = term_match.group(1).strip(), term_match.group(2)
            rows.setdefault(term_label, f"{rate}%")
        return rows

    counter_rates = extract_section_terms(r"(?:tại quầy|TẠI QUẦY).*?(?=trực tuyến|TRỰC TUYẾN|$)")
    online_rates = extract_section_terms(r"(?:trực tuyến|TRỰC TUYẾN).*?$")
    if not counter_rates and not online_rates:
        raise RuntimeError(f"No term rows parsed from PDF {pdf_url}. Text sample: {pdf_text[:300]!r}")

    term_order = list(counter_rates.keys())
    for t in online_rates:
        if t not in term_order:
            term_order.append(t)
    terms = [
        {"term": t, "counter": counter_rates.get(t, "-"), "online": online_rates.get(t, "-")}
        for t in term_order
    ]
    return {"as_of": now_vn().strftime("%Y-%m-%d"), "terms": terms}


def fetch_tpbank_official_rates():
    """TPBank's own official rate-sheet PDF, for every term listed in both
    the "TIẾT KIỆM TẠI QUẦY" (at counter) and "TIẾT KIỆM KÊNH NGÂN HÀNG SỐ"
    (digital banking channel = online) sections.

    Like Techcombank's and HDBank's PDFs, TPBank's is hosted at a
    content-ID-based URL that changes with each update (confirmed: two
    different observed URLs at different dates), so the current link is
    discovered from a product page each run rather than hardcoded.
    """
    import io
    from urllib.parse import urljoin
    from pypdf import PdfReader

    hub_url = "https://tpb.vn/cn-tiet-kiem-tiet-kiem-thuong-linh-lai-dinh-ky"
    rendered_html = render_js_page(hub_url, wait_selector="a[href*='.pdf']")
    soup = BeautifulSoup(rendered_html, "html.parser")

    pdf_link = soup.find("a", href=re.compile(r"BieuLaiSuat.*\.pdf", re.I))
    if not pdf_link:
        pdf_link = soup.find("a", href=re.compile(r"\.pdf", re.I))
    if not pdf_link:
        raise RuntimeError(
            f"Rate-sheet PDF link not found on hub page. Page text sample: {diagnostic_snippet(soup, r'pdf')!r}"
        )
    pdf_url = urljoin(hub_url, pdf_link["href"])

    pdf_resp = requests.get(pdf_url, headers=HEADERS, timeout=20)
    pdf_resp.raise_for_status()
    reader = PdfReader(io.BytesIO(pdf_resp.content))
    pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
    pdf_text = re.sub(r"[ \t]+", " ", pdf_text)

    def extract_section_terms(section_pattern):
        section_match = re.search(section_pattern, pdf_text, re.I | re.S)
        if not section_match:
            return {}
        section = section_match.group(0)
        rows = {}
        for term_match in re.finditer(
            r"((?:Không kỳ hạn|Dưới \d+|Từ \d+ đến dưới \d+|\d+)\s*tháng)\s*\n?\s*(\d+\.\d+)%",
            section,
        ):
            term_label, rate = term_match.group(1).strip(), term_match.group(2)
            rows.setdefault(term_label, f"{rate}%")
        return rows

    counter_rates = extract_section_terms(r"TIẾT KIỆM TẠI QUẦY.*?(?=TIẾT KIỆM KÊNH NGÂN HÀNG SỐ|$)")
    online_rates = extract_section_terms(r"TIẾT KIỆM KÊNH NGÂN HÀNG SỐ.*?(?=LÃI SUẤT CÁC LOẠI NGOẠI TỆ|$)")
    if not counter_rates and not online_rates:
        raise RuntimeError(f"No term rows parsed from PDF {pdf_url}. Text sample: {pdf_text[:300]!r}")

    term_order = list(counter_rates.keys())
    for t in online_rates:
        if t not in term_order:
            term_order.append(t)
    terms = [
        {"term": t, "counter": counter_rates.get(t, "-"), "online": online_rates.get(t, "-")}
        for t in term_order
    ]
    return {"as_of": now_vn().strftime("%Y-%m-%d"), "terms": terms}


def _fetch_generic_official_table(url):
    """Shared low-confidence fallback for banks where no clean static page
    or PDF could be found (used by MB Bank and VPBank below): renders the
    page with a headless browser and reads whatever table is present,
    without any specific interaction (none was confirmed necessary or
    identified for either bank). No promises this actually works - if it
    reports "unavailable", check the diagnostic snippet in the error
    against the page's current layout, or consider it may need the kind
    of interaction BIDV's fetcher performs, which would need its own
    investigation to identify correctly.
    """
    rendered_html = render_js_page(url, wait_selector="table")
    soup = BeautifulSoup(rendered_html, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        raise RuntimeError(
            f"No rate table found after page render. Page text sample: {diagnostic_snippet(soup)!r}"
        )

    terms = []
    for table in tables:
        for row in table.find_all("tr"):
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 2 or not re.search(r"\d", cells[0]):
                continue
            if "{{" in " ".join(cells):
                continue
            term_label = cells[0]
            rate = next((c for c in cells[1:] if re.search(r"\d", c)), None)
            if not rate:
                continue
            if not rate.endswith("%"):
                rate += "%"
            terms.append({"term": term_label, "counter": rate, "online": rate})

    if not terms:
        raise RuntimeError(
            f"Table(s) found but no usable rate rows parsed. Page text sample: {diagnostic_snippet(soup)!r}"
        )
    return {"as_of": now_vn().strftime("%Y-%m-%d"), "terms": terms}


def fetch_mb_official_rates():
    """MB Bank's own official rate table - lowest confidence of any
    fetcher in this file. During development, MB Bank's page turned out
    to be almost entirely unrendered template placeholders even in its
    navigation menu (not just the rate table), a step beyond what
    VietinBank or ACB showed. No dedicated PDF or simpler alternative page
    could be found either. Kept as a real attempt rather than skipped
    outright, but genuinely likely to just report "unavailable" - that's
    an honest outcome given what's known about this page, not a bug.
    """
    return _fetch_generic_official_table("https://www.mbbank.com.vn/Fee")


def fetch_vpbank_official_rates():
    """VPBank's own official rate table - similarly low confidence. No
    dedicated PDF or simple static rate page could be found (news
    coverage references "the link below" for VPBank's official rate sheet
    without the actual URL being discoverable independently). Worth
    knowing separately: VPBank's own rate and "Cake by VPBank" (their
    digital-only sub-brand) are different things with different rates,
    the same kind of distinction as the Certificate of Deposit products
    in the Special Products section - Cake's rate is not what this
    fetcher is after, even though it's easy to find and higher.
    """
    return _fetch_generic_official_table("https://www.vpbank.com.vn/ca-nhan/tiet-kiem")


COMMERCIAL_BANK_FETCHERS = [
    ("Vietcombank", fetch_vietcombank_official_rates),
    ("Techcombank", fetch_techcombank_official_rates),
    ("BIDV", fetch_bidv_official_rates),
    ("VietinBank", fetch_vietinbank_official_rates),
    ("Sacombank", fetch_sacombank_official_rates),
    ("ACB", fetch_acb_official_rates),
    ("HDBank", fetch_hdbank_official_rates),
    ("TPBank", fetch_tpbank_official_rates),
    ("MB Bank", fetch_mb_official_rates),
    ("VPBank", fetch_vpbank_official_rates),
]


# --- Special products ---------------------------------------------------------
#
# Certificates of deposit are a fundamentally different product from the
# regular savings tables above: bond-like (fixed term, no early
# withdrawal, but transferable/usable as loan collateral), sold in
# periodic limited-scale issuances rather than an always-open account, and
# priced noticeably higher than regular savings for the same bank. Both
# this and a bank's regular savings rate can be simultaneously accurate -
# they're just different products, which is why this gets its own section
# rather than folding into the terms tables above.
#
# Both Vietcombank's and Techcombank's are tracked here, since both were
# specifically asked about and both have a stable, server-rendered
# official product page with a clean "up to X%" headline (confirmed by
# fetching each directly). Other banks may or may not run an equivalent
# product, and each would need its own page checked the same way these
# two were (see README) before adding it.

SPECIAL_PRODUCT_SOURCES = [
    ("Vietcombank Certificate of Deposit",
     "https://www.vietcombank.com.vn/vi-VN/KHCN/SPDV/Dau-tu/Chung-chi-tien-gui-truc-tuyen"),
    ("Techcombank Bao Loc Certificate of Deposit",
     "https://techcombank.com/en/personal/save/certificate-of-deposit"),
]


def fetch_vcb_certificate_of_deposit_rate():
    """Vietcombank's Certificate of Deposit ("Chứng chỉ tiền gửi trực
    tuyến") headline rate. Sold only through the VCB Digibank app in
    periodic limited issuances - this is NOT the same product as the
    regular Vietcombank savings row in the commercial banks section above.

    Vietcombank's product page for this is a stable, permanent URL that
    they update with the current issuance's headline rate each time,
    rather than a dated news article (which a script has no reliable way
    to discover new instances of on its own). Confirmed server-rendered -
    the rate is present in the raw HTML.

    Takes the MAXIMUM of all matches rather than the first: responsive
    pages often embed more than one copy of the same promotional text
    (e.g. a separate mobile-layout block hidden via CSS, which
    BeautifulSoup still sees since it doesn't execute CSS/JS) - if one
    copy is stale, taking the max is more robust than trusting whichever
    happened to appear first in the raw HTML.
    """
    url = "https://www.vietcombank.com.vn/vi-VN/KHCN/SPDV/Dau-tu/Chung-chi-tien-gui-truc-tuyen"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    fix_encoding(resp)
    soup = BeautifulSoup(resp.text, "html.parser")
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    matches = re.findall(r"[Ll]ãi suất.{0,20}?đến\s*(\d+[,.]?\d*)\s*%\s*/\s*năm", text)
    if not matches:
        raise RuntimeError(
            f"Headline rate not found - product may be between issuances. "
            f"Page text sample: {diagnostic_snippet(soup, r'%')!r}"
        )
    rate = max(matches, key=lambda m: float(m.replace(",", "."))).replace(",", ".")
    return {"rate": f"{rate}%", "as_of": now_vn().strftime("%Y-%m-%d")}


def fetch_tcb_certificate_of_deposit_rate():
    """Techcombank's Bao Loc Certificate of Deposit ("Chứng chỉ tiền gửi
    Bảo Lộc") headline rate - Techcombank's equivalent to Vietcombank's
    product above, though structured differently: priced by exact holding
    period (days/months) rather than Vietcombank's fixed 6/9/12-month
    terms, and it's an always-open product rather than periodic limited
    issuances. Sold through Techcombank Mobile.

    Techcombank's product page states a headline "up to X% for 3M holding"
    figure, confirmed present in the raw HTML (server-rendered, same as
    Vietcombank's page above) - this is the rate for a 3-month hold,
    Techcombank's own headline comparison point, not necessarily the
    maximum rate available at longer holding periods.

    Takes the MAXIMUM of all matches rather than the first, for the same
    duplicate/hidden-content-block reason as the Vietcombank fetcher above.
    """
    url = "https://techcombank.com/en/personal/save/certificate-of-deposit"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    fix_encoding(resp)
    soup = BeautifulSoup(resp.text, "html.parser")
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    matches = re.findall(r"profit up to\s*(\d+(?:\.\d+)?)\s*%\s*/\s*year", text, re.I)
    if not matches:
        raise RuntimeError(
            f"Headline rate not found - page markup may have changed. "
            f"Page text sample: {diagnostic_snippet(soup, r'%')!r}"
        )
    rate = max(matches, key=float)
    return {"rate": f"{rate}%", "as_of": now_vn().strftime("%Y-%m-%d")}


SPECIAL_PRODUCT_FETCHERS = [
    ("Vietcombank Certificate of Deposit", fetch_vcb_certificate_of_deposit_rate),
    ("Techcombank Bao Loc Certificate of Deposit", fetch_tcb_certificate_of_deposit_rate),
]


# --- State (for change detection) --------------------------------------------


def load_previous_rates():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE) as f:
            content = f.read().strip()
        if not content:
            return None
        return json.loads(content)
    except json.JSONDecodeError:
        print(f"{STATE_FILE} exists but isn't valid JSON, treating as no previous state.")
        return None


def save_rates(results):
    snapshot = {"central_banks": {}, "commercial_banks": {}, "special_products": {}}
    for name, r in results.get("central_banks", {}).items():
        entry = {}
        if r["policy"].get("ok"):
            entry["policy"] = r["policy"]["rate"]
        if r["deposit"].get("ok"):
            entry["deposit"] = r["deposit"]["rate"]
        if entry:
            snapshot["central_banks"][name] = entry
    for name, r in results.get("commercial_banks", {}).items():
        if r.get("ok"):
            snapshot["commercial_banks"][name] = r["terms"]
    for name, r in results.get("special_products", {}).items():
        if r.get("ok"):
            snapshot["special_products"][name] = r["rate"]
    with open(STATE_FILE, "w") as f:
        json.dump(snapshot, f)


def prev_entry(previous_rates, name):
    """Backward-compat across two prior state-file formats: the oldest
    stored a flat rate string per bank ({name: "4.5%"}); the version right
    before this one stored {name: {"policy": ..., "deposit": ...}} at the
    top level (no central/commercial split). Both are handled below so
    neither crashes - old-format entries are treated as "no previous data"
    for that bank rather than guessed at.
    """
    if not previous_rates:
        return {}
    section = previous_rates.get("central_banks")
    if isinstance(section, dict) and isinstance(section.get(name), dict):
        return section[name]
    prev = previous_rates.get(name)
    return prev if isinstance(prev, dict) else {}


def prev_commercial_terms(previous_rates, name):
    """Returns the previous run's list of {term, counter, online} dicts for
    a commercial bank, or None if there's no comparable previous data
    (including from the single-rate-per-bank format used before this
    version, which can't be compared term-for-term).
    """
    if not previous_rates:
        return None
    section = previous_rates.get("commercial_banks")
    if not isinstance(section, dict):
        return None
    prev = section.get(name)
    return prev if isinstance(prev, list) else None


def prev_special_rate(previous_rates, name):
    if not previous_rates:
        return None
    section = previous_rates.get("special_products")
    return section.get(name) if isinstance(section, dict) else None


def find_prev_term(prev_terms, term_label):
    if not prev_terms:
        return None
    for pt in prev_terms:
        if pt.get("term") == term_label:
            return pt
    return None


def has_changed(results, previous_rates):
    if previous_rates is None:
        return True
    for name, r in results.get("central_banks", {}).items():
        prev = prev_entry(previous_rates, name)
        if r["policy"].get("ok") and prev.get("policy") != r["policy"]["rate"]:
            return True
        if r["deposit"].get("ok") and prev.get("deposit") != r["deposit"]["rate"]:
            return True
    for name, r in results.get("commercial_banks", {}).items():
        if r.get("ok") and prev_commercial_terms(previous_rates, name) != r["terms"]:
            return True
    for name, r in results.get("special_products", {}).items():
        if r.get("ok") and prev_special_rate(previous_rates, name) != r["rate"]:
            return True
    return False


# --- Formatting ----------------------------------------------------------------


def _try_fetch(fetcher, name, label):
    try:
        data = fetcher()
        return {"ok": True, **data}
    except Exception as e:
        print(f"{name} {label} source failed ({e}), continuing without it.")
        traceback.print_exc()
        return {"ok": False, "error": str(e)}


def collect_rates():
    central_banks = {}
    for name, fetcher in FETCHERS:
        policy = _try_fetch(fetcher, name, "policy rate")
        slug = DEPOSIT_SLUGS.get(name)
        if slug:
            deposit = _try_fetch(lambda slug=slug: fetch_te_deposit_rate(slug), name, "deposit rate")
        else:
            deposit = {"ok": False, "error": "no deposit source configured"}
        central_banks[name] = {"policy": policy, "deposit": deposit}

    commercial_banks = {}
    for name, fetcher in COMMERCIAL_BANK_FETCHERS:
        commercial_banks[name] = _try_fetch(fetcher, name, "deposit rate")

    special_products = {}
    for name, fetcher in SPECIAL_PRODUCT_FETCHERS:
        special_products[name] = _try_fetch(fetcher, name, "special product rate")

    return {"central_banks": central_banks, "commercial_banks": commercial_banks, "special_products": special_products}


def is_stale_annual(as_of):
    """True when as_of is a bare year (e.g. "2023") more than a year old.
    Some countries' deposit rate (Vietnam included) only gets a World-Bank
    annual update rather than a monthly one, so a scrape can return a real,
    correctly-parsed figure that's nonetheless a couple of years old - this
    flags that so it isn't mistaken for a current number.
    """
    match = re.fullmatch(r"\d{4}", (as_of or "").strip())
    if not match:
        return False
    return int(match.group()) < now_vn().year - 1


def format_email_body(results, previous_rates):
    central_banks = results.get("central_banks", {})
    commercial_banks = results.get("commercial_banks", {})
    special_products = results.get("special_products", {})

    lines = [f"Central bank interest rates - {now_vn().strftime('%Y-%m-%d %H:%M')}\n"]
    lines.append(f"{'Central bank':<24} | {'Policy rate':<28} | {'Deposit rate'}")
    lines.append("-" * 95)

    def cell(d, prev_val):
        if d.get("ok"):
            s = f"{d['rate']} ({d['as_of']})"
            if is_stale_annual(d["as_of"]):
                s += " [annual figure, may be outdated]"
            if prev_val and prev_val != d["rate"]:
                s += f" [was {prev_val}]"
            return s
        return f"unavailable ({d.get('error', 'unknown error')})"

    for name, _url in SOURCES:
        r = central_banks.get(name, {"policy": {}, "deposit": {}})
        prev = prev_entry(previous_rates, name)
        policy_cell = cell(r.get("policy", {}), prev.get("policy"))
        deposit_cell = cell(r.get("deposit", {}), prev.get("deposit"))
        lines.append(f"{name:<24} | {policy_cell:<28} | {deposit_cell}")

    lines.append("")
    lines.append("Note: policy rate = what the central bank charges commercial banks.")
    lines.append("Deposit rate = average rate commercial banks pay savers - usually higher.")
    lines.append("Deposit rate is a national average (sometimes only updated annually), not")
    lines.append("any specific bank's current advertised rate, which may run higher or lower.")

    lines.append("")
    lines.append("Vietnam commercial banks - all terms, at-counter vs online (%/year)")
    lines.append("=" * 95)
    for name, _url in COMMERCIAL_BANK_SOURCES:
        r = commercial_banks.get(name, {})
        lines.append("")
        if not r.get("ok"):
            lines.append(f"{name} - unavailable ({r.get('error', 'unknown error')})")
            continue
        prev_terms = prev_commercial_terms(previous_rates, name)
        lines.append(f"{name} (as of {r['as_of']})")
        lines.append(f"  {'Term':<14} | {'At counter':<20} | {'Online'}")
        for t in r["terms"]:
            prev_t = find_prev_term(prev_terms, t["term"])
            counter_s = t["counter"]
            online_s = t["online"]
            if prev_t:
                if prev_t.get("counter") not in (None, t["counter"]):
                    counter_s += f" [was {prev_t['counter']}]"
                if prev_t.get("online") not in (None, t["online"]):
                    online_s += f" [was {prev_t['online']}]"
            lines.append(f"  {t['term']:<14} | {counter_s:<20} | {online_s}")

    lines.append("")
    lines.append("Special products (not regular savings - see note)")
    lines.append("=" * 95)
    lines.append("Note: certificates of deposit are a different product from regular savings -")
    lines.append("bond-like, fixed term, no early withdrawal, sold in limited issuances. Both")
    lines.append("this and a bank's regular savings rate above can be accurate at once.")
    for name, _url in SPECIAL_PRODUCT_SOURCES:
        r = special_products.get(name, {})
        prev_val = prev_special_rate(previous_rates, name)
        lines.append(f"{name:<40} | {cell(r, prev_val)}")

    lines.append("")
    lines.append("Policy rate sources:")
    for name, url in SOURCES:
        lines.append(f"  {name}: {url}")
    lines.append("")
    lines.append("Deposit rate sources:")
    for name, url in DEPOSIT_SOURCES:
        lines.append(f"  {name}: {url}")
    lines.append("")
    lines.append("Commercial bank sources:")
    for name, url in COMMERCIAL_BANK_SOURCES:
        lines.append(f"  {name}: {url}")
    lines.append("")
    lines.append("Special product sources:")
    for name, url in SPECIAL_PRODUCT_SOURCES:
        lines.append(f"  {name}: {url}")

    return "\n".join(lines)


FONT_STACK = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def format_email_html(results, previous_rates):
    """Email-client-safe HTML version of the summary. Gmail (and most other
    webmail clients) strip <style> blocks and CSS classes, so every style
    here is applied inline on the element itself, and layout uses nested
    <table>s rather than divs/flexbox - the standard approach for HTML email.

    Design system based on actual screenshots of Rework.com's app (a Sales
    CRM view): mostly white/neutral, with color used sparingly as accents
    rather than large solid-color bands - a colored left border and small
    tinted icon chip per section, dark titles (not colored), and color
    reserved for the data itself (bold, large rate figures - matching how
    their "$130,000" deal value is bold and green while everything around
    it stays neutral).
    """
    esc = html.escape
    today = now_vn().strftime("%A, %B %d %Y - %H:%M")

    # --- Palette (named, not incidental) ---
    INK = "#111827"
    SLATE = "#6B7280"
    CANVAS = "#F8F9FB"
    WHITE = "#FFFFFF"
    BORDER = "#E5E7EB"
    INDIGO, INDIGO_TINT = "#4F46E5", "#EEF2FF"
    EMERALD, EMERALD_TINT = "#059669", "#ECFDF5"
    AMBER, AMBER_TINT = "#D97706", "#FFFBEB"
    CHANGED_BG, CHANGED_FG = "#FEF3C7", "#92400E"
    ERROR_BG, ERROR_FG = "#FEE2E2", "#991B1B"
    STALE_BG, STALE_FG = "#E5E7EB", "#475569"

    def badge(text, bg, fg):
        return (
            f'<span style="display:inline-block;margin-top:6px;font-size:11.5px;'
            f'color:{fg};background:{bg};padding:3px 10px;border-radius:999px;'
            f'font-weight:600;font-family:{FONT_STACK};">{text}</span>'
        )

    def section_header(icon, title, subtitle, accent, tint):
        return f"""
        <tr>
          <td style="padding:18px 22px;border-bottom:1px solid {BORDER};">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td width="46" style="padding:0;">
                  <div style="width:34px;height:34px;border-radius:9px;background:{tint};
                              text-align:center;line-height:34px;font-size:16px;">{icon}</div>
                </td>
                <td style="padding:0 0 0 10px;">
                  <div style="font-family:{FONT_STACK};font-size:16px;font-weight:700;color:{INK};">{title}</div>
                  <div style="font-family:{FONT_STACK};font-size:12px;color:{SLATE};margin-top:2px;">{subtitle}</div>
                </td>
              </tr>
            </table>
          </td>
        </tr>"""

    def col_header(label, accent_text):
        return (
            f'<td style="padding:14px 20px 8px;font-family:{FONT_STACK};font-size:11px;'
            f'text-transform:uppercase;letter-spacing:0.05em;font-weight:700;color:{accent_text};'
            f'border-bottom:2px solid {BORDER};">{label}</td>'
        )

    def rate_cell(d, prev_val, border, accent):
        if d.get("ok"):
            change_badge = ""
            if prev_val and prev_val != d["rate"]:
                change_badge = "<br>" + badge(f"changed &middot; was {esc(prev_val)}", CHANGED_BG, CHANGED_FG)
            stale_badge = ""
            if is_stale_annual(d["as_of"]):
                stale_badge = "<br>" + badge("annual figure &middot; may be outdated", STALE_BG, STALE_FG)
            return f"""
              <td style="padding:14px 20px;{border}vertical-align:top;font-family:{FONT_STACK};">
                <span style="font-weight:800;font-size:26px;letter-spacing:-0.02em;color:{accent};">{esc(d['rate'])}</span>
                <div style="font-size:12px;color:{SLATE};margin-top:3px;">{esc(d.get('as_of',''))}</div>
                {stale_badge}{change_badge}
              </td>"""
        err = esc(d.get("error", "unknown error"))
        return f"""
              <td style="padding:14px 20px;{border}vertical-align:top;font-family:{FONT_STACK};">
                <span style="font-size:13px;color:#9CA3AF;font-style:italic;">Unavailable</span><br>
                {badge(err, ERROR_BG, ERROR_FG)}
              </td>"""

    central_banks = results.get("central_banks", {})
    commercial_banks = results.get("commercial_banks", {})
    special_products = results.get("special_products", {})

    rows_html = []
    for i, (name, _url) in enumerate(SOURCES):
        r = central_banks.get(name, {"policy": {}, "deposit": {}})
        prev = prev_entry(previous_rates, name)
        border = "" if i == len(SOURCES) - 1 else f"border-bottom:1px solid {BORDER};"
        row = f"""
            <tr>
              <td style="padding:14px 20px;{border}vertical-align:top;font-family:{FONT_STACK};
                         font-size:14px;font-weight:600;color:{INK};width:30%;">{esc(name)}</td>
              {rate_cell(r.get('policy', {}), prev.get('policy'), border, INDIGO)}
              {rate_cell(r.get('deposit', {}), prev.get('deposit'), border, INDIGO)}
            </tr>"""
        rows_html.append(row)

    def bank_block_html(name, r, is_last_bank):
        if not r.get("ok"):
            err = esc(r.get("error", "unknown error"))
            border = "" if is_last_bank else f"border-bottom:1px solid {BORDER};"
            return f"""
            <tr>
              <td colspan="3" style="padding:16px 20px;{border}font-family:{FONT_STACK};">
                <div style="font-size:14px;font-weight:700;color:{INK};margin-bottom:4px;">{esc(name)}</div>
                <span style="font-size:13px;color:#9CA3AF;font-style:italic;">Unavailable</span><br>
                {badge(err, ERROR_BG, ERROR_FG)}
              </td>
            </tr>"""

        prev_terms = prev_commercial_terms(previous_rates, name)
        rows = [f"""
            <tr>
              <td colspan="3" style="padding:16px 20px 4px;background:{EMERALD_TINT};font-family:{FONT_STACK};">
                <div style="font-size:14px;font-weight:700;color:{INK};">{esc(name)}</div>
                <div style="font-size:11px;color:{SLATE};">as of {esc(r['as_of'])}</div>
              </td>
            </tr>
            <tr>
              <td style="padding:6px 20px;background:{EMERALD_TINT};font-family:{FONT_STACK};font-size:11px;text-transform:uppercase;
                         letter-spacing:0.03em;font-weight:700;color:{EMERALD};border-bottom:1px solid {BORDER};">Term</td>
              <td style="padding:6px 20px;background:{EMERALD_TINT};font-family:{FONT_STACK};font-size:11px;text-transform:uppercase;
                         letter-spacing:0.03em;font-weight:700;color:{EMERALD};border-bottom:1px solid {BORDER};">At counter</td>
              <td style="padding:6px 20px;background:{EMERALD_TINT};font-family:{FONT_STACK};font-size:11px;text-transform:uppercase;
                         letter-spacing:0.03em;font-weight:700;color:{EMERALD};border-bottom:1px solid {BORDER};">Online</td>
            </tr>"""]

        for i, t in enumerate(r["terms"]):
            prev_t = find_prev_term(prev_terms, t["term"])
            last_row = is_last_bank and i == len(r["terms"]) - 1
            border = "" if last_row else f"border-bottom:1px solid {BORDER};"
            counter_badge = ""
            online_badge = ""
            if prev_t and prev_t.get("counter") not in (None, t["counter"]):
                counter_badge = "<br>" + badge(f"was {esc(prev_t['counter'])}", CHANGED_BG, CHANGED_FG)
            if prev_t and prev_t.get("online") not in (None, t["online"]):
                online_badge = "<br>" + badge(f"was {esc(prev_t['online'])}", CHANGED_BG, CHANGED_FG)
            rows.append(f"""
            <tr>
              <td style="padding:8px 20px;{border}font-family:{FONT_STACK};font-size:13px;color:{SLATE};">{esc(t['term'])}</td>
              <td style="padding:8px 20px;{border}font-family:{FONT_STACK};font-size:15px;font-weight:700;color:{INK};">{esc(t['counter'])}{counter_badge}</td>
              <td style="padding:8px 20px;{border}font-family:{FONT_STACK};font-size:15px;font-weight:700;color:{EMERALD};">{esc(t['online'])}{online_badge}</td>
            </tr>""")
        return "".join(rows)

    commercial_rows_html = [
        bank_block_html(name, commercial_banks.get(name, {}), i == len(COMMERCIAL_BANK_SOURCES) - 1)
        for i, (name, _url) in enumerate(COMMERCIAL_BANK_SOURCES)
    ]

    special_rows_html = []
    for i, (name, _url) in enumerate(SPECIAL_PRODUCT_SOURCES):
        r = special_products.get(name, {})
        prev_val = prev_special_rate(previous_rates, name)
        border = "" if i == len(SPECIAL_PRODUCT_SOURCES) - 1 else f"border-bottom:1px solid {BORDER};"
        special_rows_html.append(f"""
            <tr>
              <td style="padding:14px 20px;{border}vertical-align:top;font-family:{FONT_STACK};
                         font-size:14px;font-weight:600;color:{INK};width:40%;">{esc(name)}</td>
              {rate_cell(r, prev_val, border, AMBER)}
            </tr>""")

    def sources_block(title, source_list, accent):
        rows = "".join(
            f'<tr><td style="padding:3px 0;font-size:12px;color:{INK};font-family:{FONT_STACK};'
            f'white-space:nowrap;padding-right:12px;">{esc(name)}</td>'
            f'<td style="padding:3px 0;font-size:12px;font-family:{FONT_STACK};">'
            f'<a href="{esc(url)}" style="color:{accent};text-decoration:none;">{esc(url)}</a></td></tr>'
            for name, url in source_list
        )
        return f"""
            <div style="font-family:{FONT_STACK};font-size:11px;text-transform:uppercase;font-weight:700;
                        letter-spacing:0.05em;color:{SLATE};margin-bottom:8px;">{title}</div>
            <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:16px;">
              {rows}
            </table>"""

    return f"""\
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="background:{CANVAS};padding:24px 0;">
  <tr>
    <td align="center">
      <table role="presentation" width="640" cellpadding="0" cellspacing="0" border="0"
             style="max-width:640px;width:100%;background:{WHITE};border-radius:14px;
                    overflow:hidden;border:1px solid {BORDER};border-left:4px solid {INDIGO};">
        {section_header("&#127963;&#65039;", "Central Bank Interest Rates", esc(today) + " (Vietnam time)", INDIGO, INDIGO_TINT)}
        <tr>
          <td>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr>
                {col_header("Central bank", SLATE)}
                {col_header("Policy rate", INDIGO)}
                {col_header("Deposit rate", INDIGO)}
              </tr>
              {"".join(rows_html)}
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:4px 24px 0;">
            <div style="font-family:{FONT_STACK};font-size:12px;color:{SLATE};
                        background:{INDIGO_TINT};border-radius:10px;padding:12px 14px;margin-top:16px;">
              <strong style="color:{INK};">Policy rate</strong> = what the central bank charges
              commercial banks. <strong style="color:{INK};">Deposit rate</strong> = the average
              rate commercial banks pay savers - usually higher, and set independently by each bank.
              It's a national average (sometimes only updated annually, flagged below when so), not
              any one bank's current advertised rate, which may run higher or lower.
            </div>
          </td>
        </tr>
        <tr>
          <td style="padding:16px 24px 22px;">
            {sources_block("Policy rate sources", SOURCES, INDIGO)}
            {sources_block("Deposit rate sources", DEPOSIT_SOURCES, INDIGO)}
          </td>
        </tr>
      </table>

      <table role="presentation" width="640" cellpadding="0" cellspacing="0" border="0"
             style="max-width:640px;width:100%;background:{WHITE};border-radius:14px;
                    overflow:hidden;border:1px solid {BORDER};border-left:4px solid {EMERALD};margin-top:20px;">
        {section_header("&#127974;", "Vietnam Commercial Banks", "All terms &middot; at counter vs online (%/year)", EMERALD, EMERALD_TINT)}
        <tr>
          <td>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
              {"".join(commercial_rows_html)}
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:16px 24px 22px;">
            {sources_block("Sources", COMMERCIAL_BANK_SOURCES, EMERALD)}
          </td>
        </tr>
      </table>

      <table role="presentation" width="640" cellpadding="0" cellspacing="0" border="0"
             style="max-width:640px;width:100%;background:{WHITE};border-radius:14px;
                    overflow:hidden;border:1px solid {BORDER};border-left:4px solid {AMBER};margin-top:20px;">
        {section_header("&#128142;", "Special Products", "Not regular savings &middot; see note", AMBER, AMBER_TINT)}
        <tr>
          <td style="padding:16px 20px 4px;">
            <div style="font-family:{FONT_STACK};font-size:12px;color:{SLATE};
                        background:{AMBER_TINT};border-radius:10px;padding:12px 14px;">
              Certificates of deposit are a different product from regular savings -
              bond-like, fixed term, no early withdrawal, sold in limited issuances.
              Both this and a bank's regular savings rate above can be accurate at once.
            </div>
          </td>
        </tr>
        <tr>
          <td>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
              {"".join(special_rows_html)}
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:16px 24px 22px;">
            {sources_block("Sources", SPECIAL_PRODUCT_SOURCES, AMBER)}
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>"""


# --- Email ------------------------------------------------------------------


def send_email(text_body, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Interest Rate Summary - {now_vn().strftime('%Y-%m-%d %H:%M')}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = INTEREST_RATE_RECIPIENT
    # Attach plain text first, HTML second - email clients render the last
    # part that they support, so HTML wins in modern clients while plain
    # text still works as a fallback everywhere else.
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [INTEREST_RATE_RECIPIENT], msg.as_string())


# --- Commands -----------------------------------------------------------------


def cmd_generate():
    results = collect_rates()
    previous_rates = load_previous_rates()

    if SEND_ONLY_ON_CHANGE and not has_changed(results, previous_rates):
        print("No rate changes since last run, skipping email.")
        open(EMAIL_BODY_FILE, "w").close()
        open(EMAIL_HTML_FILE, "w").close()
        save_rates(results)
        return

    text_body = format_email_body(results, previous_rates)
    html_body = format_email_html(results, previous_rates)

    with open(EMAIL_BODY_FILE, "w") as f:
        f.write(text_body)
    with open(EMAIL_HTML_FILE, "w") as f:
        f.write(html_body)

    print(text_body)
    save_rates(results)


def cmd_send():
    if not (os.path.exists(EMAIL_BODY_FILE) and os.path.exists(EMAIL_HTML_FILE)):
        print("No email body found, run 'generate' first.")
        return

    with open(EMAIL_BODY_FILE) as f:
        text_body = f.read()
    with open(EMAIL_HTML_FILE) as f:
        html_body = f.read()

    if not text_body.strip():
        print("Email body empty, nothing to send.")
        return

    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and INTEREST_RATE_RECIPIENT):
        print("GMAIL_ADDRESS / GMAIL_APP_PASSWORD / INTEREST_RATE_RECIPIENT not set, skipping send.")
        return

    send_email(text_body, html_body)
    print("Email sent.")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "generate"
    if command == "generate":
        cmd_generate()
    elif command == "send":
        cmd_send()
    else:
        print(f"Unknown command: {command}. Use 'generate' or 'send'.")
