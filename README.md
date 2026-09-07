# willhaben listings watcher

Checks a filtered willhaben.at rental search every hour via GitHub Actions and
sends a Telegram message for each new listing that passes the filters.

## Files

| File | Purpose |
|---|---|
| `scraper.py` | The scraper. Search URL, numeric filters and text rules are all at the top. |
| `test_filters.py` | Offline test of the text patterns. No network needed. |
| `inspect_debug.py` | Offline diagnostic: reads `debug.json` and shows why listings were rejected. |
| `.github/workflows/scrape.yml` | Hourly schedule + commits `seen.json` back to the repo. |
| `seen.json` | Listing IDs already processed. Committed by the workflow; do not gitignore. |
| `state.json` | Timestamp of the last run, used for the notification window. Also committed. |

## Configuration

Repo variable (Settings -> Secrets and variables -> Actions -> Variables):

- `WILLHABEN_URL` - full results URL copied from the browser after clicking
  "Suchen". Overrides `DEFAULT_URL` in the code.

Repo secrets (same page, Secrets tab):

- `TG_TOKEN` - Telegram bot token from @BotFather
- `TG_CHAT_ID` - your chat ID from `https://api.telegram.org/bot<TOKEN>/getUpdates`
- `SMTP_USER` - the Gmail address that sends the mail
- `SMTP_PASS` - a Google App Password (16 chars), NOT the account password

## Local run

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

python test_filters.py

set DEBUG=1
set TG_TOKEN=...
set TG_CHAT_ID=...
python scraper.py
```

`DEBUG=1` dumps the parsed page JSON to `debug.json`, which is what you inspect
when willhaben changes their data shape.

## Notes

- The search URL must be copied from the browser. Hand-building it from
  individual query params does not work: willhaben resolves the area filter
  from the `sfId`, so a rebuilt URL silently returns the wrong region.
- If the scraper suddenly reports 0 matches, the `sfId` may have expired. Redo
  the search in the browser and update the `WILLHABEN_URL` repo variable.
- `seen.json` must NOT be gitignored - it is the state that prevents duplicate
  notifications, and the workflow commits it after each run.
## Notifications

- Every new listing is reported; nothing is dropped. Up to `MAX_INDIVIDUAL`
  (15) they arrive as one message each. Above that they are batched into
  digests of `DIGEST_CHUNK` (10) so a large backlog is a few messages, not 90.
- When nothing new turns up, a heartbeat message reports the window since the
  previous run plus the filter counts. Turn it off with `HEARTBEAT = False`.
  Heartbeats are sent silently (`HEARTBEAT_SILENT`); real listings are not.
- The window comes from `state.json`. On the very first run there is no
  previous timestamp, so the window shows start == end. Normal from run two on.
- Email is a second channel alongside Telegram: one email per run containing
  every new listing, or a "keine neuen Inserate" mail when there is nothing.
  Recipient is `EMAIL_TO` in `scraper.py`; set `EMAIL_ENABLED = False` to stop.
  Each channel works independently - missing SMTP credentials do not affect
  Telegram and vice versa.
- Gmail requires an App Password (myaccount.google.com -> Security -> 2-Step
  Verification -> App passwords). Regular passwords are rejected by SMTP.
- `BONUS` maps a regex to `(points, "display label")`. The label is what goes
  into messages - never the pattern itself, since regex syntax like
  `(?<!kein )` contains `<` and breaks Telegram's HTML parser.
- All listing text is HTML-escaped before sending. If a Telegram send still
  fails on entity parsing, the same content is resent as plain text so a
  notification is never lost.
- Times use `TZ_NAME` (Europe/Vienna). GitHub runners are UTC, so without this
  the timestamps would be an hour or two off local time.

## Notes

- `MAX_PRICE` and `MIN_AREA` in `scraper.py` must be kept in sync with
  `PRICE_TO` and `ESTATE_SIZE/LIVING_AREA_FROM` in the search URL. The double
  filtering is deliberate (willhaben's own filters are sometimes loose) but the
  two drifting apart silently drops valid listings.
- Listings are notified in willhaben's newest-first order. `score()` only
  applies keyword bonuses/penalties and is shown in the message for context;
  uncomment the `kept.sort(...)` line in `main()` to rank by it instead.
- `ALLOWED_POSTCODES` in `scraper.py` is a location safety net, off by default.
  Fill it in to guarantee the scraper never alerts on the wrong region - useful
  because willhaben area IDs are opaque and easy to mis-select in the UI.
  (117223-117231 are Vienna districts 1-9, not Lower Austria.)
- The workflow's commit step retries with a rebase up to 5 times. Pushing to
  the repo while a run is in flight otherwise rejects the `seen.json` commit,
  which causes one duplicate batch of notifications on the next run.
- Scheduled GitHub Actions run late (typically 5-20 min) and occasionally skip
  an hour. For faster alerts, run the same script from cron on a small VPS.
- GitHub disables scheduled workflows after 60 days of repo inactivity. The
  `seen.json` commits usually prevent this.
