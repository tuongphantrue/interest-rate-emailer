# Central Bank Interest Rates -> Email (runs on GitHub Actions, no local computer needed)

This repo emails you a daily summary of policy interest rates from six
major central banks, automatically, using GitHub's free scheduled-workflow
runners. Nothing needs to run on your own machine.

## Central banks tracked

| Central bank | Rate | Source |
|---|---|---|
| US Federal Reserve | Fed Funds target rate | FRED API (needs a free key) |
| European Central Bank | Main refinancing rate | ECB Statistical Data Warehouse API |
| Bank of England | Bank Rate | BOE public statistics page |
| Bank of Japan | Policy rate | boj.or.jp (scraped, best-effort) |
| People's Bank of China | Loan Prime Rate | pbc.gov.cn (scraped, best-effort) |
| State Bank of Vietnam | Refinancing rate | sbv.gov.vn (scraped, best-effort) |

The Fed, ECB, and BOE have clean official data feeds and should stay
reliable long-term. BOJ, PBOC, and SBV don't publish a clean English API,
so those three grab a best-effort headline/link from their site instead —
if one of them starts reporting "unavailable", the site's markup probably
changed; check the matching `fetch_*` function in
`interest_rate_emailer.py`.

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

## Changing the schedule

Open `.github/workflows/send-interest-rate.yml` and edit this line:

```
- cron: "0 1 * * *"
```

Cron format is `minute hour day month weekday`, always in **UTC**. Examples:

- `0 1 * * *` -> once a day at 1am UTC (8am Vietnam, current setting)
- `0 */6 * * *` -> every 6 hours
- `0 1 * * 1` -> once a week, Monday 1am UTC

A handy converter: https://crontab.guru (shows what a cron string means, but
you still need to convert your local time to UTC yourself, e.g. via
https://www.timeanddate.com/worldclock/converter.html)

Central bank rates only change a handful of times a year, so a daily or
even weekly schedule is plenty — there's no need to poll every 30 minutes
like the price-emailer repos.

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
