# NJ Security Bid Watcher

Scans NJ county, municipal, and school-district bid pages for security-system
solicitations (access control, CCTV, surveillance, alarms, intercom, etc.) and
emails you only NEW matches, with links.

## Why this works for NJ
Under L.2025 c.72, every NJ local government and school district must post bid
advertisements on its own website (since March 1, 2026) and register that page
with the Dept. of State directory:
https://www.nj.gov/state/statewide-legal-notices-list.shtml
That directory is your master list of sites to scan.

## Setup
1. `pip install -r requirements.txt`
2. Build the source list (below), or hand-edit `sources.json`.
3. `python bid_watch.py --dry-run` to see what it finds.
4. `python bid_watch.py --baseline` once, so old bids don't flood your inbox.
5. Set env vars: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS
   (Gmail/Google Workspace: use an App Password). Emails go to
   John.borgia@cyberhomessystems.com by default; override with EMAIL_TO.
6. Schedule for Monday 7:00 AM (cron):
   `0 7 * * 1 cd /path/bid-watcher && python bid_watch.py`
   Each Monday John gets the new bids, or a short "no new bids" note so he knows it ran.

## Getting the full NJ site list
The DOS directory is an embedded Power BI report. Open it, hover over the table,
use "..." > Export data (CSV) if enabled. Then:
    python build_sources.py your_export.csv
If export is disabled, copy the table into a spreadsheet and save as CSV with a
name column and a URL column. Or email DOS.Public-Notice@sos.nj.gov and ask for the list.

## Notes
- The script also opens PDF bid ads (first 6 pages) when the link title is
  generic; set PDF_SCAN = False in bid_watch.py to turn that off.
- Sites that load content with JavaScript return nothing; failures are listed
  in the email. Scanned PDFs (images) can't be read.
- Edit KEYWORDS at the top of bid_watch.py to tune results.
- NJSTART (state bids, njstart.gov) is JavaScript-based; use its built-in
  email alerts for state-level bids.

## Running on GitHub Actions (recommended)
1. Create a **private** GitHub repo and upload everything in this folder
   (including the hidden `.github/workflows/bid-watch.yml`).
2. Repo > Settings > Secrets and variables > Actions > New repository secret.
   Add: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS.
   (Gmail: smtp.gmail.com / 587 / your address / App Password.)
3. Make sure `sources.json` is filled in (see "Getting the full NJ site list").
4. Actions tab > "Weekly NJ Bid Watch" > Run workflow > choose **baseline** once.
5. After that it runs itself every Monday ~7 AM Eastern and emails John.
   Use "dry-run" to preview results any time without emailing.
The job saves `seen_bids.db` back to the repo after each run so it never
re-sends old bids. GitHub's cron is UTC, so the time shifts an hour with daylight saving.
