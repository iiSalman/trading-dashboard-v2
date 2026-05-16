"""Tiny Flask proxy: the public GitHub Pages dashboard cannot hold a GitHub
token, so it POSTs here. We hold GH_TOKEN as a Render env var, validate the
request origin, and forward to GitHub's workflow_dispatch endpoint."""
import os
import time
import threading
from collections import deque

import requests
from flask import Flask, jsonify, request

# Yahoo's edge aggressively rate-limits any TLS handshake that doesn't look
# like a real browser (JA3 fingerprinting). curl_cffi impersonates Chrome's
# TLS handshake exactly, which gets us past Render-shared-IP rate limiting.
# We use it directly (bypassing yfinance) because yfinance's `session=` has
# known leakage where the cookie-bootstrap call uses raw requests anyway.
_YF_LAST_ERR = None
try:
    from curl_cffi import requests as _cffi_requests
    _yf_session = _cffi_requests.Session(impersonate='chrome')
except Exception as _e:
    _yf_session = None
    _YF_LAST_ERR = f'curl_cffi import: {_e}'

app = Flask(__name__)

GH_TOKEN     = os.environ.get('GH_TOKEN', '').strip()
GH_REPO      = os.environ.get('GH_REPO', 'iiSalman/trading-dashboard-v2')
GH_WORKFLOW  = os.environ.get('GH_WORKFLOW', 'refresh.yml')
GH_BRANCH    = os.environ.get('GH_BRANCH', 'main')

# Tradier Sandbox API — free, no IP-blocking, returns options chains with
# Greeks. Sign up at developer.tradier.com (no credit card) and set
# TRADIER_TOKEN as a Render env var. Used by /tradier/options/<T>.
TRADIER_TOKEN = os.environ.get('TRADIER_TOKEN', '').strip()
TRADIER_BASE  = os.environ.get('TRADIER_BASE', 'https://sandbox.tradier.com').strip()
_TRADIER_LAST_HTTP = None
_TRADIER_LAST_ERR  = None

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


# Yahoo cookie / crumb (cached 1h). Some endpoints don't require a crumb,
# but we always carry the session cookie since it lowers 429 risk.
_crumb_cache = {'crumb': None, 'expires': 0}
_crumb_lock = threading.Lock()
_YF_LAST_HTTP = None  # for /health/data observability

def _yahoo_bootstrap():
    """Hit fc.yahoo.com to drop a session cookie; fetch a fresh crumb."""
    global _YF_LAST_HTTP, _YF_LAST_ERR
    if not _yf_session:
        return None
    with _crumb_lock:
        if _crumb_cache['crumb'] and time.time() < _crumb_cache['expires']:
            return _crumb_cache['crumb']
        try:
            _yf_session.get('https://fc.yahoo.com', timeout=8, allow_redirects=True)
            r = _yf_session.get('https://query1.finance.yahoo.com/v1/test/getcrumb', timeout=8)
            _YF_LAST_HTTP = r.status_code
            if r.status_code == 200 and r.text and 'Too Many' not in r.text and len(r.text) < 64:
                _crumb_cache['crumb'] = r.text.strip()
                _crumb_cache['expires'] = time.time() + 3600
                return _crumb_cache['crumb']
            _YF_LAST_ERR = f'crumb HTTP {r.status_code}: {r.text[:80]}'
        except Exception as e:
            _YF_LAST_ERR = f'crumb fetch: {e}'
        return None

def _yahoo_get(url, params=None, with_crumb=False):
    """GET a Yahoo JSON endpoint via curl_cffi. Returns parsed JSON or raises."""
    global _YF_LAST_HTTP, _YF_LAST_ERR
    if not _yf_session:
        raise RuntimeError('curl_cffi session unavailable')
    p = dict(params or {})
    if with_crumb:
        c = _yahoo_bootstrap()
        if c:
            p['crumb'] = c
    r = _yf_session.get(url, params=p, timeout=10)
    _YF_LAST_HTTP = r.status_code
    if r.status_code != 200:
        _YF_LAST_ERR = f'{url.rsplit("/",2)[-2]}/{url.rsplit("/",1)[-1]} HTTP {r.status_code}: {r.text[:120]}'
        raise RuntimeError(f'yahoo HTTP {r.status_code}')
    return r.json()


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


@app.route('/health/data')
def health_data():
    """Visibility into the Yahoo data layer (for debugging 429s)."""
    cffi_ver = None
    try:
        import curl_cffi as _cc
        cffi_ver = getattr(_cc, '__version__', 'unknown')
    except Exception:
        pass
    return jsonify({
        'curl_cffi_loaded':    _yf_session is not None,
        'curl_cffi_version':   cffi_ver,
        'crumb_cached':        bool(_crumb_cache['crumb']),
        'last_yahoo_http':     _YF_LAST_HTTP,
        'last_yahoo_error':    _YF_LAST_ERR,
        'tradier_configured':  bool(TRADIER_TOKEN),
        'tradier_base':        TRADIER_BASE,
        'last_tradier_http':   _TRADIER_LAST_HTTP,
        'last_tradier_error':  _TRADIER_LAST_ERR,
        'cache_keys':          [str(k) for k in _data_cache.keys()],
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


@app.route('/tradier/options/<ticker>', methods=['GET', 'OPTIONS'])
def tradier_options(ticker):
    """Fetch up to 3 nearest expiries from Tradier Sandbox API. Same JSON
    shape as /yahoo/options so callers can swap without rewriting parsers.
    Tradier is a regulated US broker; sandbox tier is free, no credit card,
    not subject to IP-reputation blocking like Yahoo. 15-min delayed."""
    global _TRADIER_LAST_HTTP, _TRADIER_LAST_ERR
    origin = request.headers.get('Origin', '')
    headers = _data_cors(origin)
    if request.method == 'OPTIONS':
        return ('', 204, headers)

    t = _safe_ticker(ticker)
    if not t:
        return (jsonify({'error': 'invalid ticker'}), 400, headers)
    if not TRADIER_TOKEN:
        return (jsonify({'error': 'TRADIER_TOKEN not configured on server'}), 503, headers)

    cached = _cache_get(('tradier_opts', t))
    if cached is not None:
        return (jsonify(cached), 200, headers)

    auth = {'Authorization': f'Bearer {TRADIER_TOKEN}', 'Accept': 'application/json'}

    # 1) Get the list of expirations + the current quote (for spot)
    try:
        r_exp = requests.get(
            f'{TRADIER_BASE}/v1/markets/options/expirations',
            params={'symbol': t, 'includeAllRoots': 'true'},
            headers=auth, timeout=8,
        )
        _TRADIER_LAST_HTTP = r_exp.status_code
        if r_exp.status_code != 200:
            _TRADIER_LAST_ERR = f'expirations HTTP {r_exp.status_code}: {r_exp.text[:120]}'
            return (jsonify({'error': f'tradier HTTP {r_exp.status_code}'}), 502, headers)
        exp_node = (r_exp.json() or {}).get('expirations') or {}
        all_exps = exp_node.get('date') or []
        if isinstance(all_exps, str):  # single-expiry quirk
            all_exps = [all_exps]
        if not all_exps:
            return (jsonify({'error': 'no options chain for this ticker'}), 404, headers)
    except requests.RequestException as e:
        _TRADIER_LAST_ERR = f'expirations fetch: {e}'
        return (jsonify({'error': 'tradier fetch failed', 'detail': str(e)[:200]}), 502, headers)

    # 2) Spot price
    spot = None
    try:
        r_q = requests.get(
            f'{TRADIER_BASE}/v1/markets/quotes',
            params={'symbols': t}, headers=auth, timeout=5,
        )
        if r_q.status_code == 200:
            q = ((r_q.json() or {}).get('quotes') or {}).get('quote') or {}
            if isinstance(q, list):
                q = q[0] if q else {}
            spot = _to_float(q.get('last') or q.get('close'))
    except requests.RequestException:
        pass  # spot is best-effort

    # 3) Fetch chains for the 3 nearest expiries
    today_d = int(time.time() // 86400)
    expiries_out = []
    for date_str in all_exps[:3]:
        try:
            r_c = requests.get(
                f'{TRADIER_BASE}/v1/markets/options/chains',
                params={'symbol': t, 'expiration': date_str, 'greeks': 'true'},
                headers=auth, timeout=8,
            )
            _TRADIER_LAST_HTTP = r_c.status_code
            if r_c.status_code != 200:
                _TRADIER_LAST_ERR = f'chain HTTP {r_c.status_code}: {r_c.text[:120]}'
                continue
            opts = ((r_c.json() or {}).get('options') or {}).get('option') or []
            if isinstance(opts, dict):
                opts = [opts]
            calls = {}
            puts = {}
            for o in opts:
                K = _to_float(o.get('strike'))
                if K is None:
                    continue
                t_type = (o.get('option_type') or '').lower()
                if t_type == 'call':
                    calls[K] = o
                elif t_type == 'put':
                    puts[K] = o
            strikes_out = []
            for K in sorted(set(calls) | set(puts)):
                c = calls.get(K) or {}
                p = puts.get(K)  or {}
                strikes_out.append({
                    'K': K,
                    'call_vol':  _to_int(c.get('volume')),
                    'call_oi':   _to_int(c.get('open_interest')),
                    'call_last': _to_float(c.get('last')) or 0.0,
                    'put_vol':   _to_int(p.get('volume')),
                    'put_oi':    _to_int(p.get('open_interest')),
                    'put_last':  _to_float(p.get('last')) or 0.0,
                })
            try:
                exp_unix = int(time.mktime(time.strptime(date_str, '%Y-%m-%d')))
                dte = max(0, int(exp_unix // 86400) - today_d)
            except Exception:
                dte = None
            expiries_out.append({'date': date_str, 'dte': dte, 'strikes': strikes_out})
        except requests.RequestException as e:
            _TRADIER_LAST_ERR = f'chain fetch {date_str}: {e}'
            continue

    payload = {
        'ticker': t,
        'spot': spot,
        'source': 'Tradier Sandbox',
        'expiries': expiries_out,
    }
    _cache_set(('tradier_opts', t), payload)
    return (jsonify(payload), 200, headers)


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

    def _strike_row_from_chain(opt_block):
        calls = {c.get('strike'): c for c in (opt_block.get('calls') or [])}
        puts  = {p.get('strike'): p for p in (opt_block.get('puts')  or [])}
        out = []
        for K in sorted(set(calls) | set(puts)):
            c = calls.get(K) or {}
            p = puts.get(K)  or {}
            out.append({
                'K': K,
                'call_vol':  _to_int  (c.get('volume')),
                'call_oi':   _to_int  (c.get('openInterest')),
                'call_last': _to_float(c.get('lastPrice')) or 0.0,
                'put_vol':   _to_int  (p.get('volume')),
                'put_oi':    _to_int  (p.get('openInterest')),
                'put_last':  _to_float(p.get('lastPrice')) or 0.0,
            })
        return out

    url = f'https://query2.finance.yahoo.com/v7/finance/options/{t}'
    try:
        data = _yahoo_get(url, with_crumb=True)
        root = (data or {}).get('optionChain', {}).get('result', [])
        if not root:
            return (jsonify({'error': 'no options chain'}), 404, headers)
        node = root[0]
        spot = _to_float((node.get('quote') or {}).get('regularMarketPrice'))
        exp_dates = node.get('expirationDates') or []

        today_d = int(time.time() // 86400)
        expiries = []
        # The first expiry comes inline with the initial response
        first_opt = (node.get('options') or [{}])[0]
        first_unix = first_opt.get('expirationDate') or (exp_dates[0] if exp_dates else None)
        if first_unix:
            expiries.append({
                'date': time.strftime('%Y-%m-%d', time.gmtime(first_unix)),
                'dte':  max(0, int(first_unix // 86400) - today_d),
                'strikes': _strike_row_from_chain(first_opt),
            })
        # Pull up to 2 more expiries via ?date=<unix>
        for extra_unix in exp_dates[1:3]:
            try:
                d2 = _yahoo_get(url, params={'date': extra_unix}, with_crumb=True)
                r2 = (d2 or {}).get('optionChain', {}).get('result', [])
                if not r2:
                    continue
                opt = (r2[0].get('options') or [{}])[0]
                expiries.append({
                    'date': time.strftime('%Y-%m-%d', time.gmtime(extra_unix)),
                    'dte':  max(0, int(extra_unix // 86400) - today_d),
                    'strikes': _strike_row_from_chain(opt),
                })
            except Exception:
                continue

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
        data = _yahoo_get(
            f'https://query1.finance.yahoo.com/v8/finance/chart/{t}',
            params={'range': rng, 'interval': interval},
        )
        root = (data or {}).get('chart', {}).get('result', [])
        if not root:
            return (jsonify({'error': 'no chart data'}), 404, headers)
        node = root[0]
        ts = node.get('timestamp') or []
        ind = (node.get('indicators') or {}).get('quote', [{}])[0]
        meta = node.get('meta') or {}
        opens  = ind.get('open')   or []
        highs  = ind.get('high')   or []
        lows   = ind.get('low')    or []
        closes = ind.get('close')  or []
        vols   = ind.get('volume') or []
        candles = []
        for i, t_unix in enumerate(ts):
            c = closes[i] if i < len(closes) else None
            if c is None:
                continue
            candles.append({
                't': int(t_unix),
                'o': _to_float(opens[i] if i < len(opens) else None),
                'h': _to_float(highs[i] if i < len(highs) else None),
                'l': _to_float(lows[i]  if i < len(lows)  else None),
                'c': _to_float(c),
                'v': _to_int  (vols[i]  if i < len(vols)  else None),
            })
        payload = {
            'ticker': t,
            'range': rng,
            'interval': interval,
            'spot':           _to_float(meta.get('regularMarketPrice')),
            'previous_close': _to_float(meta.get('chartPreviousClose') or meta.get('previousClose')),
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
            'POST /refresh', 'GET /tick',
            'GET /health', 'GET /health/data',
            'GET /tradier/options/<T>   (preferred — Tradier Sandbox)',
            'GET /yahoo/options/<T>     (fallback)',
            'GET /yahoo/chart/<T>?range=1d|5d|1mo|3mo|1y',
            'GET /finra/darkpool/<T>?days=1..10',
        ],
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5050)))
