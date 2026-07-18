"""
interest_rate_emailer.py

Fetches current policy interest rates from six major central banks and
emails a summary. Designed to run on GitHub Actions (see
.github/workflows/send-interest-rate.yml) or locally via cron. No local
computer needs to stay on.

Central banks covered (each rendered as its own line, each degrades
gracefully if it fails on a given run):

1. US Federal Reserve - Fed Funds target rate, via FRED API (free, needs a key)
2. European Central Bank - main refinancing rate, via ECB Statistical Data Warehouse API (no key)
3. Bank of England - Bank Rate, via BOE's public statistics page (no key)
4. Bank of Japan - policy rate, scraped from boj.or.jp (no clean API published)
5. People's Bank of China - Loan Prime Rate, scraped from pbc.gov.cn (no clean API published)
6. State Bank of Vietnam - refinancing rate, scraped from sbv.gov.vn (no clean API published)

The three scraped sources (BOJ, PBOC, SBV) grab a best-effort headline/link
since none of the three publishes a clean English API - if a run reports
"unavailable", the site's markup probably changed; check the corresponding
fetch_* function below.

Usage:
  python interest_rate_emailer.py generate   # fetch rates, build email body -> email_body.txt
  python interest_rate_emailer.py send       # send email_body.txt via SMTP

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
import sys
import json
import smtplib
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo
from email.mime.text import MIMEText

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
    ("Bank of Japan", "https://www.boj.or.jp/en/mopo/mpmdeci/state_all/index.htm"),
    ("People's Bank of China", "http://www.pbc.gov.cn/english/130721/index.html"),
    ("State Bank of Vietnam", "https://www.sbv.gov.vn/webcenter/portal/en/home/rm/ir"),
]

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


def fetch_boj_rate():
    """Bank of Japan policy rate - best-effort scrape of the latest decision headline."""
    url = "https://www.boj.or.jp/en/mopo/mpmdeci/state_all/index.htm"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    latest = soup.find("a")
    if not latest:
        raise RuntimeError("No decision entries found - page markup may have changed")
    return {"rate": latest.get_text(strip=True), "as_of": now_vn().strftime("%Y-%m-%d")}


def fetch_pboc_rate():
    """PBOC Loan Prime Rate - best-effort scrape of the latest release headline."""
    url = "http://www.pbc.gov.cn/english/130721/index.html"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    latest = soup.find("a")
    if not latest:
        raise RuntimeError("No release entries found - page markup may have changed")
    return {"rate": latest.get_text(strip=True), "as_of": now_vn().strftime("%Y-%m-%d")}


def fetch_sbv_rate():
    """State Bank of Vietnam refinancing rate - best-effort scrape of the rates page."""
    url = "https://www.sbv.gov.vn/webcenter/portal/en/home/rm/ir"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    text = soup.get_text(" ", strip=True)[:200]
    if not text:
        raise RuntimeError("Page returned no readable text - markup may have changed")
    return {"rate": text, "as_of": now_vn().strftime("%Y-%m-%d")}


FETCHERS = [
    ("US Federal Reserve", fetch_fed_rate),
    ("European Central Bank", fetch_ecb_rate),
    ("Bank of England", fetch_boe_rate),
    ("Bank of Japan", fetch_boj_rate),
    ("People's Bank of China", fetch_pboc_rate),
    ("State Bank of Vietnam", fetch_sbv_rate),
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
    snapshot = {name: r["rate"] for name, r in results.items() if r.get("ok")}
    with open(STATE_FILE, "w") as f:
        json.dump(snapshot, f)


def has_changed(results, previous_rates):
    if previous_rates is None:
        return True
    for name, r in results.items():
        if not r.get("ok"):
            continue
        if previous_rates.get(name) != r["rate"]:
            return True
    return False


# --- Formatting ----------------------------------------------------------------


def collect_rates():
    results = {}
    for name, fetcher in FETCHERS:
        try:
            data = fetcher()
            results[name] = {"ok": True, "rate": data["rate"], "as_of": data["as_of"]}
        except Exception as e:
            print(f"{name} source failed ({e}), continuing without it.")
            traceback.print_exc()
            results[name] = {"ok": False, "error": str(e)}
    return results


def format_email_body(results, previous_rates):
    lines = [f"Central bank interest rates - {now_vn().strftime('%Y-%m-%d %H:%M')}\n"]
    lines.append(f"{'Central bank':<26}{'Rate':<28}{'As of'}")
    lines.append("-" * 70)

    for name, _url in SOURCES:
        r = results.get(name, {})
        if r.get("ok"):
            changed = ""
            if previous_rates and previous_rates.get(name) not in (None, r["rate"]):
                changed = f"  (was {previous_rates[name]})"
            lines.append(f"{name:<26}{r['rate']:<28}{r['as_of']}{changed}")
        else:
            lines.append(f"{name:<26}unavailable this run ({r.get('error', 'unknown error')})")

    lines.append("")
    lines.append("Sources:")
    for name, url in SOURCES:
        lines.append(f"  {name}: {url}")

    return "\n".join(lines)


# --- Email ------------------------------------------------------------------


def send_email(body):
    msg = MIMEText(body)
    msg["Subject"] = f"Interest Rate Summary - {now_vn().strftime('%Y-%m-%d %H:%M')}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = INTEREST_RATE_RECIPIENT

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
        save_rates(results)
        return

    body = format_email_body(results, previous_rates)
    with open(EMAIL_BODY_FILE, "w") as f:
        f.write(body)

    print(body)
    save_rates(results)


def cmd_send():
    if not os.path.exists(EMAIL_BODY_FILE):
        print("No email body found, run 'generate' first.")
        return

    with open(EMAIL_BODY_FILE) as f:
        body = f.read()

    if not body.strip():
        print("Email body empty, nothing to send.")
        return

    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and INTEREST_RATE_RECIPIENT):
        print("GMAIL_ADDRESS / GMAIL_APP_PASSWORD / INTEREST_RATE_RECIPIENT not set, skipping send.")
        return

    send_email(body)
    print("Email sent.")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "generate"
    if command == "generate":
        cmd_generate()
    elif command == "send":
        cmd_send()
    else:
        print(f"Unknown command: {command}. Use 'generate' or 'send'.")
