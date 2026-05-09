# Quant Dashboard

A small Flask dashboard that scores a watchlist of tickers using live data from Yahoo Finance (yfinance). No API key required.

## Quick start

```bash
pip install -r requirements.txt
python3 app.py
```

Then open http://localhost:5050.

## Stack

- Flask
- numpy (Black-Scholes gamma)
- yfinance (quotes + options chain)
- Chart.js (frontend, via CDN)

## Configuration

- `WATCHLIST` and `RISK_FREE_RATE`: edit constants at the top of `app.py`.
- `PORT`: set the `PORT` env var to override the default 5050.

## Deploy

`render.yaml` is preconfigured for Render. Push this folder to a GitHub repo, connect the repo on Render, and it deploys with no env vars needed. The public URL Render assigns is your shareable link.

## Notes

Yahoo Finance applies per-IP rate limits. The app caches results for 15 minutes (`CACHE_TTL`) and warms the cache on startup. If you grow the watchlist beyond ~20 tickers on a hosted IP you may need a longer TTL or a request-cache layer.
