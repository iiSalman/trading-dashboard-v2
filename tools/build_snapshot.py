#!/usr/bin/env python3
"""Fetch live market data via yfinance and write a JSON snapshot the static
dashboard reads. Designed to run in GitHub Actions (whose IPs aren't blocked
by Yahoo) on a 5-minute cron.

In addition to the aggregate snapshot.json, this script also writes a
per-ticker JSON file at docs/data/tickers/<T>.json for each watchlist name.
v3 stock.html serves these for "hot" tickers, bypassing the Render proxy
entirely (free CORS, zero IP-reputation issues, ~5-min freshness)."""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from analyzer import WATCHLIST, get_all_data, get_expirations, get_chain, _yf

OUT = os.path.join(ROOT, 'docs', 'data', 'snapshot.json')
TICKERS_DIR = os.path.join(ROOT, 'docs', 'data', 'tickers')


def _build_ticker_snapshot(ticker):
    """Build one per-ticker JSON in the shape v3 stock.html expects from
    the proxy's /yahoo/options + /yahoo/chart endpoints. Best-effort: any
    section that fails is dropped, not raised, so a partial file is still
    usable by the frontend."""
    out = {
        'ticker': ticker,
        'source': 'GitHub Actions cron · Yahoo',
        'updated': datetime.now(timezone.utc).isoformat(),
        'spot': None,
        'previous_close': None,
        'expiries': [],
        'candles': [],
    }

    # 1) Intraday 1d/5m candles + spot/prev
    try:
        df = _yf(ticker).history(period='1d', interval='5m', auto_adjust=False, prepost=False)
        if df is not None and not df.empty:
            for ts, row in df.iterrows():
                c = row.get('Close')
                if c is None or c != c:  # NaN
                    continue
                out['candles'].append({
                    't': int(ts.timestamp()),
                    'o': float(row.get('Open',  c)),
                    'h': float(row.get('High',  c)),
                    'l': float(row.get('Low',   c)),
                    'c': float(c),
                    'v': int(row.get('Volume', 0) or 0),
                })
            if out['candles']:
                out['spot'] = out['candles'][-1]['c']
        # Pull prev close from a 5d daily history
        df5 = _yf(ticker).history(period='5d', interval='1d', auto_adjust=False)
        if df5 is not None and len(df5) >= 2:
            out['previous_close'] = float(df5['Close'].iloc[-2])
    except Exception as e:
        print(f'  {ticker}: candles error: {e}', file=sys.stderr)

    # 2) Options chains — up to 3 nearest expiries
    try:
        all_exps = get_expirations(ticker)[:3]
        today_d = int(time.time() // 86400)
        for date_str in all_exps:
            try:
                rows = get_chain(ticker, date_str)
            except Exception as e:
                print(f'  {ticker} {date_str}: chain error: {e}', file=sys.stderr)
                continue
            calls = {}
            puts = {}
            for r in rows:
                K = r.get('strike')
                if K is None:
                    continue
                K = float(K)
                if r.get('option_type') == 'call':
                    calls[K] = r
                else:
                    puts[K] = r

            def _i(x):
                try: return int(x) if x == x else 0
                except (TypeError, ValueError): return 0
            def _f(x):
                try: f = float(x); return f if f == f else 0.0
                except (TypeError, ValueError): return 0.0

            strikes_out = []
            for K in sorted(set(calls) | set(puts)):
                c = calls.get(K) or {}
                p = puts.get(K)  or {}
                strikes_out.append({
                    'K': K,
                    'call_vol':  _i(c.get('volume')),
                    'call_oi':   _i(c.get('open_interest')),
                    'call_last': _f(c.get('last')),
                    'put_vol':   _i(p.get('volume')),
                    'put_oi':    _i(p.get('open_interest')),
                    'put_last':  _f(p.get('last')),
                })
            try:
                exp_unix = int(time.mktime(time.strptime(date_str, '%Y-%m-%d')))
                dte = max(0, int(exp_unix // 86400) - today_d)
            except Exception:
                dte = None
            out['expiries'].append({'date': date_str, 'dte': dte, 'strikes': strikes_out})
    except Exception as e:
        print(f'  {ticker}: expirations error: {e}', file=sys.stderr)

    return out


def write_per_ticker_snapshots():
    """Run in parallel for the WATCHLIST (~25 tickers). Each gets its own
    JSON file in docs/data/tickers/<T>.json. Failures don't kill the run."""
    os.makedirs(TICKERS_DIR, exist_ok=True)
    written = 0
    skipped = 0
    targets = list(WATCHLIST) + ['SPY']  # SPY = market reference, top-card in v3 strip
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(_build_ticker_snapshot, t): t for t in targets}
        for future in as_completed(futures):
            t = futures[future]
            try:
                data = future.result()
                # Skip if both data sources collapsed — keep stale file
                if not data['candles'] and not data['expiries']:
                    skipped += 1
                    continue
                path = os.path.join(TICKERS_DIR, f'{t}.json')
                with open(path, 'w') as f:
                    json.dump(data, f, separators=(',', ':'), default=str)
                written += 1
            except Exception as e:
                print(f'  {t}: snapshot write error: {e}', file=sys.stderr)
                skipped += 1
    print(f"Per-ticker snapshots: wrote {written}, skipped {skipped}")


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

    # Per-ticker snapshots for v3 stock.html (best-effort, doesn't gate the run)
    try:
        write_per_ticker_snapshots()
    except Exception as e:
        print(f"per-ticker step failed (non-fatal): {e}", file=sys.stderr)


if __name__ == '__main__':
    main()
