"""Tiny Flask proxy: the public GitHub Pages dashboard cannot hold a GitHub
token, so it POSTs here. We hold GH_TOKEN as a Render env var, validate the
request origin, and forward to GitHub's workflow_dispatch endpoint."""
import os
import time
import threading
from collections import deque

import requests
import yfinance as yf
from flask import Flask, jsonify, request

# Yahoo's edge aggressively rate-limits raw requests from shared hosting IPs
# (Render's pool included). curl_cffi impersonates a real Chrome TLS
# fingerprint (JA3) which bypasses the JA3-based bot detection. yfinance
# accepts a `session=` parameter that gets used for all upstream calls.
try:
    from curl_cffi import requests as _cffi_requests
    _yf_session = _cffi_requests.Session(impersonate='chrome')
except Exception:  # fall back to plain requests if curl_cffi missing
    _yf_session = None

app = Flask(__name__)

GH_TOKEN     = os.environ.get('GH_TOKEN', '').strip()
GH_REPO      = os.environ.get('GH_REPO', 'iiSalman/trading-dashboard-v2')
GH_WORKFLOW  = os.environ.get('GH_WORKFLOW', 'refresh.yml')
GH_BRANCH    = os.environ.get('GH_BRANCH', 'main')

ALLOWED_ORIGINS = {
    'https://iisalman.github.io',
    'http://localhost:8000',
    'http://localhost:5050',
    'http://127.0.0.1:8000',
}

_recent = deque(maxlen=20)
_lock = threading.Lock()
RATE_WINDOW = 30  # seconds between dispatches per origin

# /tick (external pinger) bookkeeping — separate from per-origin rate limit
# because the pinger is unauthenticated and doesn't send Origin.
_last_tick_dispatch = 0.0
_tick_lock = threading.Lock()
TICK_MIN_GAP_S = 240  # at most one dispatch every 4 min from /tick


def _cors_headers(origin):
    if origin in ALLOWED_ORIGINS:
        return {
            'Access-Control-Allow-Origin': origin,
            'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type',
            'Access-Control-Max-Age': '600',
        }
    return {}


def _data_cors(origin):
    """Looser CORS for read-only data endpoints (Yahoo/FINRA proxies).
    Returns headers if the origin is one of ours; otherwise still returns
    Access-Control-Allow-Origin: * so curl/server-to-server consumers work.
    These endpoints expose only public market data — no token, no state."""
    if origin in ALLOWED_ORIGINS:
        return {
            'Access-Control-Allow-Origin': origin,
            'Access-Control-Allow-Methods': 'GET, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type',
            'Access-Control-Max-Age': '600',
        }
    return {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'GET, OPTIONS',
    }


# 60-second TTL cache for upstream data fetches (Yahoo/FINRA)
_data_cache = {}
_data_cache_lock = threading.Lock()
DATA_TTL = 60

def _cache_get(key):
    with _data_cache_lock:
        entry = _data_cache.get(key)
        if entry and (time.time() - entry[0]) < DATA_TTL:
            return entry[1]
    return None

def _cache_set(key, value):
    with _data_cache_lock:
        _data_cache[key] = (time.time(), value)


YAHOO_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/plain,*/*',
}


def _rate_limited(origin):
    now = time.time()
    with _lock:
        for o, ts in list(_recent):
            if now - ts > RATE_WINDOW * 4:
                _recent.popleft()
        for o, ts in _recent:
            if o == origin and (now - ts) < RATE_WINDOW:
                return int(RATE_WINDOW - (now - ts))
        _recent.append((origin, now))
    return 0


@app.route('/refresh', methods=['POST', 'OPTIONS'])
def refresh():
    origin = request.headers.get('Origin', '')
    headers = _cors_headers(origin)

    if request.method == 'OPTIONS':
        return ('', 204, headers)

    if origin not in ALLOWED_ORIGINS:
        return (jsonify({'error': 'origin not allowed'}), 403, headers)

    wait = _rate_limited(origin)
    if wait:
        return (jsonify({'error': 'rate limited', 'retry_in_seconds': wait}), 429, headers)

    if not GH_TOKEN:
        return (jsonify({'error': 'GH_TOKEN not configured on server'}), 500, headers)

    r = requests.post(
        f'https://api.github.com/repos/{GH_REPO}/actions/workflows/{GH_WORKFLOW}/dispatches',
        headers={
            'Authorization': f'Bearer {GH_TOKEN}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'trading-dashboard-refresher',
        },
        json={'ref': GH_BRANCH},
        timeout=15,
    )

    if r.status_code == 204:
        return (jsonify({
            'status': 'dispatched',
            'workflow': GH_WORKFLOW,
            'expected_eta_seconds': 35,
        }), 200, headers)
    return (
        jsonify({'error': f'GitHub returned {r.status_code}', 'detail': r.text[:300]}),
        502, headers,
    )


@app.route('/tick', methods=['GET', 'HEAD'])
def tick():
    """External pinger (UptimeRobot / cron-job.org) hits this every 5 min.
    Replaces GitHub Actions' unreliable cron schedule, and keeps the Render
    dyno warm so the user-facing Force Refresh button never cold-starts."""
    if request.method == 'HEAD':
        return ('', 200)
    if not GH_TOKEN:
        return jsonify({'error': 'GH_TOKEN not configured'}), 500

    global _last_tick_dispatch
    now = time.time()
    with _tick_lock:
        gap = now - _last_tick_dispatch
        if gap < TICK_MIN_GAP_S:
            return jsonify({
                'dispatched': False,
                'reason': 'rate_limited',
                'next_in_seconds': int(TICK_MIN_GAP_S - gap),
            })
        _last_tick_dispatch = now

    r = requests.post(
        f'https://api.github.com/repos/{GH_REPO}/actions/workflows/{GH_WORKFLOW}/dispatches',
        headers={
            'Authorization': f'Bearer {GH_TOKEN}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'trading-dashboard-tick',
        },
        json={'ref': GH_BRANCH},
        timeout=15,
    )
    if r.status_code == 204:
        return jsonify({'dispatched': True, 'workflow': GH_WORKFLOW})
    # On failure, reset the gap so the next ping can retry immediately.
    with _tick_lock:
        _last_tick_dispatch = 0.0
    return jsonify({
        'dispatched': False,
        'error': f'GitHub returned {r.status_code}',
        'detail': r.text[:300],
    }), 502


@app.route('/health')
def health():
    return jsonify({
        'ok': True,
        'repo': GH_REPO,
        'workflow': GH_WORKFLOW,
        'branch': GH_BRANCH,
        'token_configured': bool(GH_TOKEN),
    })


def _safe_ticker(t):
    """Validate a user-supplied ticker symbol."""
    t = (t or '').strip().upper()
    if not t or len(t) > 6:
        return None
    # Allow letters, digits, dot, hyphen (covers BRK.B, BF-B, etc.)
    if not all(c.isalnum() or c in '.-' for c in t):
        return None
    return t


def _to_float(x):
    """yfinance returns numpy scalars/NaN; coerce to plain Python float-or-None."""
    try:
        f = float(x)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _to_int(x):
    try:
        f = float(x)
        if f != f:
            return 0
        return int(f)
    except (TypeError, ValueError):
        return 0


@app.route('/yahoo/options/<ticker>', methods=['GET', 'OPTIONS'])
def yahoo_options(ticker):
    """Fetch up to 3 nearest expiries via yfinance (handles Yahoo's
    cookie/crumb dance + rate-limit backoff internally).
    Returns: { ticker, spot, expiries: [{ date, dte, strikes:[{K,call_vol,...}] }] }"""
    origin = request.headers.get('Origin', '')
    headers = _data_cors(origin)
    if request.method == 'OPTIONS':
        return ('', 204, headers)

    t = _safe_ticker(ticker)
    if not t:
        return (jsonify({'error': 'invalid ticker'}), 400, headers)

    cached = _cache_get(('opts', t))
    if cached is not None:
        return (jsonify(cached), 200, headers)

    try:
        tk = yf.Ticker(t, session=_yf_session) if _yf_session else yf.Ticker(t)
        exps = list(tk.options or [])[:3]
        if not exps:
            return (jsonify({'error': 'no options chain'}), 404, headers)

        spot = None
        try:
            fi = getattr(tk, 'fast_info', None)
            if fi:
                spot = _to_float(fi.get('last_price') if hasattr(fi, 'get') else fi.last_price)
        except Exception:
            spot = None

        today = time.gmtime()
        today_epoch_d = int(time.mktime((today.tm_year, today.tm_mon, today.tm_mday, 0,0,0,0,0,0)) // 86400)

        expiries = []
        for date_str in exps:
            try:
                chain = tk.option_chain(date_str)
                calls_df = chain.calls
                puts_df  = chain.puts
            except Exception:
                continue
            strikes_map = {}
            for _, row in calls_df.iterrows():
                K = _to_float(row.get('strike'))
                if K is None:
                    continue
                strikes_map.setdefault(K, {})['call'] = row
            for _, row in puts_df.iterrows():
                K = _to_float(row.get('strike'))
                if K is None:
                    continue
                strikes_map.setdefault(K, {})['put'] = row
            def _g(series, field):
                """Safe get from a pandas Series, returning None if absent."""
                if series is None:
                    return None
                try:
                    if field in series:
                        return series[field]
                except Exception:
                    pass
                return None

            strikes_out = []
            for K in sorted(strikes_map.keys()):
                c = strikes_map[K].get('call')
                p = strikes_map[K].get('put')
                strikes_out.append({
                    'K': K,
                    'call_vol':  _to_int(_g(c, 'volume')),
                    'call_oi':   _to_int(_g(c, 'openInterest')),
                    'call_last': _to_float(_g(c, 'lastPrice')) or 0.0,
                    'put_vol':   _to_int(_g(p, 'volume')),
                    'put_oi':    _to_int(_g(p, 'openInterest')),
                    'put_last':  _to_float(_g(p, 'lastPrice')) or 0.0,
                })
            try:
                exp_tuple = time.strptime(date_str, '%Y-%m-%d')
                exp_epoch_d = int(time.mktime(exp_tuple) // 86400)
                dte = max(0, exp_epoch_d - today_epoch_d)
            except Exception:
                dte = None
            expiries.append({'date': date_str, 'dte': dte, 'strikes': strikes_out})

        payload = {'ticker': t, 'spot': spot, 'expiries': expiries}
        _cache_set(('opts', t), payload)
        return (jsonify(payload), 200, headers)
    except Exception as e:
        return (jsonify({'error': 'options fetch failed', 'detail': str(e)[:200]}), 502, headers)


@app.route('/yahoo/chart/<ticker>', methods=['GET', 'OPTIONS'])
def yahoo_chart(ticker):
    """Fetch price candles via yfinance.
    ?range=1d (5-min) or ?range=5d (15-min) or ?range=1mo (1-day) etc."""
    origin = request.headers.get('Origin', '')
    headers = _data_cors(origin)
    if request.method == 'OPTIONS':
        return ('', 204, headers)

    t = _safe_ticker(ticker)
    if not t:
        return (jsonify({'error': 'invalid ticker'}), 400, headers)

    rng = request.args.get('range', '1d').lower()
    interval_map = {'1d': '5m', '5d': '15m', '1mo': '1d', '3mo': '1d', '1y': '1wk'}
    if rng not in interval_map:
        return (jsonify({'error': 'invalid range'}), 400, headers)
    interval = interval_map[rng]

    cached = _cache_get(('chart', t, rng))
    if cached is not None:
        return (jsonify(cached), 200, headers)

    try:
        tk = yf.Ticker(t, session=_yf_session) if _yf_session else yf.Ticker(t)
        df = tk.history(period=rng, interval=interval, auto_adjust=False, prepost=False)
        if df is None or df.empty:
            return (jsonify({'error': 'no chart data'}), 404, headers)
        candles = []
        for ts, row in df.iterrows():
            try:
                t_unix = int(ts.timestamp())
            except Exception:
                continue
            c = _to_float(row.get('Close'))
            if c is None:
                continue
            candles.append({
                't': t_unix,
                'o': _to_float(row.get('Open')),
                'h': _to_float(row.get('High')),
                'l': _to_float(row.get('Low')),
                'c': c,
                'v': _to_int(row.get('Volume')),
            })

        spot = candles[-1]['c'] if candles else None
        prev = None
        try:
            fi = getattr(tk, 'fast_info', None)
            if fi:
                spot = _to_float(fi.get('last_price') if hasattr(fi, 'get') else fi.last_price) or spot
                prev = _to_float(fi.get('previous_close') if hasattr(fi, 'get') else fi.previous_close)
        except Exception:
            pass

        payload = {
            'ticker': t,
            'range': rng,
            'interval': interval,
            'spot': spot,
            'previous_close': prev,
            'candles': candles,
        }
        _cache_set(('chart', t, rng), payload)
        return (jsonify(payload), 200, headers)
    except Exception as e:
        return (jsonify({'error': 'chart fetch failed', 'detail': str(e)[:200]}), 502, headers)


@app.route('/finra/darkpool/<ticker>', methods=['GET', 'OPTIONS'])
def finra_darkpool(ticker):
    """Fetch last N days of FINRA Reg-SHO CNMS short-volume files (free, T+1).
    The CNMS file is consolidated NMS short volume per symbol per day — the
    closest free public proxy for off-exchange / dark-pool interest at daily
    granularity. (True intraday dark-pool tape is a paid product.)"""
    origin = request.headers.get('Origin', '')
    headers = _data_cors(origin)
    if request.method == 'OPTIONS':
        return ('', 204, headers)

    t = _safe_ticker(ticker)
    if not t:
        return (jsonify({'error': 'invalid ticker'}), 400, headers)

    try:
        days_wanted = max(1, min(10, int(request.args.get('days', 5))))
    except ValueError:
        days_wanted = 5

    cached = _cache_get(('finra', t, days_wanted))
    if cached is not None:
        return (jsonify(cached), 200, headers)

    out_days = []
    consecutive_empty = 0
    EMPTY_GIVEUP = 6  # circuit-breaker: stop after this many empty days in a row
    now = time.time()
    # Walk backward up to 21 calendar days to gather <days_wanted> trading days
    # with actual data (skip weekends + empty-stub days near publish lag).
    for offset in range(1, 22):
        d = time.gmtime(now - offset * 86400)
        if d.tm_wday >= 5:  # Sat/Sun
            continue
        ymd = time.strftime('%Y%m%d', d)
        url = f'https://cdn.finra.org/equity/regsho/daily/CNMSshvol{ymd}.txt'
        try:
            r = requests.get(url, timeout=5)
            if r.status_code != 200 or len(r.text) < 200:
                consecutive_empty += 1
                if consecutive_empty >= EMPTY_GIVEUP:
                    break
                continue
            short_vol = 0
            total_vol = 0
            for line in r.text.splitlines()[1:]:
                parts = line.split('|')
                if len(parts) >= 5 and parts[1].strip().upper() == t:
                    try:
                        short_vol += int(parts[2])
                        total_vol += int(parts[4])
                    except (ValueError, IndexError):
                        continue
            if total_vol > 0:
                consecutive_empty = 0
                out_days.append({
                    'date': time.strftime('%Y-%m-%d', d),
                    'short_vol': short_vol,
                    'total_vol': total_vol,
                    'short_pct': round(100.0 * short_vol / total_vol, 2),
                })
                if len(out_days) >= days_wanted:
                    break
            else:
                consecutive_empty += 1
                if consecutive_empty >= EMPTY_GIVEUP:
                    break
        except requests.RequestException:
            consecutive_empty += 1
            if consecutive_empty >= EMPTY_GIVEUP:
                break
            continue

    out_days.reverse()  # oldest first
    payload = {
        'ticker': t,
        'source': 'FINRA Reg-SHO CNMS daily short volume',
        'note': 'Daily consolidated short volume — proxy for off-exchange interest. Intraday dark-pool tape is a paid product.',
        'days': out_days,
    }
    _cache_set(('finra', t, days_wanted), payload)
    return (jsonify(payload), 200, headers)


@app.route('/')
def root():
    return jsonify({
        'service': 'trading-dashboard refresh proxy + data',
        'endpoints': [
            'POST /refresh', 'GET /tick', 'GET /health',
            'GET /yahoo/options/<T>',
            'GET /yahoo/chart/<T>?range=1d|5d|1mo|3mo|1y',
            'GET /finra/darkpool/<T>?days=1..10',
        ],
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5050)))
