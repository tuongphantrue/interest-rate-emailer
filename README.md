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

## Vietnam commercial banks (Vietcombank, Techcombank)

Because the national-average deposit rate above isn't the number most
people actually want, the email also has a second section with each
bank's own advertised **12-month VND savings rate** (the tenor Vietnamese
financial press typically uses when comparing banks).

This is a meaningfully heavier feature than everything else in this repo:

- **A headless browser is required.** Both banks' rate pages return
  genuinely empty table cells in their raw HTML — the actual numbers are
  populated by client-side JavaScript after the page loads. This was
  confirmed by fetching both pages directly: the raw HTML literally has no
  digits in it. Every other fetcher in this file uses a plain
  `requests.get()`; these two use [Playwright](https://playwright.dev) to
  render the page in a real headless Chromium instance first.
- **This adds real cost and time.** Every run now installs Playwright's
  Chromium binary (cached between runs via `actions/cache` so it doesn't
  re-download every time, but the browser itself still needs to launch,
  load two pages, and wait for their JS to settle) and takes noticeably
  longer than the rest of the script combined. Since the schedule runs
  every 30 minutes, this is worth being aware of against GitHub Actions'
  free-tier minutes.
- **Techcombank's fetcher is more likely to need a tweak.** Vietcombank's
  page is a straightforward rendered table (read the row labeled "12"),
  which is fairly stable. Techcombank's page renders as a set of product
  cards (Phat Loc Savings, Flexible Savings, etc.) rather than one simple
  table, so its fetcher does a best-effort text search for a 12-month
  figure instead of a fixed table position — more likely to break if
  Techcombank redesigns that page. If it starts reporting "unavailable",
  check `fetch_techcombank_rate()` in `interest_rate_emailer.py` against
  the site's current layout.
- **Both banks offer other terms, online-only rates, and promotions** not
  captured here (e.g. Vietcombank's site itself notes rates can run
  higher for online deposits or short-term promotions). The 12-month
  counter rate is a reasonable single number for comparison, not
  necessarily the *best* rate either bank currently offers.

If any single source fails to fetch, only that line notes the failure —
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
playwright install --with-deps chromium
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
