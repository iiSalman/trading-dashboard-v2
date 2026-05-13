# Quant Dashboard

Static dashboard that scores a watchlist of tickers using live data from Yahoo Finance.

**Live:** https://iisalman.github.io/trading-dashboard-v2/

## How it works

1. An external HTTP pinger (UptimeRobot / cron-job.org, free) hits `GET https://trading-dashboard-v2.onrender.com/tick` every 5 min.
2. `/tick` on the Render proxy dispatches the `refresh.yml` workflow (rate-limited to once per 4 min) and keeps the dyno warm.
3. The workflow runs `python tools/build_snapshot.py` — calls yfinance, computes PCR / GEX / scores / market-bias via `analyzer.py`, writes `docs/data/snapshot.json`, commits.
4. GitHub Pages serves `docs/` so `index.html` reads the freshly committed snapshot.

Why an external pinger instead of GitHub Actions' built-in cron: scheduled workflows on public free-tier repos get silently throttled — runs are dropped under load, so `*/5 * * * *` ends up firing once every 1-3 hours in practice. The workflow file still has the cron block as a backup, but `/tick` is the primary driver.

Yahoo rate-limits cloud-host IP ranges (Render, Heroku, etc.); GitHub-hosted runners aren't blocked, which is why the data fetch happens inside Actions rather than on the Render dyno.

## Stack

- Python (numpy, yfinance, curl_cffi) — data pipeline
- GitHub Actions — scheduler
- GitHub Pages — hosting
- Chart.js (CDN) — frontend chart
- Vanilla HTML/CSS/JS — UI

## Local dev

```bash
pip install -r requirements.txt
python tools/build_snapshot.py        # writes docs/data/snapshot.json
python -m http.server -d docs 8000    # serves the dashboard at localhost:8000
```

## Configuration

Edit `WATCHLIST` and `RISK_FREE_RATE` at the top of `analyzer.py`.
