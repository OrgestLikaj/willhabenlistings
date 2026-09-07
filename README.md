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

## Configuration

Repo variable (Settings -> Secrets and variables -> Actions -> Variables):

- `WILLHABEN_URL` - full results URL copied from the browser after clicking
  "Suchen". Overrides `DEFAULT_URL` in the code.

Repo secrets (same page, Secrets tab):

- `TG_TOKEN` - Telegram bot token from @BotFather
- `TG_CHAT_ID` - your chat ID from `https://api.telegram.org/bot<TOKEN>/getUpdates`

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
