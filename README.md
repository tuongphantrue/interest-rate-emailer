# Central Bank Interest Rates -> Email (runs on GitHub Actions, no local computer needed)

This repo emails you a daily summary of **both** the policy rate and the
average commercial bank deposit rate for six major economies,
automatically, using GitHub's free scheduled-workflow runners. Nothing
needs to run on your own machine.

**Why two rates:** a central bank's policy rate (e.g. SBV's 4.5%
refinancing rate) is what it charges *commercial banks* — it is not what
those banks pay you as a saver or charge you as a borrower. Commercial
deposit/lending rates are set independently by each bank based on market
conditions, and are usually higher (that's why you might see a bank
advertising 6-8% on a term deposit while the central bank's own rate sits
at 4.5% — both numbers are correct, they're just different things). The
email shows both side by side so that's clear at a glance.

## Rates tracked

| Central bank | Policy rate | Deposit rate | Source |
|---|---|---|---|
| US Federal Reserve | Fed Funds target rate | Avg. commercial deposit rate | FRED API (policy, needs a free key) + TradingEconomics (deposit) |
| European Central Bank | Main refinancing rate | Avg. commercial deposit rate | ECB Statistical Data Warehouse API (policy) + TradingEconomics (deposit) |
| Bank of England | Bank Rate | Avg. commercial deposit rate | BOE public statistics page (policy) + TradingEconomics (deposit) |
| Bank of Japan | Policy rate | Avg. commercial deposit rate | TradingEconomics (both, scraped) |
| People's Bank of China | Loan Prime Rate | Avg. commercial deposit rate | TradingEconomics (both, scraped) |
| State Bank of Vietnam | Refinancing rate | Avg. commercial deposit rate | TradingEconomics (both, scraped) |

The Fed, ECB, and BOE have clean official data feeds for their *policy*
rate and should stay reliable long-term. BOJ, PBOC, and SBV don't publish
a clean, scrapable English number on their own sites — BOJ's decisions are
PDFs with no rate in the surrounding page text, PBOC's English news index
mixes unrelated headlines in with rate releases, and SBV's site is a noisy
multi-widget portal. Those three (plus the *deposit* rate for all six
economies, since none of the official sources above publish that) are
instead read from [TradingEconomics](https://tradingeconomics.com), which
reports every country's rates in the same plain-English sentence format
regardless of country, which is far more reliably parsed than each site's
own differently-structured page. If one of them starts reporting
"unavailable", that sentence format probably changed; check
`fetch_te_rate()` / `fetch_te_deposit_rate()` in `interest_rate_emailer.py`.
TradingEconomics doesn't offer a free public API, hence the scrape — if
you'd rather use an authoritative source per bank, each one's official
site is still linked in the email footer.

Note that "average commercial deposit rate" is a broad national average
(often World-Bank-sourced and updated annually for smaller economies) —
it's a useful ballpark for "what banks generally pay," not a specific
promotional rate any one bank is currently advertising. A specific bank's
current term-deposit rate can run higher or lower than this average.

**Vietnam specifically**: TradingEconomics' deposit-rate figure for
Vietnam is stuck on a 2023 World Bank data point (4.78%) as of when this
was written — there's no more recent free, cleanly-scrapable figure for
it. If you've seen banks advertise 6-8% on VND term deposits, that's not
wrong; it's just a different, more current number than the stale national
average this repo can automate. The email flags any deposit rate that's a
bare annual figure more than a year old with an "annual figure, may be
outdated" badge, specifically so this one doesn't get mistaken for a
current number.

## Vietnam commercial banks (10 banks)

Because the national-average deposit rate above isn't the number most
people actually want, the email also has a second section with each
bank's own advertised **12-month VND savings rate** (the tenor Vietnamese
financial press typically uses when comparing banks), for:

Vietcombank, Techcombank, BIDV, VietinBank, MB Bank, ACB, VPBank,
Sacombank, HDBank, and TPBank.

**All ten are read from one source: [24hmoney.vn](https://24hmoney.vn)'s
per-bank rate page**, not each bank's own site. This wasn't the first
approach tried — Vietcombank's own rate page turned out to be populated by
client-side JavaScript (empty in the raw HTML), and Techcombank's
equivalent page is an interactive calculator with no static table at all,
both of which needed increasingly heavy workarounds (a headless browser,
then PDF-parsing) that were fragile and slow. 24hmoney.vn's page is plain
server-rendered HTML — the numbers are present in the raw HTTP response,
confirmed directly — with the *same* table format for every bank, so one
simple `requests.get()` (no browser, no PDF library) now covers all ten.

Each bank's page has two tables: "tại Quầy" (at the counter — the
standard, walk-in rate) and "Trực tuyến" (online, usually a bit higher).
The fetcher reads the counter table specifically, then the row labeled
"12" (months). If a bank's page layout changes and the row can't be
found, the error includes a snippet of what the page actually contained,
so a failure is diagnosable from the email itself rather than requiring
another round of guessing — check `fetch_bank_deposit_rate()` in
`interest_rate_emailer.py` against whatever that snippet shows.

Each bank also offers other terms, online-only promotions, and
balance-tiered rates not captured here — the 12-month counter rate is a
reasonable single number for comparison, not necessarily the *best* rate
any given bank currently offers (24hmoney.vn's own page, linked in the
email, has the full table per bank).

If any single bank fails to fetch, only that line notes the failure —
the rest of the email still generates and sends normally.

## One-time setup (~5 minutes)

1. **Create a GitHub account** if you don't have one: https://github.com/join

2. **Create a new repository**
   - Click "+" (top right) -> "New repository"
   - Name it anything, e.g. `interest-rate-emailer`
   - Set it to **Private** (recommended, keeps your workflow config private)
   - Click "Create repository"

3. **Upload these files** to the repo (drag-and-drop works fine via the
   GitHub web UI: "Add file" -> "Upload files"), keeping the folder structure:
   - `interest_rate_emailer.py`
   - `requirements.txt`
   - `.github/workflows/send-interest-rate.yml`

4. **Get a free FRED API key** (needed for the Fed rate):
   - https://fred.stlouisfed.org/docs/api/api_key.html

5. **Create a Gmail App Password** (your normal Gmail password won't work):
   - Turn on 2-Step Verification: https://myaccount.google.com/signinoptions/two-step-verification
   - Then create an app password: https://myaccount.google.com/apppasswords
   - Choose "Mail" as the app, copy the 16-character password it gives you.

6. **Add your secrets to the repo** (this keeps your email/password/key out of the code):
   - In your repo: Settings -> Secrets and variables -> Actions -> "New repository secret"
   - Add four secrets:
     - `GMAIL_ADDRESS` = your Gmail address
     - `GMAIL_APP_PASSWORD` = the 16-character app password from step 5
     - `INTEREST_RATE_RECIPIENT` = the email address that should receive the summary
     - `FRED_API_KEY` = the key from step 4

7. **Test it manually**
   - Go to the "Actions" tab in your repo
   - Click "Send Interest Rate Summary" on the left
   - Click "Run workflow" -> "Run workflow" (green button)
   - Wait ~15-20 seconds, refresh, click into the run to see logs / confirm success
   - Check the recipient inbox for the email

That's it — from now on it runs automatically on the schedule below, with
no computer of yours needing to be on.

## Schedule

```
- cron: "*/30 * * * *"
```

Runs every 30 minutes (cron is always in UTC), matching the cadence of the
other `*-emailer` repos. This line should not be changed.

## Only emailing on rate changes

By default the workflow sends an email on **every** scheduled run, whether
or not any rate has actually moved since last time. If you'd rather only
get emailed when a rate changes, open
`.github/workflows/send-interest-rate.yml`, find `SEND_ONLY_ON_CHANGE:
"false"` under the "Generate email" step, and change it to:

```
SEND_ONLY_ON_CHANGE: "true"
```

With that on, `generate` compares this run's rates against the last saved
snapshot — stored in `last_rates.json` on a dedicated
`interest-rate-state` branch the workflow creates/updates automatically —
and skips the email if nothing changed.

## Notes

- GitHub Actions free tier includes 2,000 minutes/month for private repos —
  this job takes a few seconds a run, so it's effectively free even at a
  daily cadence.
- You can also trigger it manually anytime via the "Run workflow" button.
- If a run fails, check the Actions tab -> the failed run -> logs. Common
  causes: a secret is missing/misspelled, the Gmail app password or FRED
  key was revoked, or a central bank site changed its page markup.

## Running locally instead

If you'd rather run this on your own machine instead of GitHub Actions:

```
pip install -r requirements.txt
export FRED_API_KEY="your_fred_key"
export GMAIL_ADDRESS="you@gmail.com"
export GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"
export INTEREST_RATE_RECIPIENT="you@gmail.com"
python interest_rate_emailer.py generate
python interest_rate_emailer.py send
```

Schedule it yourself with cron (`crontab -e`):

```
0 8 * * * cd /path/to/interest-rate-emailer && /usr/bin/python3 interest_rate_emailer.py generate && /usr/bin/python3 interest_rate_emailer.py send >> interest_rate_emailer.log 2>&1
```
