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

# Specific commercial banks, as opposed to the country-level averages above.
# Both banks' rate pages are populated by client-side JS (confirmed empty in
# the raw HTML), so these require a headless browser rather than a plain
# requests.get() - see fetch_vietcombank_rate/fetch_techcombank_rate below.
COMMERCIAL_BANK_SOURCES = [
    ("Vietcombank", "https://www.vietcombank.com.vn/en/Personal/Cong-cu-Tien-ich/KHCN---Lai-suat"),
    ("Techcombank", "https://techcombank.com/en/tools-utilities/interest-rates"),
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

# --- Commercial bank fetchers (headless browser) -----------------------------
#
# Both Vietcombank's and Techcombank's rate pages return genuinely empty
# table cells in the raw HTML response - their numbers are populated by
# client-side JavaScript after the page loads, confirmed by fetching both
# pages directly. A plain requests.get() (everything above this point)
# cannot see that data, so these two use Playwright to render the page in
# a real (headless) browser first, then parse the resulting HTML.
#
# This is meaningfully heavier than every other fetcher in this file: it
# needs the `playwright` package AND its browser binary installed (see
# requirements.txt and the "Install Playwright browser" workflow step),
# and each call takes several seconds instead of a fraction of one.


def render_js_page(url, wait_selector=None, timeout_ms=30000, settle_ms=3000):
    """Loads a page with a headless Chromium browser and returns the fully
    rendered HTML, for pages whose content is populated by client-side JS
    (confirmed necessary for both bank pages below - see module note above).
    `settle_ms` is extra idle time after "networkidle" for slow AJAX widgets
    that finish their network calls but still take a moment to paint.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            if wait_selector:
                page.wait_for_selector(wait_selector, timeout=timeout_ms)
            page.wait_for_timeout(settle_ms)
            return page.content()
        finally:
            browser.close()


def fetch_vietcombank_rate():
    """Vietcombank's 12-month VND savings rate - the tenor most commonly
    used when comparing Vietnamese banks (see the 12-month comparisons
    Vietnamese financial press runs every month). Renders the page with a
    headless browser, then reads the now-populated rate table for the row
    whose term is "12" and takes its VND column.
    """
    rendered_html = render_js_page(
        "https://www.vietcombank.com.vn/en/Personal/Cong-cu-Tien-ich/KHCN---Lai-suat",
        wait_selector="table",
    )
    soup = BeautifulSoup(rendered_html, "html.parser")
    table = soup.find("table")
    if not table:
        raise RuntimeError("Rate table not found after page render - markup may have changed")

    for row in table.find_all("tr"):
        cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
        if not cells:
            continue
        if re.search(r"\b12\b", cells[0]):
            for cell in cells[1:]:
                if re.search(r"\d", cell):
                    rate = cell if "%" in cell else f"{cell}%"
                    return {"rate": rate, "as_of": now_vn().strftime("%Y-%m-%d")}
    raise RuntimeError("12-month VND row not found in rendered table")


def fetch_techcombank_rate():
    """Techcombank's 12-month VND savings rate. Techcombank's page renders
    as product cards rather than one simple table (confirmed via direct
    fetch: the raw HTML has empty icon placeholders where the rates widget
    mounts), so this does a best-effort text search for a 12-month figure
    in the rendered page instead of a fixed table position. More likely
    than the Vietcombank fetcher to need a follow-up tweak once real output
    is visible from an actual run - flagging that rather than pretending
    otherwise.
    """
    rendered_html = render_js_page("https://techcombank.com/en/tools-utilities/interest-rates")
    soup = BeautifulSoup(rendered_html, "html.parser")
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    match = re.search(r"12[\s-]?months?[^%]{0,20}?(\d{1,2}(?:\.\d{1,2})?)\s?%", text, re.I)
    if not match:
        match = re.search(r"(\d{1,2}(?:\.\d{1,2})?)\s?%[^%]{0,20}?12[\s-]?months?", text, re.I)
    if not match:
        raise RuntimeError("12-month rate not found after page render - markup may have changed")
    return {"rate": f"{match.group(1)}%", "as_of": now_vn().strftime("%Y-%m-%d")}


COMMERCIAL_BANK_FETCHERS = [
    ("Vietcombank", fetch_vietcombank_rate),
    ("Techcombank", fetch_techcombank_rate),
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
    snapshot = {"central_banks": {}, "commercial_banks": {}}
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
            snapshot["commercial_banks"][name] = r["rate"]
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


def prev_commercial_rate(previous_rates, name):
    if not previous_rates:
        return None
    section = previous_rates.get("commercial_banks")
    return section.get(name) if isinstance(section, dict) else None


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
        if r.get("ok") and prev_commercial_rate(previous_rates, name) != r["rate"]:
            return True
    return False


# --- Formatting ----------------------------------------------------------------


def _try_fetch(fetcher, name, label):
    try:
        data = fetcher()
        return {"ok": True, "rate": data["rate"], "as_of": data["as_of"]}
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

    return {"central_banks": central_banks, "commercial_banks": commercial_banks}


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
    lines.append(f"Vietnam commercial banks - 12-month VND savings rate")
    lines.append("-" * 95)
    for name, _url in COMMERCIAL_BANK_SOURCES:
        r = commercial_banks.get(name, {})
        prev_val = prev_commercial_rate(previous_rates, name)
        lines.append(f"{name:<24} | {cell(r, prev_val)}")

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

    return "\n".join(lines)


FONT_STACK = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def format_email_html(results, previous_rates):
    """Email-client-safe HTML version of the summary. Gmail (and most other
    webmail clients) strip <style> blocks and CSS classes, so every style
    here is applied inline on the element itself, and layout uses nested
    <table>s rather than divs/flexbox - the standard approach for HTML email.
    """
    esc = html.escape
    today = now_vn().strftime("%A, %B %d %Y - %H:%M")

    def badge(text, bg, fg):
        return (
            f'<span style="display:inline-block;margin-top:6px;font-size:12px;'
            f'color:{fg};background:{bg};padding:2px 9px;border-radius:999px;'
            f'font-family:{FONT_STACK};">{text}</span>'
        )

    def rate_cell(d, prev_val, border):
        if d.get("ok"):
            change_badge = ""
            if prev_val and prev_val != d["rate"]:
                change_badge = "<br>" + badge(f"changed &middot; was {esc(prev_val)}", "#fef3c7", "#92400e")
            stale_badge = ""
            if is_stale_annual(d["as_of"]):
                stale_badge = "<br>" + badge("annual figure &middot; may be outdated", "#e5e7eb", "#4b5563")
            return f"""
              <td style="padding:14px 20px;{border}vertical-align:top;font-family:{FONT_STACK};font-size:14px;">
                <span style="font-weight:700;color:#111827;">{esc(d['rate'])}</span>
                <div style="font-size:12px;color:#6b7280;margin-top:2px;">{esc(d.get('as_of',''))}</div>
                {stale_badge}{change_badge}
              </td>"""
        err = esc(d.get("error", "unknown error"))
        return f"""
              <td style="padding:14px 20px;{border}vertical-align:top;font-family:{FONT_STACK};">
                <span style="font-size:13px;color:#9ca3af;font-style:italic;">Unavailable</span><br>
                {badge(err, "#fee2e2", "#991b1b")}
              </td>"""

    central_banks = results.get("central_banks", {})
    commercial_banks = results.get("commercial_banks", {})

    rows_html = []
    for i, (name, _url) in enumerate(SOURCES):
        r = central_banks.get(name, {"policy": {}, "deposit": {}})
        prev = prev_entry(previous_rates, name)
        border = "" if i == len(SOURCES) - 1 else "border-bottom:1px solid #f0f1f3;"
        row = f"""
            <tr>
              <td style="padding:14px 20px;{border}vertical-align:top;font-family:{FONT_STACK};
                         font-size:14px;font-weight:600;color:#111827;width:30%;">{esc(name)}</td>
              {rate_cell(r.get('policy', {}), prev.get('policy'), border)}
              {rate_cell(r.get('deposit', {}), prev.get('deposit'), border)}
            </tr>"""
        rows_html.append(row)

    commercial_rows_html = []
    for i, (name, _url) in enumerate(COMMERCIAL_BANK_SOURCES):
        r = commercial_banks.get(name, {})
        prev_val = prev_commercial_rate(previous_rates, name)
        border = "" if i == len(COMMERCIAL_BANK_SOURCES) - 1 else "border-bottom:1px solid #f0f1f3;"
        row = f"""
            <tr>
              <td style="padding:14px 20px;{border}vertical-align:top;font-family:{FONT_STACK};
                         font-size:14px;font-weight:600;color:#111827;width:30%;">{esc(name)}</td>
              {rate_cell(r, prev_val, border)}
            </tr>"""
        commercial_rows_html.append(row)

    def sources_block(title, source_list):
        rows = "".join(
            f'<tr><td style="padding:2px 0;font-size:12px;color:#374151;font-family:{FONT_STACK};'
            f'white-space:nowrap;padding-right:12px;">{esc(name)}</td>'
            f'<td style="padding:2px 0;font-size:12px;font-family:{FONT_STACK};">'
            f'<a href="{esc(url)}" style="color:#2563eb;text-decoration:none;">{esc(url)}</a></td></tr>'
            for name, url in source_list
        )
        return f"""
            <div style="font-family:{FONT_STACK};font-size:12px;text-transform:uppercase;
                        letter-spacing:0.04em;color:#6b7280;margin-bottom:8px;">{title}</div>
            <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:16px;">
              {rows}
            </table>"""

    return f"""\
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="background:#f4f5f7;padding:24px 0;">
  <tr>
    <td align="center">
      <table role="presentation" width="640" cellpadding="0" cellspacing="0" border="0"
             style="max-width:640px;width:100%;background:#ffffff;border-radius:12px;
                    overflow:hidden;border:1px solid #e5e7eb;">
        <tr>
          <td style="background:#1f2937;padding:20px 24px;">
            <div style="font-family:{FONT_STACK};font-size:18px;font-weight:700;color:#ffffff;">
              Central Bank Interest Rates
            </div>
            <div style="font-family:{FONT_STACK};font-size:13px;color:#9ca3af;margin-top:4px;">
              {esc(today)} (Vietnam time)
            </div>
          </td>
        </tr>
        <tr>
          <td>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td style="padding:14px 20px 8px;font-family:{FONT_STACK};font-size:12px;
                           text-transform:uppercase;letter-spacing:0.04em;color:#6b7280;
                           border-bottom:1px solid #e5e7eb;">Central bank</td>
                <td style="padding:14px 20px 8px;font-family:{FONT_STACK};font-size:12px;
                           text-transform:uppercase;letter-spacing:0.04em;color:#6b7280;
                           border-bottom:1px solid #e5e7eb;">Policy rate</td>
                <td style="padding:14px 20px 8px;font-family:{FONT_STACK};font-size:12px;
                           text-transform:uppercase;letter-spacing:0.04em;color:#6b7280;
                           border-bottom:1px solid #e5e7eb;">Deposit rate</td>
              </tr>
              {"".join(rows_html)}
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:4px 24px 0;">
            <div style="font-family:{FONT_STACK};font-size:12px;color:#6b7280;
                        background:#f9fafb;border-radius:8px;padding:10px 14px;margin-top:16px;">
              <strong style="color:#374151;">Policy rate</strong> = what the central bank charges
              commercial banks. <strong style="color:#374151;">Deposit rate</strong> = the average
              rate commercial banks pay savers - usually higher, and set independently by each bank.
              It's a national average (sometimes only updated annually, flagged below when so), not
              any one bank's current advertised rate, which may run higher or lower.
            </div>
          </td>
        </tr>
        <tr>
          <td style="padding:16px 24px 22px;">
            {sources_block("Policy rate sources", SOURCES)}
            {sources_block("Deposit rate sources", DEPOSIT_SOURCES)}
          </td>
        </tr>
      </table>
      <table role="presentation" width="640" cellpadding="0" cellspacing="0" border="0"
             style="max-width:640px;width:100%;background:#ffffff;border-radius:12px;
                    overflow:hidden;border:1px solid #e5e7eb;margin-top:20px;">
        <tr>
          <td style="background:#1f2937;padding:16px 24px;">
            <div style="font-family:{FONT_STACK};font-size:15px;font-weight:700;color:#ffffff;">
              Vietnam Commercial Banks
            </div>
            <div style="font-family:{FONT_STACK};font-size:12px;color:#9ca3af;margin-top:2px;">
              12-month VND savings rate
            </div>
          </td>
        </tr>
        <tr>
          <td>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
              {"".join(commercial_rows_html)}
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:16px 24px 22px;">
            {sources_block("Sources", COMMERCIAL_BANK_SOURCES)}
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
