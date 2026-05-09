#!/usr/bin/env python3
"""Fetch live market data via yfinance and write a JSON snapshot the static
dashboard reads. Designed to run in GitHub Actions (whose IPs aren't blocked
by Yahoo) on a 15-minute cron."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from analyzer import get_all_data

OUT = os.path.join(ROOT, 'docs', 'data', 'snapshot.json')


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    data = get_all_data()

    with open(OUT, 'w') as f:
        json.dump(data, f, indent=2, default=str)

    spy = data.get('spy', {}) or {}
    tickers = data.get('tickers', []) or []
    errs = [t for t in tickers if t.get('error')]
    print(f"Wrote {OUT}")
    print(f"  SPY: ${spy.get('price')} ({spy.get('change')}%) bias={data.get('market_bias')}")
    print(f"  Tickers: {len(tickers)} ({len(errs)} errors)")
    for t in errs:
        print(f"    {t.get('ticker')}: {t.get('error')}")

    if errs and len(errs) == len(tickers):
        print("FATAL: every ticker errored. Likely upstream rate-limit.", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
