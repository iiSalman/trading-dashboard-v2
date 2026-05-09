# Quant Dashboard

Static dashboard that scores a watchlist of tickers using live data from Yahoo Finance.

**Live:** https://iisalman.github.io/trading-dashboard-v2/

## How it works

1. A GitHub Actions cron (`*/15 * * * *`, see `.github/workflows/refresh.yml`) runs `python tools/build_snapshot.py`.
2. That script calls yfinance, computes PCR / GEX / scores / market-bias via `analyzer.py`, and writes `docs/data/snapshot.json`.
3. The action commits the updated JSON.
4. GitHub Pages serves `docs/` so the dashboard at `index.html` reads the freshly committed snapshot.

No server, no API key, no laptop dependency. Yahoo rate-limits cloud-host IP ranges (Render, Heroku, etc.); GitHub-hosted runners aren't blocked, which is why this architecture works where a Flask-on-Render setup didn't.

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
